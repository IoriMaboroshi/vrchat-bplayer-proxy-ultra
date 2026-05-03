"""
VRChat BPlayer Proxy API endpoints: /play, /info, /health, /pages, /qualities, /season, /episode
"""

import subprocess
import asyncio
import tempfile
import os
import shutil
import uuid
import time
import logging
from typing import Optional, Tuple, Dict

from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, Response
from starlette.background import BackgroundTask

from config import (
    BILIBILI_UA,
    BILIBILI_REFERER,
    FFMPEG_PATH,
    QUALITY_MAP,
    QUALITY_LABELS,
)
from utils.middleware import verify_token
from utils.geo import lookup_ip
from bilibili.video import (
    get_play_url_comprehensive,
    get_video_info,
    get_video_pages,
    get_season_info,
    get_episode_info,
)
from bilibili.auth import get_current_cookies, load_cookies
from db.models import insert_log

# Configure logging
logger = logging.getLogger("bilibili-proxy.api")

router = APIRouter()

# ============================================================
#  STREAM PROGRESS TRACKING (Fix #7)
# ============================================================

_stream_status: Dict[str, dict] = {}
_status_lock = asyncio.Lock()


async def _set_stream_status(stream_id: str, status: str, progress_pct: float = 0, eta_seconds: float = 0):
    async with _status_lock:
        _stream_status[stream_id] = {
            "status": status,
            "progress_pct": progress_pct,
            "eta_seconds": eta_seconds,
            "updated_at": time.time(),
        }


async def _cleanup_stream_status(stream_id: str):
    async with _status_lock:
        _stream_status.pop(stream_id, None)


async def _log_call(request: Request, bvid: str, qx: str, qn: int, video_url: str, audio_url: str):
    """Log API call asynchronously."""
    try:
        caller_ip = request.client.host if request.client else "unknown"
        geo = await lookup_ip(caller_ip)
        ua = request.headers.get("user-agent", "")
        await insert_log(caller_ip, geo, bvid, qx, qn, video_url, audio_url, ua)
    except Exception:
        pass  # Logging failure should not break the request


def _parse_range_header(range_header: str, total_size: int) -> Optional[Tuple[int, int]]:
    """Parse HTTP Range header. Returns (start_byte, end_byte) or None if invalid."""
    if not range_header:
        return None
    try:
        unit, ranges = range_header.split("=", 1)
        if unit != "bytes":
            return None
        start_str, end_str = ranges.split("-", 1)
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else total_size - 1
        if start < 0 or end >= total_size or start > end:
            return None
        return (start, end)
    except (ValueError, IndexError):
        return None


def _bytes_to_time_offset(byte_offset: int, total_bytes: int, duration: float) -> float:
    """Convert byte offset to approximate time offset in seconds."""
    if total_bytes <= 0 or duration <= 0:
        return 0.0
    return (byte_offset / total_bytes) * duration


# ============================================================
#  /play — CORE STREAMING ENDPOINT
# ============================================================

@router.get("/play")
async def play(
    request: Request,
    bvid: Optional[str] = Query(None, description="Bilibili BV号 (与 ep_id 二选一)"),
    qx: str = Query("1080p", description="清晰度"),
    cid: Optional[int] = Query(None, description="直接指定 cid"),
    t: Optional[int] = Query(None, description="起始时间(秒)"),
    page: Optional[int] = Query(None, description="分P编号(1-indexed)"),
    ep_id: Optional[str] = Query(None, description="番剧/电影 ep_id (如 ep1482617)"),
    enable_hdr: int = Query(0, description="启用 HDR (0/1)"),
    allow_4k: int = Query(1, description="允许 4K (0/1)"),
    allow_8k: int = Query(0, description="允许 8K (0/1)"),
    enable_hires: int = Query(1, description="启用 Hi-Res 无损音质 (0/1)"),
    format: str = Query("fmp4", description="输出格式: fmp4 (默认) / m3u8 / mp4"),
    dis: str = Query("no", description="调试模式: yes=显示调试面板, no=直接播放"),
    _token_valid: None = None,
):
    """
    Stream video as fMP4 (FFmpeg real-time remux).
    Supports Range requests, live HLS, and concurrency control.
    """
    # Check token
    token = request.query_params.get("token", "")
    from config import API_TOKEN, DYNAMIC_SETTINGS
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")

    # Check if logged in
    cookies = get_current_cookies() or load_cookies()
    if not cookies:
        raise HTTPException(status_code=503, detail="未登录 Bilibili 账号，请先通过 Web 面板扫码登录")

    # Validate: must provide bvid or ep_id
    if not bvid and not ep_id:
        raise HTTPException(status_code=400, detail="需要提供 bvid 或 ep_id 参数")

    # Resolve ep_id to bvid+cid if provided
    resolved_bvid = bvid
    resolved_cid = cid
    resolved_bvid_str = resolved_bvid or ""

    if ep_id and not bvid:
        clean_ep_id = ep_id
        if str(ep_id).lower().startswith("ep"):
            clean_ep_id = str(ep_id)[2:]
        ep_data = await get_episode_info(clean_ep_id)
        if "error" in ep_data:
            raise HTTPException(status_code=400, detail=ep_data["error"])
        current = ep_data.get("current", {})
        if not current:
            raise HTTPException(status_code=400, detail="未找到该剧集")
        resolved_bvid = current.get("bvid", "")
        if not resolved_bvid and ep_data.get("episodes"):
            resolved_bvid = ep_data["episodes"][0].get("bvid", "")
        if not resolved_cid:
            resolved_cid = current.get("cid", 0)
        resolved_bvid_str = resolved_bvid or ""

    # Resolve page to cid if provided
    if page is not None and page > 0 and not resolved_cid:
        pages_data = await get_video_pages(resolved_bvid)
        if "error" in pages_data:
            raise HTTPException(status_code=400, detail=pages_data["error"])
        pages = pages_data.get("pages", [])
        if page > len(pages):
            raise HTTPException(status_code=400, detail=f"分P编号超出范围 (共 {len(pages)} 个分P)")
        resolved_cid = pages[page - 1]["cid"]

    # Get play URLs with comprehensive fallback
    play_data = await get_play_url_comprehensive(
        bvid=resolved_bvid,
        cid=resolved_cid,
        qx=qx,
        enable_hdr=bool(enable_hdr),
        allow_4k=bool(allow_4k),
        allow_8k=bool(allow_8k),
        enable_hires=bool(enable_hires),
    )

    if "error" in play_data:
        await _log_call(request, resolved_bvid_str, qx, 0, "", "")
        raise HTTPException(status_code=400, detail=play_data["error"])

    video_url = play_data.get("video_url", "")
    audio_url = play_data.get("audio_url", "")
    qn = play_data.get("actual_qn", 0)
    duration = play_data.get("duration", 0)
    title = play_data.get("title", "")

    # Log the call
    await _log_call(request, resolved_bvid_str, qx, qn, video_url, audio_url)

    # === DEBUG MODE (dis=yes) ===
    if dis.lower() == "yes":
        import urllib.parse as ulp
        # Get DASH codec info
        codec_info = "N/A"
        try:
            from bilibili.wbi import sign_params
            import httpx
            cookies2 = get_current_cookies() or load_cookies()
            params2 = await sign_params({"bvid": resolved_bvid, "cid": resolved_cid, "qn": qn, "fnval": 4048, "fnver": 0, "fourk": 1})
            async with httpx.AsyncClient(timeout=10, cookies=cookies2) as cc:
                r2 = await cc.get("https://api.bilibili.com/x/player/wbi/playurl", params=params2,
                    headers={"User-Agent": BILIBILI_UA, "Referer": BILIBILI_REFERER})
                dd = r2.json()
                dash2 = dd.get("data", {}).get("dash", {})
                vlist = dash2.get("video", [])
                codec_rows = ""
                for v in vlist:
                    cs = v.get("codecs", "")[:40] if v.get("codecs") else "?"
                    cid_map = {7: "AVC/H.264", 12: "HEVC/H.265", 13: "AV1"}
                    cname = cid_map.get(v.get("codecid", 0), "未知(" + str(v.get("codecid", 0)) + ")")
                    selected = " ← 已选用" if v.get("baseUrl", v.get("base_url", "")) == video_url else ""
                    codec_rows += "<tr><td>" + str(v.get("id")) + "</td><td>" + str(v.get("codecid")) + "</td><td>" + cname + "</td><td>" + str(v.get("width")) + "</td><td>" + str(round(v.get("bandwidth", 0)/1000)) + "kbps</td><td>" + cs + "</td><td style='color:#4caf50'>" + selected + "</td></tr>"
                codec_info = codec_rows
        except Exception as e:
            codec_info = "<tr><td colspan='7'>获取失败: " + str(e) + "</td></tr>"

        dbg_url = f"/play?bvid={resolved_bvid or ''}&token={token}&qx={qx}&format={format}&dis=no"
        if resolved_cid: dbg_url += f"&cid={resolved_cid}"
        if t: dbg_url += f"&t={t}"
        dbg_url_no_dis = request.url.path + "?" + "&".join([f"{k}={v}" for k, v in request.query_params.items() if k != "dis"])

        cid_map = {7: "AVC/H.264 ✅ 浏览器兼容", 12: "HEVC/H.265 ⚠️ Chrome/Firefox不支持", 13: "AV1"}
        vcodec_guess = "?"
        try:
            for v in vlist:
                if v.get("baseUrl", v.get("base_url", "")) == video_url:
                    vcodec_guess = cid_map.get(v.get("codecid", 0), "?")
        except: pass

        debug_html = f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><title>BPlayer Debug - {title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0d1117;color:#c9d1d9;padding:16px}}
h2{{color:#58a6ff;margin:16px 0 8px}}
.player{{background:#161b22;border-radius:12px;padding:16px;margin:16px 0;text-align:center}}
.player video{{max-width:100%;max-height:60vh;border-radius:8px;background:#000}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}}
th{{background:#21262d;color:#8b949e;padding:8px 12px;text-align:left;font-weight:500}}
td{{padding:8px 12px;border-bottom:1px solid #21262d}}
.info{{display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px}}
.info div{{background:#161b22;padding:10px 14px;border-radius:8px}}
.info .label{{color:#8b949e}}
.info .value{{color:#c9d1d9;font-weight:500}}
.badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}}
.badge-ok{{background:#1b5e20;color:#81c784}}
.badge-warn{{background:#4a3a2a;color:#ff9800}}
.raw-url{{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;font-family:monospace;font-size:12px;word-break:break-all;color:#7ee787;margin:8px 0}}
</style></head>
<body>
<h1 style="color:#fb7299">🎬 BPlayer Debug Panel</h1>
<div class="player">
    <video controls autoplay muted preload="auto" style="width:100%;max-width:960px">
        <source src="{dbg_url}" type="video/mp4">
        <p>加载中... 首次缓冲约需 5-15 秒</p>
    </video>
    <p style="color:#8b949e;font-size:12px;margin-top:8px">⏳ 首次加载需等待 5-15 秒（FFmpeg 连接 B站 CDN），后续流畅播放</p>
</div>
<div class="info">
    <div><span class="label">标题</span><br><span class="value">{title}</span></div>
    <div><span class="label">BV号</span><br><span class="value">{resolved_bvid or 'N/A'}</span></div>
    <div><span class="label">时长</span><br><span class="value">{duration}s</span></div>
    <div><span class="label">清晰度</span><br><span class="value">{qx} (qn={qn})</span></div>
    <div><span class="label">输出格式</span><br><span class="value">{format}</span></div>
    <div><span class="label">视频编码</span><br><span class="value">{vcodec_guess}</span></div>
    <div><span class="label">协议</span><br><span class="value">DASH (分离音视频流)</span></div>
    <div><span class="label">CID</span><br><span class="value">{resolved_cid or 'N/A'}</span></div>
</div>
<h2>📊 DASH 流编码详情</h2>
<table>
<tr><th>qn</th><th>codecid</th><th>编码</th><th>分辨率</th><th>码率</th><th>codecs</th><th>选用</th></tr>
{codec_info}
</table>
<h2>🔗 播放地址</h2>
<div class="raw-url">{dbg_url_no_dis}</div>
<h2>📋 响应头</h2>
<table>
<tr><td>Content-Type</td><td>video/mp4</td></tr>
<tr><td>Accept-Ranges</td><td>bytes</td></tr>
<tr><td>X-Content-Duration</td><td>{duration}</td></tr>
<tr><td>Transfer-Encoding</td><td>chunked</td></tr>
<tr><td>Cache-Control</td><td>no-cache</td></tr>
</table>
<h2>🎯 编码兼容性说明</h2>
<p style="color:#8b949e;font-size:13px;line-height:1.6">
• <b>AVC/H.264</b> (codecid=7): 所有浏览器和播放器支持 ✅<br>
• <b>HEVC/H.265</b> (codecid=12): 仅 Edge/Safari 支持, Chrome/Firefox 不支持 ⚠️<br>
• <b>AV1</b> (codecid=13): Chrome/Firefox 支持, 硬件解码需新设备<br>
• 本项目已优化：优先选取 AVC/H.264 流以确保最大兼容性
</p>
</body></html>"""
        return Response(debug_html, media_type="text/html")

    # Auto quality degradation
    from config import DYNAMIC_SETTINGS
    enable_auto_degrade = DYNAMIC_SETTINGS.get("enable_auto_degrade", "0")
    max_mbps = float(DYNAMIC_SETTINGS.get("max_bandwidth_mbps", "0"))
    if enable_auto_degrade == "1" and max_mbps > 0:
        from utils.bandwidth import should_degrade, get_total_bandwidth_mbps
        from config import QUALITY_FALLBACK_ORDER, HDR_QUALITIES
        actual_hires = bool(enable_hires)
        adjusted_qn, adjusted_hires = should_degrade(
            target_qn=qn,
            enable_hires=actual_hires,
            max_bandwidth_mbps=max_mbps,
            quality_fallback_order=QUALITY_FALLBACK_ORDER,
            hdr_qualities=HDR_QUALITIES,
            allow_4k=bool(allow_4k),
            allow_8k=bool(allow_8k),
        )
        if adjusted_qn != qn or adjusted_hires != actual_hires:
            degraded_qx = "1080p"
            for qk, qv in QUALITY_MAP.items():
                if qv == adjusted_qn:
                    degraded_qx = qk
                    break
            degraded = await get_play_url_comprehensive(
                bvid=resolved_bvid, cid=resolved_cid, qx=degraded_qx,
                enable_hdr=bool(enable_hdr), allow_4k=bool(allow_4k),
                allow_8k=bool(allow_8k), enable_hires=adjusted_hires,
            )
            if "error" not in degraded:
                video_url = degraded.get("video_url", video_url)
                audio_url = degraded.get("audio_url", audio_url)
                qn = degraded.get("actual_qn", qn)
                logger.info(
                    "[AutoDegrade] QoS: %s, Hi-Res=%s, Total=%.1fMbps",
                    QUALITY_LABELS.get(qn, "?"), adjusted_hires, get_total_bandwidth_mbps(),
                )

    if not video_url:
        raise HTTPException(status_code=400, detail="未获取到视频地址")

    # Register bandwidth tracking
    stream_id = None
    if DYNAMIC_SETTINGS.get("enable_auto_degrade", "0") == "1":
        from utils.bandwidth import register_stream
        stream_id = register_stream(qn=qn, hires=bool(enable_hires))

    # Auto-detect desktop players that can't handle fMP4
    resolved_format = format
    if resolved_format == "fmp4":
        ua = request.headers.get("user-agent", "").lower()
        desktop_players = [
            "potplayer", "mpv", "lavf", "libavformat",
            "daum", "kmplayer", "gomplayer", "smplayer",
            "media player classic", "mpc-hc", "mpc-be",
            "foobar2000", "winamp", "aimp",
        ]
        if any(player in ua for player in desktop_players):
            resolved_format = "mp4"

    # === PARSE RANGE HEADER (Fix #1) ===
    range_header = request.headers.get("range", "")
    range_start_byte = 0
    range_end_byte = 0
    has_range = False
    content_length_estimate = 0

    # Estimate total file size from duration and quality bitrate
    if duration > 0:
        from utils.bandwidth import QUALITY_BANDWIDTH_MAP
        est_mbps = QUALITY_BANDWIDTH_MAP.get(qn, 8.0)
        content_length_estimate = int(duration * est_mbps * 1024 * 1024 / 8)

    if range_header and content_length_estimate > 0:
        parsed = _parse_range_header(range_header, content_length_estimate)
        if parsed:
            range_start_byte, range_end_byte = parsed
            has_range = True
            # Convert byte range to time offset for FFmpeg -ss
            offset_seconds = _bytes_to_time_offset(range_start_byte, content_length_estimate, duration)
            if offset_seconds > 0 and (not t or offset_seconds > t):
                t = int(offset_seconds)

    # === FORMAT: M3U8 (Fix #3 — live streaming) ===
    if resolved_format == "m3u8":
        hls_id = uuid.uuid4().hex[:8]
        tmp_dir = f"/tmp/hls_{hls_id}"
        os.makedirs(tmp_dir, exist_ok=True)
        playlist_path = os.path.join(tmp_dir, "playlist.m3u8")

        # Start stream status tracking
        await _set_stream_status(hls_id, "processing", 0, duration if duration else 30)

        ff_cmd = [FFMPEG_PATH, "-y", "-threads", "2"]
        if t and t > 0:
            ff_cmd += ["-ss", str(t)]
        ff_cmd += [
            "-headers", f"Referer: {BILIBILI_REFERER}\r\nUser-Agent: {BILIBILI_UA}\r\n",
            "-i", video_url,
        ]
        if audio_url:
            ff_cmd += [
                "-headers", f"Referer: {BILIBILI_REFERER}\r\nUser-Agent: {BILIBILI_UA}\r\n",
                "-i", audio_url,
            ]
        ff_cmd += ["-c", "copy", "-map", "0:v:0"]
        if audio_url:
            ff_cmd += ["-map", "1:a:0"]
        if duration and duration > 0:
            ff_cmd += ["-t", str(int(float(duration)) + 2)]
        ff_cmd += [
            "-f", "hls",
            "-hls_time", "6",
            "-hls_list_size", "10",
            "-hls_segment_type", "fmp4",
            "-hls_flags", "independent_segments+delete_segments",
            "-hls_segment_filename", os.path.join(tmp_dir, "seg_%03d.m4s"),
            "-hls_playlist_type", "event",
            playlist_path,
        ]

        # Use Popen for live HLS streaming
        ffmpeg_proc = subprocess.Popen(
            ff_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        # Wait for first segments to be generated (max 5 seconds)
        start_wait = time.time()
        initial_playlist_content = ""
        while time.time() - start_wait < 8:
            await asyncio.sleep(0.3)
            if os.path.exists(playlist_path):
                with open(playlist_path) as f:
                    content = f.read()
                if "seg_001" in content or (content.strip() and len(content.strip().split("\n")) > 5):
                    initial_playlist_content = content
                    break

        if not initial_playlist_content:
            # Wait a bit more
            await asyncio.sleep(2)
            if os.path.exists(playlist_path):
                with open(playlist_path) as f:
                    initial_playlist_content = f.read()

        if not initial_playlist_content.strip():
            ffmpeg_proc.kill()
            ffmpeg_proc.wait()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            await _cleanup_stream_status(hls_id)
            raise HTTPException(status_code=500, detail="HLS 编码启动失败")

        # Build playlist with absolute URLs
        req_host = request.headers.get("host", "8.148.64.28:14513")
        parts = req_host.split(":")
        req_hostname = parts[0]
        req_port = parts[1] if len(parts) > 1 else "14513"
        base = f"http://{req_hostname}:{req_port}/segments/{hls_id}"

        playlist_content = initial_playlist_content
        playlist_content = playlist_content.replace('URI="init.mp4"', f'URI="{base}/init.mp4"')
        playlist_content = playlist_content.replace("seg_", f"{base}/seg_")

        # Live HLS: don't use VOD type initially (toggle event instead)
        if "#EXT-X-PLAYLIST-TYPE:" not in playlist_content:
            playlist_content = playlist_content.replace("#EXT-X-TARGETDURATION:", "#EXT-X-PLAYLIST-TYPE:EVENT\n#EXT-X-TARGETDURATION:", 1)

        # Remove ENDLIST for live mode
        playlist_content = playlist_content.replace("#EXT-X-ENDLIST\n", "").replace("#EXT-X-ENDLIST", "")

        # Store playlist path for regeneration
        _active_hls[hls_id] = {
            "tmp_dir": tmp_dir,
            "playlist_path": playlist_path,
            "process": ffmpeg_proc,
            "base": base,
            "started_at": time.time(),
            "duration": duration,
        }

        # Background task to update stream status
        async def _monitor_hls():
            try:
                while True:
                    await asyncio.sleep(1)
                    if os.path.exists(playlist_path):
                        with open(playlist_path) as f:
                            content = f.read()
                        seg_count = content.count("seg_")
                        if duration > 0:
                            progress = min(seg_count * 6 / duration * 100, 99)
                            eta = max(0, (duration - seg_count * 6))
                            await _set_stream_status(hls_id, "processing", progress, eta)
                    if ffmpeg_proc.poll() is not None:
                        await _set_stream_status(hls_id, "ready", 100, 0)
                        # Add ENDLIST when done
                        if os.path.exists(playlist_path):
                            with open(playlist_path, "a") as f:
                                f.write("#EXT-X-ENDLIST\n")
                        break
            except Exception:
                pass
        asyncio.create_task(_monitor_hls())

        import urllib.parse
        resp_headers = {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "max-age=5",
        }
        if duration > 0:
            resp_headers["X-Content-Duration"] = str(float(duration))
        if title:
            resp_headers["X-Content-Title"] = urllib.parse.quote(title, safe="")

        return Response(
            playlist_content,
            media_type="application/vnd.apple.mpegurl",
            headers=resp_headers,
        )

    # === FORMAT: MP4 (with progress tracking — Fix #7) ===
    elif resolved_format == "mp4":
        mp4_id = uuid.uuid4().hex[:8]
        await _set_stream_status(mp4_id, "processing", 0, duration if duration else 30)

        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp_path = tmp.name
        tmp.close()

        ff_cmd = [FFMPEG_PATH, "-y", "-threads", "2"]
        if t and t > 0:
            ff_cmd += ["-ss", str(t)]
        ff_cmd += [
            "-headers", f"Referer: {BILIBILI_REFERER}\r\nUser-Agent: {BILIBILI_UA}\r\n",
            "-i", video_url,
        ]
        if audio_url:
            ff_cmd += [
                "-headers", f"Referer: {BILIBILI_REFERER}\r\nUser-Agent: {BILIBILI_UA}\r\n",
                "-i", audio_url,
            ]
        ff_cmd += ["-c", "copy", "-map", "0:v:0"]
        if audio_url:
            ff_cmd += ["-map", "1:a:0"]
        ff_cmd += ["-f", "mp4", "-movflags", "faststart", tmp_path]

        # Start FFmpeg in background with progress tracking
        proc = subprocess.Popen(
            ff_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        async def _monitor_mp4():
            try:
                last_size = 0
                while proc.poll() is None:
                    await asyncio.sleep(0.5)
                    if os.path.exists(tmp_path):
                        current_size = os.path.getsize(tmp_path)
                        if content_length_estimate > 0:
                            progress = min(current_size / content_length_estimate * 100, 99)
                            eta = max(0, (content_length_estimate - current_size) / max(current_size - last_size, 1) * 0.5)
                            await _set_stream_status(mp4_id, "processing", progress, eta)
                        last_size = max(current_size, last_size)
                if proc.returncode == 0:
                    await _set_stream_status(mp4_id, "ready", 100, 0)
                else:
                    await _set_stream_status(mp4_id, "error", 0, 0)
            except Exception:
                pass
        asyncio.create_task(_monitor_mp4())

        # Wait for FFmpeg completion (with timeout)
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: proc.wait(timeout=600),
        )

        await _set_stream_status(mp4_id, "ready", 100, 0)

        if proc.returncode != 0:
            try: os.unlink(tmp_path)
            except: pass
            await _cleanup_stream_status(mp4_id)
            raise HTTPException(status_code=500, detail="FFmpeg 处理失败")

        def cleanup():
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            if stream_id:
                from utils.bandwidth import unregister_stream
                unregister_stream(stream_id)

        import urllib.parse
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{urllib.parse.quote(title or bvid or "video", safe="")}.mp4"',
        }
        if duration > 0:
            headers["X-Content-Duration"] = str(float(duration))
        if title:
            headers["X-Content-Title"] = urllib.parse.quote(title, safe="")

        return FileResponse(
            tmp_path,
            media_type="video/mp4",
            headers=headers,
            background=BackgroundTask(cleanup),
        )

    # === FORMAT: fMP4 (simple streaming) ===
    else:
        ff_cmd = [FFMPEG_PATH, "-threads", "2"]

        # Apply seek offset (from t param or Range header)
        if t and t > 0:
            ff_cmd += ["-ss", str(t)]

        ff_cmd += [
            "-headers", f"Referer: {BILIBILI_REFERER}\r\nUser-Agent: {BILIBILI_UA}\r\n",
            "-i", video_url,
        ]

        if audio_url:
            ff_cmd += [
                "-headers", f"Referer: {BILIBILI_REFERER}\r\nUser-Agent: {BILIBILI_UA}\r\n",
                "-i", audio_url,
            ]

        ff_cmd += ["-c", "copy", "-map", "0:v:0"]
        if audio_url:
            ff_cmd += ["-map", "1:a:0"]

        ff_cmd += [
            "-f", "mp4",
            "-movflags", "frag_keyframe+default_base_moof+dash",
            "-fflags", "+genpts",
            "-flush_packets", "1",
            "pipe:1",
        ]

        process = subprocess.Popen(
            ff_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        async def stream():
            try:
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
                    await asyncio.sleep(0)
            finally:
                process.kill()
                try:
                    process.wait(timeout=5)
                except: pass
                if stream_id:
                    from utils.bandwidth import unregister_stream
                    unregister_stream(stream_id)

        # Build response headers (Fix #1)
        import urllib.parse
        headers = {
            "Content-Disposition": "inline",
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
        }
        if duration > 0:
            headers["X-Content-Duration"] = str(float(duration))
        if title:
            headers["X-Content-Title"] = urllib.parse.quote(title, safe="")

        # For fMP4 streaming, do NOT set Content-Length (actual stream size is unknown).
        # For Range requests, set Content-Range with the total estimate but not Content-Length
        # since the stream is remuxed on-the-fly and real size differs from estimate.

        if has_range:
            headers["Content-Range"] = f"bytes {range_start_byte}-{range_end_byte}/{content_length_estimate}"
            return StreamingResponse(
                stream(),
                media_type="video/mp4",
                headers=headers,
                status_code=206,
            )
        else:
            return StreamingResponse(
                stream(),
                media_type="video/mp4",
                headers=headers,
            )


# ============================================================
#  HLS ACTIVE STREAMS STORAGE (for segment serving + playlist updates)
# ============================================================

_active_hls: Dict[str, dict] = {}


@router.get("/segments/{stream_id}/{filename}")
async def serve_segment(stream_id: str, filename: str):
    """Serve HLS .m4s segment files."""
    seg_path = f"/tmp/hls_{stream_id}/{filename}"
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=404, detail="Segment not found")
    mime = "video/mp4" if filename.endswith(".m4s") else "video/mp2t"
    return FileResponse(seg_path, media_type=mime)


@router.get("/segments/{stream_id}/playlist.m3u8")
async def serve_hls_playlist(stream_id: str):
    """Serve updated HLS playlist for live streaming."""
    playlist_path = f"/tmp/hls_{stream_id}/playlist.m3u8"
    if not os.path.exists(playlist_path):
        raise HTTPException(status_code=404, detail="Playlist not found")

    with open(playlist_path) as f:
        content = f.read()

    # Inject absolute URLs
    if stream_id in _active_hls:
        base = _active_hls[stream_id]["base"]
        content = content.replace('URI="init.mp4"', f'URI="{base}/init.mp4"')
        content = content.replace("seg_", f"{base}/seg_")
        if "#EXT-X-PLAYLIST-TYPE:" not in content:
            content = content.replace("#EXT-X-TARGETDURATION:", "#EXT-X-PLAYLIST-TYPE:EVENT\n#EXT-X-TARGETDURATION:", 1)
        content = content.replace("#EXT-X-ENDLIST\n", "").replace("#EXT-X-ENDLIST", "")

    return Response(content, media_type="application/vnd.apple.mpegurl")


# ============================================================
#  INFO / PAGES / QUALITIES / SEASON / EPISODE ENDPOINTS
# ============================================================

@router.get("/info")
async def video_info(
    bvid: str = Query(..., description="Bilibili BV号"),
    cid: Optional[int] = Query(None, description="分P cid"),
    page: Optional[int] = Query(None, description="分P编号(1-indexed)"),
    _token_valid: None = None,
):
    """Get video metadata with optional page-specific info."""
    info = await get_video_info(bvid, cid=cid)
    if "error" in info:
        return JSONResponse(info, status_code=400)
    return JSONResponse(info)


@router.get("/pages")
async def video_pages(
    bvid: str = Query(..., description="Bilibili BV号"),
    _token_valid: None = None,
):
    """Get list of pages for multi-P videos."""
    pages_data = await get_video_pages(bvid)
    if "error" in pages_data:
        return JSONResponse(pages_data, status_code=400)
    return JSONResponse(pages_data)


@router.get("/qualities")
async def video_qualities(
    bvid: str = Query(..., description="Bilibili BV号"),
    cid: Optional[int] = Query(None, description="分P cid"),
    _token_valid: None = None,
):
    """Get available quality options for a video."""
    play_data = await get_play_url_comprehensive(
        bvid=bvid,
        cid=cid,
        qx="1080p",
    )
    if "error" in play_data:
        return JSONResponse(play_data, status_code=400)

    all_qns = play_data.get("all_available_qualities", [])
    qualities = []
    for qn in all_qns:
        qualities.append({
            "qn": qn,
            "label": QUALITY_LABELS.get(qn, f"未知 ({qn})"),
        })

    return JSONResponse({
        "bvid": bvid,
        "cid": play_data.get("cid"),
        "qualities": qualities,
    })


@router.get("/season")
async def season_info(
    season_id: str = Query(..., description="剧集 season_id"),
    _token_valid: None = None,
):
    """Get season info and episode list."""
    data = await get_season_info(season_id)
    if "error" in data:
        return JSONResponse(data, status_code=400)
    return JSONResponse(data)


@router.get("/episode")
async def episode_info(
    ep_id: str = Query(..., description="剧集 ep_id"),
    _token_valid: None = None,
):
    """Get episode info."""
    clean_ep_id = ep_id
    if str(ep_id).lower().startswith("ep"):
        clean_ep_id = str(ep_id)[2:]
    data = await get_episode_info(clean_ep_id)
    if "error" in data:
        return JSONResponse(data, status_code=400)
    return JSONResponse(data)


# ============================================================
#  TOKEN INFO ENDPOINT (Fix #6)
# ============================================================

@router.get("/api/token-info")
async def token_info():
    """Return current API token (masked and full). Used by URL generator."""
    from config import API_TOKEN as CFG_TOKEN
    full = CFG_TOKEN
    masked = full[:4] + "****" + full[-4:] if len(full) > 8 else "****"
    return JSONResponse({
        "token": full,
        "masked": masked,
    })


# ============================================================
#  STREAM STATUS ENDPOINT (Fix #7)
# ============================================================

@router.get("/api/stream-status/{stream_id}")
async def stream_status(stream_id: str):
    """Get progress of an MP4/M3U8 encoding stream."""
    async with _status_lock:
        status = _stream_status.get(stream_id)
    if not status:
        return JSONResponse({"status": "not_found", "stream_id": stream_id}, status_code=404)
    return JSONResponse({
        "stream_id": stream_id,
        "status": status["status"],
        "progress_pct": status["progress_pct"],
        "eta_seconds": status["eta_seconds"],
    })


# ============================================================
#  VIDEO SUMMARY (for URL generator)
# ============================================================

@router.get("/video-summary")
async def video_summary(
    request: Request,
    bvid: Optional[str] = Query(None, description="Bilibili BV号"),
    ep_id: Optional[str] = Query(None, description="番剧 ep_id (如 ep1482617)"),
    _token_valid: None = None,
):
    """Get comprehensive video summary for the URL generator."""
    token = request.query_params.get("token", "")
    from config import API_TOKEN
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")

    if not bvid and not ep_id:
        raise HTTPException(status_code=400, detail="需要提供 bvid 或 ep_id 参数")

    resolved_bvid: Optional[str] = bvid
    if ep_id and not bvid:
        clean_ep_id = str(ep_id)
        if clean_ep_id.lower().startswith("ep"):
            clean_ep_id = clean_ep_id[2:]
        ep_data = await get_episode_info(clean_ep_id)
        if "error" in ep_data:
            raise HTTPException(status_code=400, detail=ep_data["error"])
        current = ep_data.get("current", {})
        if not current:
            raise HTTPException(status_code=400, detail="未找到该剧集")
        resolved_bvid = current.get("bvid", "")
        if not resolved_bvid and ep_data.get("episodes"):
            resolved_bvid = ep_data["episodes"][0].get("bvid", "")

    if not resolved_bvid:
        raise HTTPException(status_code=400, detail="无法解析视频 ID")

    info = await get_video_info(resolved_bvid)
    if "error" in info:
        raise HTTPException(status_code=400, detail=info["error"])

    play_data = await get_play_url_comprehensive(
        bvid=resolved_bvid,
        qx="1080p",
        enable_hires=True,
        allow_4k=True,
        allow_8k=True,
    )

    qualities: list = []
    all_qns = play_data.get("all_available_qualities", [])
    for qn in all_qns:
        qualities.append({
            "qn": qn,
            "label": QUALITY_LABELS.get(qn, f"未知 ({qn})"),
        })

    has_hires = False
    if "error" not in play_data:
        has_hires = play_data.get("audio_url", "") != ""

    return JSONResponse({
        "bvid": resolved_bvid,
        "title": info.get("title", ""),
        "cover": info.get("cover", ""),
        "duration": info.get("duration", 0),
        "owner": info.get("owner", {}),
        "pages": info.get("pages", []),
        "qualities": qualities,
        "has_hires": has_hires,
    })


# ============================================================
#  HEALTH ENDPOINT (Fix #8 + #11)
# ============================================================

@router.get("/health")
async def health():
    """Health check with system metrics, cookie status, and active streams."""
    cookies = get_current_cookies() or load_cookies()
    from config import DYNAMIC_SETTINGS

    # Cookie expiry check (Fix #8)
    cookie_info = {
        "logged_in": bool(cookies),
        "expires_in": "unknown",
    }
    if cookies:
        from bilibili.auth import _cookie_expiry_estimate
        cookie_info["expires_in"] = _cookie_expiry_estimate()

    # System metrics (Fix #11)
    system_info = {
        "active_streams": 0,  # concurrency limit removed per user request
    }

    try:
        import psutil
        system_info["cpu_percent"] = round(psutil.cpu_percent(interval=0.1), 1)
        system_info["memory_percent"] = round(psutil.virtual_memory().percent, 1)
        system_info["disk_percent"] = round(psutil.disk_usage("/").percent, 1)
    except ImportError:
        # Fallback to /proc if psutil not available
        try:
            import resource
            import os as _os_module
            # Memory from /proc/meminfo
            with open("/proc/meminfo") as f:
                mem = f.read()
            total = int([l for l in mem.split("\n") if "MemTotal" in l][0].split()[1])
            available = int([l for l in mem.split("\n") if "MemAvailable" in l][0].split()[1])
            system_info["memory_percent"] = round((total - available) / total * 100, 1)
            # CPU from /proc/stat
            with open("/proc/stat") as f:
                cpu_line = f.readline()
            cpu_parts = [int(x) for x in cpu_line.split()[1:]]
            cpu_idle = cpu_parts[3]
            cpu_total = sum(cpu_parts)
            system_info["cpu_percent"] = round((1 - cpu_idle / cpu_total) * 100, 1)
            # Disk from statvfs
            stat = _os_module.statvfs("/")
            disk_total = stat.f_frsize * stat.f_blocks
            disk_free = stat.f_frsize * stat.f_bavail
            system_info["disk_percent"] = round((1 - disk_free / disk_total) * 100, 1)
        except Exception:
            system_info["cpu_percent"] = -1
            system_info["memory_percent"] = -1
            system_info["disk_percent"] = -1

    # Uptime
    try:
        with open("/proc/uptime") as f:
            uptime_seconds = int(float(f.readline().split()[0]))
        system_info["uptime_seconds"] = uptime_seconds
    except Exception:
        system_info["uptime_seconds"] = -1

    return JSONResponse({
        "status": "ok",
        "bilibili_logged_in": bool(cookies),
        "service": "vrchat-bplayer-proxy",
        "cookie": cookie_info,
        "system": system_info,
        "settings": {
            "default_quality": DYNAMIC_SETTINGS.get("default_quality", "1080p"),
            "enable_hdr": DYNAMIC_SETTINGS.get("enable_hdr", "0"),
            "allow_4k": DYNAMIC_SETTINGS.get("allow_4k", "1"),
            "allow_8k": DYNAMIC_SETTINGS.get("allow_8k", "0"),
            "max_concurrent_streams": DYNAMIC_SETTINGS.get("max_concurrent_streams", "4"),
        },
    })
