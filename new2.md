# VRChat BPlayer Proxy — 完整项目明细报告

> 生成时间: 2026-05-03
> 项目目录: G:\opencodeworkspace\vrchat-bplayer-proxy-main
> 当前状态: **开发中，核心功能可用但存在遗留问题**

---

## 一、项目概述

### 目的
将 Bilibili DASH 分离流（音视频独立）实时转换为 **HLS (M3U8+TS)** 单一流，使 VRChat 视频播放器、浏览器、VLC/PotPlayer 等能直接播放 B 站高清视频。

### 核心原理
```
B站 DASH API → 获取视频/音频 URL → FFmpeg 实时封装/转码 → HLS segment → 播放器
```

### 源编码处理策略

| 源编码 | codecid | 处理方式 | CPU/GPU 负载 |
|--------|---------|----------|---------------|
| AVC/H.264 | 7 | `-c:v copy` 直接复制（零开销） | 极低 |
| HEVC/H.265 | 12 | GPU 硬件转码为 H.264 | 低—中 |
| AV1 | 13 | GPU 硬件转码为 H.264 | 中 |

---

## 二、技术架构

### 语言/框架
| 组件 | 版本/说明 |
|------|-----------|
| Python | 3.14（UV 包管理） |
| Web框架 | FastAPI 0.133 |
| 服务器 | Uvicorn 0.46（双实例） |
| 模板引擎 | Jinja2 3.1（Starlette 0.45.3 锁死，兼容 Python 3.14 bug） |
| HTTP客户端 | httpx 0.28（异步 B 站 API 请求） |
| 数据库 | SQLite / aioSQLite |
| 音视频核心 | FFmpeg（本地 8.1 AMF / Docker 7.1 VA-API） |

### 模块结构
```
main.py               → 双 uvicorn 启动（API + Web），lifespan 管理
api/routes.py         → 核心 /play 端点、HLS 流管理、segment 服务、播放器页面
web/routes.py         → Web 面板路由（仪表盘/统计/生成器/设置/登录）
web/middleware.py      → Token 验证 + Web Session 管理
utils/codec_adapter.py → FFmpeg 编码器探测、验证、命令构建
bilibili/video.py     → B 站 DASH URL 解析、清晰度自动回退
bilibili/auth.py      → B 站扫码登录、Cookie 管理、用户信息
bilibili/wbi.py       → WBI 签名算法
config.py             → 全局配置、动态设置加载
db/models.py          → SQLite 查询封装（日志/设置/统计）
templates/*.html      → Web 面板 Jinja2 模板
data/                 → cookies.json / transcode_settings.json / 日志
```

### 独立路由架构（双端口）
```
API 服务 (14515)
  ├── /play              → 视频播放（生成 HLS 流）
  ├── /segments/{id}/    → HLS playlist + segment 文件服务
  ├── /info              → 视频元信息
  ├── /pages             → 分P列表
  ├── /qualities         → 可用清晰度
  ├── /season            → 番剧整季信息
  ├── /episode           → 剧集信息
  ├── /video-summary     → 视频摘要（生成器用）
  ├── /health            → 健康检查 + 系统指标
  ├── /api/token-info    → 当前 API Token
  ├── /api/stream-status → 编码进度
  ├── /api/quality-aliases → 自定义清晰度别名（CRUD）
  └── /api/transcode-settings → 转码限制设置（CRUD）

Web 面板 (8080)
  ├── /login             → Web 账号登录
  ├── /dashboard         → 控制台（统计图表）
  ├── /stats             → 详细统计
  ├── /generator         → URL 生成器
  ├── /settings          → 全配置管理
  ├── /help              → API 文档
  ├── /qr-login          → B 站扫码登录
  └── /poll-login        → 扫码轮询
```

---

## 三、播放流程详解（`/play` 端点）

### 完整请求生命周期
```
1. 参数解析 → bvid / ep_id / qx / token / page / cid / t / dis
2. Token 验证 → 403 无效则拒绝
3. Cookie 检查 → 无 Cookie 503
4. 清晰度解析 → 别名映射 → 屏蔽检查
5. 调用 get_play_url_comprehensive() → B 站 DASH API
   ├── fnval=2128（DASH+4K+AV1）
   ├── WBI 签名
   ├── Cookie 携带
   └── 清晰度自动回退（best→worst: 4k→1080p60→1080p→720p→480p→360p）
6. 获取结果：video_url / audio_url / codecid / qn / duration / title
7. 构建 FFmpeg 命令
   ├── AVC 源 + 无降级 → _build_avc_copy_command()（-c:v copy）
   └── HEVC/AV1 或需降级 → build_hls_command()（GPU 转码 + scale）
8. 启动 FFmpeg 子进程（Popen）
   ├── 输出到 TMP\hls_{stream_id}\
   ├── playlist.m3u8 + seg_000.ts ~ seg_xxx.ts
   └── stderr 写入 ffmpeg_stderr.log
9. 注册活动流：_active_hls[stream_id]
10. 后台监控：asyncio.create_task(_monitor_hls)
11. 等待首段生成（最多 4 秒）

### 自适应等待策略（按视频时长+客户端类型）

| 客户端 | 视频时长 | 最大等待 | 播放模式 | 说明 |
|--------|----------|----------|----------|------|
| 浏览器 | <10分钟 | duration/5秒（5~15s） | VOD（完成则完整播放） | 简短等待换最佳体验 |
| 浏览器 | ≥10分钟 | 5秒 | EVENT（即时播放） | 不等编码，动态加载 |
| VLC/PotPlayer | 任意 | duration/5秒（15~60s） | VOD（完整playlist） | 播放器需要ENDLIST |
| 其他 | 任意 | 5秒 | EVENT | 降级策略 |

12. 返回响应：
    - 浏览器 → HTML 播放器页面（含 hls.js + 自定义进度条）
    - VLC/播放器 → 302 重定向到 /segments/{id}/playlist.m3u8
```

### FFmpeg 关键参数
```
-fflags +genpts+igndts               # 修正 DASH 分离流时间戳
-analyzeduration 5M -probesize 5M    # 快速分析输入（已从10M优化）
-async 1                             # 强制音频时间戳对齐
-ss {N}                              # 起始时间跳转（秒）
-c:v copy                            # AVC源：直接复制视频流（零开销）
-c:v h264_amf                        # AMD GPU转码
-c:a aac -b:a 128k                   # 音频重编码为 AAC 128kbps
-hls_list_size 0                     # 保留所有 segment（不限制数量）
-hls_playlist_type event             # EVENT 模式（逐段追加，不滑动窗口）
-hls_flags independent_segments+delete_segments  # 独立segment + 自动清理
-hls_init_time 2 -hls_time 4         # 初始segment 2s / 后续 4s
-f hls                                # HLS 输出格式
-hls_segment_filename seg_%03d.ts     # segment 文件名模板
```

---

## 四、HLS 流管理机制

### 存储
- **临时目录**：`%TEMP%/hls_{stream_id}/`（Windows）/ `/tmp/hls_{stream_id}/`（Linux）
- **Playlist**：`playlist.m3u8`（动态写入，FFmpeg 每完成一个 segment 更新）
- **Segment**：`seg_000.ts` ~ `seg_xxx.ts`（MPEG-TS 格式，含音视频）

### 生命周期
- **创建**：`/play` 请求时创建，`uuid4().hex[:8]` 作为 stream_id
- **监控**：`_monitor_hls` 后台任务，每1秒更新编码进度
  - FFmpeg 完成时自动标记 `#EXT-X-ENDLIST`（由 FFmpeg 自身写入）
- **清理**：`_cleanup_stale_hls` 后台任务，每60秒清理超过10分钟的流
  - 杀掉 FFmpeg 进程 + 删除临时目录

### 路由服务
- `GET /segments/{stream_id}/playlist.m3u8` → `serve_hls_playlist()`（优先注册）
  - 返回 `application/vnd.apple.mpegurl`
  - Segment 路径自动转为绝对 URL（兼容手机播放器）
- `GET /segments/{stream_id}/{filename}` → `serve_segment()`（通用路由）
  - 返回 `video/mp2t`（.ts）或 `application/octet-stream`

---

## 五、编码器支持矩阵

### GPU 检测流程
```
platform.system() == "Windows" → PowerShell WMI 查询 GPU 名称
platform.system() == "Linux"   → /proc/driver/nvidia 或 lspci
_validate_encoder_works()     → 640x360 5帧实测验证
```

### 编码器选择优先级
```
1. 匹配 GPU 厂商的硬件编码器（验证通过者优先）
2. VA-API（AMD Linux通用后备，需 /dev/dri）
3. libx264（软件兜底）
```

### Linux（Docker/宿主机）
| GPU | 编码器 | 检测方式 | Docker 要求 |
|-----|--------|----------|-------------|
| AMD | h264_vaapi | lspci | `--device /dev/dri` + `video` group |
| NVIDIA | h264_nvenc | /proc/driver/nvidia | `--gpus all` + nvidia-container-toolkit |
| Intel | h264_qsv | lspci | `--device /dev/dri` |
| 兜底 | libx264 | 软件 | 无需 GPU |

### Windows（本地）
| GPU | 编码器 | 检测方式 | 性能（RX7900XTX实测） |
|-----|--------|----------|----------------------|
| AMD | h264_amf | PowerShell WMI | copy 81.8x / 转码 待测 |
| NVIDIA | h264_nvenc | WMI | 待测 |
| Intel | h264_qsv | WMI | 待测 |
| 兜底 | libx264 | 软件 | 慢 |

### 各部署环境编码器现状
| 部署方式 | 编码器 | 性能 | 备注 |
|----------|--------|------|------|
| 本地 UV | h264_amf (RX 7900 XTX) | ~81.8x copy | ✅ 当前主力 |
| Docker Desktop | libx264 | 慢 | ⏸️ GPU 不可用（无 /dev/dri） |
| OpenWRT Docker | h264_vaapi (A8-8700K) | ~2.3x | ❌ 已卸载 |

---

## 六、Web 面板功能清单

### 页面列表
| 页面 | 路由 | 功能 | 登录要求 |
|------|------|------|----------|
| 登录 | `/login` | Web 账号密码登录 | 否 |
| 仪表盘 | `/dashboard` | B站账号信息、调用统计图表 | 是 |
| 统计 | `/stats` | 详细调用记录、IP统计 | 是 |
| 生成器 | `/generator` | 视频查询、URL生成、质量选择 | 是 |
| 设置 | `/settings` | 全配置管理 | 是 |
| API文档 | `/help` | API 使用说明 | 是 |
| 扫码登录 | `/qr-login` | B站二维码登录 | 否 |
| 扫码轮询 | `/poll-login` | AJAX 轮询扫码状态 | 否 |

### 设置页配置项明细

| 设置项 | 存储位置 | 类型 | 说明 | 即时生效 |
|--------|----------|------|------|----------|
| API Token | DB `api_token` | 展示+重置按钮 | 查看/重置 API Token | 是 |
| 默认清晰度 | DB `default_quality` | 下拉选择 | 对应 QUALITY_MAP | 是 |
| 用户名修改 | DB `web_username` | 输入框 | 修改 Web 登录用户名 | 是 |
| 修改密码 | DB（SHA256哈希） | 输入框×3 | 当前密码+新密码+确认 | 是 |
| 启用登录验证 | DB `enable_web_auth` | checkbox | Web 面板是否需要登录 | 是 |
| FFmpeg 路径 | DB `ffmpeg_path` | 输入框 | 自定义 FFmpeg 路径 | 否（需重启） |
| API 端口 | DB `api_port` | 数字输入 | 修改 API 端口 | 否（需重启） |
| Web 面板端口 | DB `web_port` | 数字输入 | 修改 Web 面板端口 | 否（需重启） |
| 日志保留天数 | DB `log_retention_days` | 数字输入 | 日志清理周期 | 是 |
| Cookie 刷新间隔 | DB `cookie_refresh_interval` | 数字输入 | B站 Cookie 轮询间隔(小时) | 是 |
| **最高画质上限** | transcode_settings.json | 下拉选择 | 无限制/360p~4K，超出转码降级 | 是 |
| 登出所有会话 | - | 按钮 | 清除 Web 会话 | 是 |
| 恢复出厂设置 | DB | 按钮 | 重置所有设置 | 否（需重启） |

### 已删除的历史设置项
| 设置项 | 删除原因 |
|--------|----------|
| 启用 HDR | focus VRChat，不需要 |
| 允许 4K | 改用最高画质上限统一管理 |
| 允许 8K | 同上 |
| 启用 Hi-Res 音质 | VRChat 不需要 |
| 启用自动降级 | 简化逻辑 |
| 默认输出格式 | 只保留 M3U8/HLS |
| 最大并发流数 | 不再限制 |
| 最大带宽 | 不再限制 |

---

## 七、当前问题清单

### 🔴 严重问题（阻塞正常体验）

#### 1. 音画不同步
- **现象**：前几秒完全无声，后续音频从 t=0 开始播放，视频却在 t=N 位置，造成严重的音画偏移
- **已尝试修复**：
  - 添加 `-fflags +genpts+igndts`（生成新PTS，忽略输入DTS）
  - 添加 `-async 1`（强制音频时间戳对齐）
  - 添加 `-af aresample=async=1`（音频重采样同步）
- **状态**：❌ 未解决
- **根因分析**：
  - B站 DASH 的视频流和音频流是**完全独立的两个 CDN URL**，各自拥有独立的内部时间戳
  - 使用 `-c:v copy`（视频直接复制）时，FFmpeg **不会改动视频码流内部数据**，因此 `+genpts` 等参数对 copy 模式**无效**
  - copy 模式的视频时间戳直接继承源流，而音频经过 AAC 重编码会获得新的时间戳，两者起点不一致
  - 音频流的起始 PTS 可能在 0 附近，而视频流可能在 1000+ （取决于 keyframe 位置）
- **可能解决方案**：
  - **方案A**：强制视频也走 decode→encode 路径（不用 copy），用 `h264_amf` 重新编码来对齐
  - **方案B**：使用 `-itsoffset {t}` 手动设置音频输入偏移
  - **方案C**：对 copy 模式使用 `-bsf:v h264_mp4toannexb` + `-start_at_zero` 强制归零
  - **方案D**：从 B站 API 响应中分析音视频流的实际起始 PTS 差异，动态补偿

#### 2. VLC / MX 播放器无法播放
- **现象**：手机端 VLC 和 MX Player 打开播放链接后报错或静默失败
- **已尝试修复**：
  - playlist 中 segment 路径从相对路径改为绝对 URL
  - 确保 Content-Type 返回 `application/vnd.apple.mpegurl`
- **状态**：❌ 未解决
- **可能原因**：
  - VLC 对 HLS v6+ / EVENT 类型 playlist 支持不完善
  - 手机播放器收到 302 重定向后，请求 playlist 时未正确携带 Referer 等头
  - 音频流缺失/异常导致播放器拒绝播放（音画不同步可能是诱因）
  - 手机端 VLC 通过参数化 URL 访问 m3u8 存在兼容性问题
  - 绝对 URL 中包含 `localhost`（手机无法解析，但局域网 IP 应正常）

### 🟡 中等问题

#### 3. 加载速度偏慢（~7秒首响）
- **原因**：
  - B站 API 请求 + WBI 签名 ≈ 1秒
  - FFmpeg 初始化 + 输入流分析（analyzeduration 5M）≈ 1-2秒
  - 等待首段生成 ≈ 2-3秒
  - HTML 页面渲染 + hls.js 初始化 ≈ 1秒
- **已优化**：
  - analyzeduration 从 10M → 5M
  - 首段等待超时从 8秒 → 4秒
- **潜力**：可改为预加载（B站 DASH URL 获取和 FFmpeg 初始化异步并行）

#### 4. 长视频播放器原生进度条无总时长
- **现象**：长视频以 EVENT 模式播放时，hls.js 原生播放器不显示总时长
- **已缓解**：通过自定义进度条 + 总时长标签覆盖显示
- **根治**：需在 VOD 模式下返回（即等待 FFmpeg 完成），但对长视频不现实

### 🔵 小问题

#### 5. API/Web 端口修改后需手动重启
- 动态设置写入 DB 但 uvicorn 已绑定，下次启动才生效

#### 6. HLS 流 10 分钟自动过期
- `_cleanup_stale_hls` 每 60 秒清理超过 600 秒的流
- 长视频播放超过 10 分钟时流可能被清理（但通常用户不会等 10 分钟才开始播放）

#### 7. Docker Desktop 无 GPU 加速
- Windows 上 Docker Desktop 无法直接访问 AMD GPU AMF API
- 只能用 libx264 软件编码，性能远低于本地 UV

---

## 八、已完成修复汇总

| 修复项 | 说明 |
|--------|------|
| **路由顺序** | `serve_hls_playlist` 在 `serve_segment` 之前注册，确保 Content-Type 正确 |
| **Content-Type** | Playlist 返回 `application/vnd.apple.mpegurl`（非 octet-stream） |
| **hls_list_size 0** | playlist 保留所有 segment，不会滑动窗口丢弃 |
| **hls_playlist_type event** | FFmpeg EVENT 模式替代默认 LIVE 滑动窗口 |
| **重复 ENDLIST** | 移除 `_monitor_hls` 中的手动 ENDLIST 追加 |
| **绝对 URL segment** | playlist 中 segment 路径改为请求宿主 base URL |
| **最高画质上限** | 设置页面下拉选择 + `set_transcode_setting` 实时生效 |
| **高质量源降级** | 获取用户请求的最高清晰度，FFmpeg scale 滤镜降级 |
| **Windows 防火墙** | 删除 `python.exe` 入站阻止规则，添加端口放行规则 |
| **播放器覆盖层** | 点击播放按钮替代 `autoplay muted`，解除浏览器音频限制 |
| **自定义进度条** | 显示 B站 API 返回的真实总时长 + 已加载编码进度 |
| **FFmpeg 时间戳** | `+genpts+igndts` + `-async 1` |
| **analyzeduration** | 从 10M 减到 5M，加速 FFmpeg 初始化 |
| **自适应等待** | 短视频等完成 → VOD，长视频快速开始 → EVENT |
| **设置页完善** | 添加最高画质上限 UI（Jinja2 + 保存逻辑） |
| **Web面板清理** | help.html / generator.html 移除已废弃参数 |
| **依赖锁** | requirements.txt 添加 starlette<0.46, jinja2<3.2 |
| **OpenWRT 清理** | Docker 容器、镜像、数据完全卸载 |
| **本地 Docker Desktop** | docker-compose 可运行（libx264 软件编码） |
| **本地 UV** | `uv run python main.py` + AMD AMF 编码 ✅ 当前主力 |

---

## 九、部署信息

### 当前活动部署
| 部署方式 | 地址 | 编码器 | 性能 | 状态 |
|----------|------|--------|------|------|
| **本地 UV** | `192.168.5.100:14515/8080` | h264_amf (RX 7900 XTX) | ~81.8x copy | ✅ 运行中 |
| Docker Desktop | localhost:14515/8080 | libx264（软件） | 慢 | ⏸️ 已停止 |
| OpenWRT | - | - | - | ❌ 已完全卸载 |

### 管理命令
```bash
# 停止本地服务
Get-Process python | Where-Object {$_.CommandLine -like "*main.py*"} | Stop-Process -Force

# 启动本地服务（UV）
cd G:\opencodeworkspace\vrchat-bplayer-proxy-main
uv run python main.py

# 查看日志
Get-Content data/proxy.log -Tail 30

# Docker Desktop 启动（如果需要）
cd G:\opencodeworkspace\vrchat-bplayer-proxy-main
docker compose up -d --build
```

### 测试链接
```
# 短视频（2分34秒）
http://192.168.5.100:14515/play?bvid=BV1H1pMzPEKP&qx=1080p&token=thechwinlyu

# 长视频（约1小时）
http://192.168.5.100:14515/play?bvid=BV1pFQABrEeT&qx=1080p&token=thechwinlyu

# API 健康检查
http://192.168.5.100:14515/health

# Web 设置面板
http://192.168.5.100:8080/settings
```

### 文件清单（核心文件）
| 文件 | 行数 | 说明 |
|------|------|------|
| `api/routes.py` | ~1259 | 核心 API + HLS 流管理 + 播放器页面 |
| `utils/codec_adapter.py` | ~434 | 编码器探测/验证/命令构建 |
| `bilibili/video.py` | ~478 | B站 DASH URL 解析 |
| `bilibili/auth.py` | ~223 | 扫码登录 + Cookie 管理 |
| `web/routes.py` | ~534 | Web 面板路由 |
| `config.py` | ~148 | 全局配置 |
| `main.py` | ~124 | 双服务启动 |
| `templates/settings.html` | ~280 | 设置页面 |
| `templates/help.html` | ~290 | API 文档 |
| `templates/generator.html` | ~340 | URL 生成器 |
| `templates/dashboard.html` | ~340 | 仪表盘 |
| `Dockerfile.openwrt` | ~57 | Docker 镜像（含 VA-API） |
| `docker-compose.yml` | ~18 | Docker Compose 配置 |

---

## 十、B站 API 可用性

| 功能 | API | 参数 | 状态 |
|------|-----|------|------|
| 视频信息 | `web-interface/view` | bvid | ✅ |
| DASH 播放地址 | `wbi/playurl` | bvid+cid+qn+fnval=2128 | ✅ |
| WBI 签名 | — | — | ✅ |
| QR 扫码登录 | `passport-login/web/qrcode/generate` | — | ✅ |
| 扫码轮询 | `passport-login/web/qrcode/poll` | qrcode_key | ✅ |
| Cookie 有效期检测 | `web-interface/nav` | — | ✅ (6小时轮询) |
| 剧集/番剧信息 | `pgc/view/web/season` | season_id/ep_id | ✅ |
| Hi-Res 音质 | — | — | 已删除（VRChat 不需要） |
| Dolby 音质 | — | — | 已删除 |

---

## 十一、环境依赖

### 本地开发机
- **OS**: Windows 10/11
- **Python**: 3.14（UV 管理）
- **FFmpeg**: 8.1-full_build（Scoop, gyan.dev）
- **GPU**: AMD Radeon RX 7900 XTX
- **编码器**: h264_amf（已验证）/ libx264（兜底）
- **包管理**: UV 0.11.7

### pip 依赖
```
fastapi>=0.100
uvicorn[standard]>=0.23
httpx>=0.24
qrcode>=7.4
pillow>=10.0
jinja2>=3.1,<3.2
aiosqlite>=0.19
python-multipart>=0.0.6
psutil>=5.9
starlette>=0.36,<0.46
```

