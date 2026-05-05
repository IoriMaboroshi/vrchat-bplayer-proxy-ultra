> ⚠️ DISCLAIMER: This software is provided for EDUCATIONAL PURPOSES ONLY.
> Users are solely responsible for compliance with all applicable laws,
> third-party terms of service, and content usage rights.
> The developers assume NO liability for any unauthorized use.

# VRCBPP — VRChat BPlayer Proxy Ultra

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-00a393)](https://fastapi.tiangolo.com)
[![License MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **Looking for better playback quality and multi-viewer support?**
>
> Check out **[VRChat Media Pipeline](https://github.com/IoriMaboroshi/vrchat-media-pipeline)** — the successor project that preprocesses media locally with GPU acceleration and pushes to a remote server for static HLS serving. It offers VOD playback with seek support, persistent task tracking, and a full pipeline management dashboard. If you have a powerful local GPU and want the best playback experience, Media Pipeline is the recommended choice.
>
> This project (BPlayer Proxy Ultra) remains available for users who prefer real-time proxy streaming with minimal local processing.

A high-performance DASH-to-HLS real-time transcoding proxy designed for Unity-based video players (VRChat, PotPlayer, VLC). Converts separate audio/video DASH streams into a single playable stream with hardware-accelerated transcoding.

**Ultra version** adds multi-thread download, smart caching, bandwidth auto-downgrade, and a full web management dashboard.

**No Affiliation:** This project is not affiliated with, endorsed by, or connected to any streaming platform. All trademarks and platform names are the property of their respective owners.

---

## Architecture

```
                          Public Internet
                                |
                    +-----------v-----------+
                    |   aliWH1:14513/14514  |
                    |  (nginx reverse proxy |
                    |   + SSL termination)  |
                    +-----------+-----------+
                                |
                          :14515 (nginx)
                                |
                    +-----------v-----------+
                    |   frp tunnel          |
                    |   remote :14516       |
                    +-----------+-----------+
                                |
                    +-----------v-----------+
                    |   localhost:14515      |
                    |   VRCBPP API Server    |
                    |   (FastAPI + uvicorn)  |
                    +-----------+-----------+
                                |
              +-----------------+-----------------+
              |                                   |
    +---------v---------+               +---------v---------+
    |   Web Dashboard   |               |   FFmpeg + aria2  |
    |   :8080           |               |   Transcoding     |
    +-------------------+               +-------------------+
```

**Dual-port design:**
- **API Server** (`:14515`) — Streaming endpoints, auth-protected
- **Web Dashboard** (`:8080`) — Settings, stats, URL generator, preload manager

---

## Features

### Core Transcoding
- DASH to HLS/fMP4 real-time transcoding via FFmpeg
- Automatic AVC/H.264 stream selection for browser compatibility
- Smart quality fallback (best available to lowest)
- fMP4 streaming with Range/206 partial content support
- Seeking support via `t` parameter (start offset in seconds)
- Debug mode (`dis=yes`) with HTML player and DASH encoding table

### Multi-Thread Download
- aria2c-powered DASH preloading (32 threads default)
- Adjustable connection count (1-64) via API or dashboard
- 12-hour TTL smart caching with key-based management

### Playback Modes
- **Standard** — Direct fMP4 streaming, instant headers
- **Debug** — `dis=yes` returns HTML page with embedded player
- **Raw** — `raw=1` forces m3u8 redirect for compatible players

### Web Panel
- Real-time stats dashboard with Chart.js graphs
- URL generator with quality presets
- Preload manager with cache status visualization
- Settings editor with hot-reload
- Account authentication
- Full API documentation page

### Access Control
- Token-based API authentication (URL parameter)
- Separate web panel login (username + password)
- Cookie-based web session management
- Role-level access for upstream platform integration

---

## API Endpoint Reference

All endpoints require `?token=YOUR_TOKEN` unless noted. Default token: `your_token`.

### Complete Endpoint Table

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/play` | Required | Transcode and stream video (fMP4 output) |
| `GET` | `/info` | Required | Video metadata JSON (title, uploader, stats) |
| `GET` | `/pages` | Required | Multi-P video part list |
| `GET` | `/qualities` | Required | Available quality options |
| `GET` | `/season` | Required | Season info by `season_id` |
| `GET` | `/episode` | Required | Episode info by `episode_id` |
| `GET` | `/health` | **None** | Health check, auth state, system metrics |
| `POST` | `/api/preload` | Required | Preload video into local cache (12h TTL) |
| `GET` | `/api/cache-stats` | Required | List cached entries (title, size, expiry) |
| `DELETE` | `/api/cache/{key}` | Required | Delete specific cache entry |
| `GET` | `/api/aria2-settings` | Required | View aria2 connection count |
| `POST` | `/api/aria2-settings` | Required | Update aria2 connections (1-64) |
| `GET` | `/api/token-info` | Required | Get masked API token for URL generator |
| `GET` | `/api/stream-status/{stream_id}` | Required | Get fMP4 encoding progress |

---

### `/play` — Full Parameter Reference

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `id` | string | Yes* | — | Content identifier string (e.g. `example_content_id`) |
| `episode_id` | string | Yes* | — | Episode/series identifier. Alternative to `id`. |
| `token` | string | Yes | — | API authentication token |
| `qx` | string | No | `1080p` | Quality preset (see quality table below) |
| `cid` | int | No | — | Specific part identifier (bypasses page lookup) |
| `page` | int | No | — | Part number (1-indexed) |
| `t` | int | No | — | Start time offset in seconds |
| `raw` | int | No | `0` | Force m3u8 redirect for players that do not support direct fMP4 |
| `dis` | string | No | `no` | Debug mode: `yes` shows HTML player and encoding info |
| `format` | string | No | `fmp4` | Output format: `fmp4` (streaming) or `mp4` (complete file) |

*\* Either `id` or `episode_id` is required.*

### Quality Mapping

| `qx` Value | Display Quality | Requirement |
|-----------|----------------|-------------|
| `4k` | 4K Ultra HD | Platform-dependent |
| `1080p60` | 1080P 60fps | Platform-dependent |
| `1080p` | 1080P HD | Platform-dependent |
| `720p` | 720P HD | — |
| `480p` | 480P | — |
| `360p` | 360P | — |

If the requested quality is unavailable, the server auto-falls back from best to worst.

### `/play` — Response Headers

| Header | Description |
|--------|-------------|
| `X-Content-Duration` | Video duration in seconds |
| `X-Content-Title` | URL-encoded video title |
| `X-Stream-Id` | Stream ID for encoding status queries |
| `Content-Range` | Range response support (206 Partial Content) |
| `Accept-Ranges` | Always `bytes` |

### `/health` — Response Example

```json
{
  "status": "ok",
  "platform_authenticated": true,
  "service": "vrchat-bplayer-proxy",
  "cookie": {
    "logged_in": true,
    "expires_in": "valid"
  },
  "system": {
    "active_streams": 2,
    "encoder": "AMD AMF",
    "cpu_percent": 45.2,
    "memory_percent": 62.1,
    "disk_percent": 33.0,
    "uptime_seconds": 123456
  }
}
```

### Stream Status Values

| Status | Description |
|--------|-------------|
| `processing` | FFmpeg is actively encoding |
| `completed` | Encoding finished |
| `not_found` | Stream ID expired or invalid |

### HTTP Status Codes

| Code | Meaning |
|------|---------|
| `200` | Success |
| `206` | Partial Content (Range request) |
| `400` | Bad request (invalid content identifier, video unavailable) |
| `403` | Token missing or invalid |
| `503` | Platform authentication required or server busy |

---

## Quick Start

### Requirements

- Python 3.9+
- FFmpeg (any build with desired hardware encoders)
- aria2c (optional, for multi-threaded download)
- GPU drivers for hardware transcoding (optional)

### Install and Run

```bash
# Clone the repository
git clone <repo-url> vrchat-bplayer-proxy
cd vrchat-bplayer-proxy

# Create virtual environment
uv venv
uv pip install -r requirements.txt

# Start server
uv run python main.py

# Access:
# API:      http://localhost:14515
# Dashboard: http://localhost:8080
```

### Verify

```bash
# Health check (no token required)
curl http://localhost:14515/health

# Stream a video
curl -o test.mp4 "http://localhost:14515/play?id=example_content_id&token=your_token&qx=1080p"

# Debug mode with HTML player
curl "http://localhost:14515/play?id=example_content_id&token=your_token&dis=yes"
```

### Platform Authentication

1. Open `http://localhost:8080` in a browser
2. Log in via the account authentication page
3. Confirm auth status via the `/health` endpoint

---

## Configuration

Edit `config.py` or use the Web Dashboard at `/settings`. Settings changed via the dashboard are hot-reloaded (some port changes require restart).

### All Config Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `API_HOST` | env | `0.0.0.0` | API bind address |
| `API_PORT` | env/dynamic | `14515` | API server port (dynamic changes require restart) |
| `WEB_HOST` | env | `0.0.0.0` | Web dashboard bind address |
| `WEB_PORT` | env/dynamic | `8080` | Web dashboard port (restart required) |
| `API_TOKEN` | env/dynamic | `your_token` | API auth token |
| `FFMPEG_PATH` | env/dynamic | `ffmpeg` | FFmpeg binary path |
| `ARIA2_CONNECTIONS` | static/dynamic | `32` | aria2 download threads (1-64) |
| `DEFAULT_QX` | static/dynamic | `1080p` | Default quality preset |
| `QUALITY_MAP` | static | `{4k:120, 1080p60:112, ...}` | Quality name to quality index mapping |
| `QUALITY_FALLBACK_ORDER` | static | `[120,112,64,32,16,8]` | Quality fallback chain |
| `WEB_USERNAME` | static/dynamic | `admin` | Web login username |
| `WEB_PASSWORD_HASH` | static | sha256(`password`) | Web login password hash |
| `UPSTREAM_UA` | static | Chrome 131 UA | User-agent for upstream API requests |
| `enable_web_auth` | dynamic | `1` | Enable web panel login |
| `log_retention_days` | dynamic | `30` | Log retention period |
| `cookie_refresh_interval` | dynamic | `24` | Cookie refresh interval (hours) |
| `public_base_url` | dynamic | `""` | Public URL for reverse proxy setups |

Dynamic settings are stored in the SQLite DB and hot-reloaded at runtime. Edit via the Web Dashboard `/settings` page.

### Quality Fallback Chain

```python
QUALITY_FALLBACK_ORDER = [120, 112, 64, 32, 16, 8]
# Maps to: 4k, 1080p60, 1080p, 720p, 480p, 360p
```

---

## Hardware Encoder Matrix

| Encoder | Flag | GPU Required | Drivers | Image Quality | Throughput | Notes |
|---------|------|-------------|---------|---------------|------------|-------|
| **AMD AMF** | `amf` | AMD RX 500 series+, Vega, RDNA 1/2/3 | AMD Adrenalin / ROCm | Good | High | Best on Linux with ROCm |
| **NVIDIA NVENC** | `nvenc` | NVIDIA GTX 900 series+, all RTX | NVIDIA Driver + CUDA | Excellent | Very High | Two encoders on RTX 4090 |
| **Intel QSV** | `qsv` | Intel HD Graphics 4000+, UHD, Iris Xe, Arc | Intel Media SDK / VA-API | Good | Medium | Good for low-power deployments |
| **VA-API** | `vaapi` | Any VA-API compatible GPU | Mesa / Intel VA-API | Fair | Low | Generic fallback, Linux only |
| **Software** | `libx264` | None (CPU only) | None | Excellent | Low | Universal fallback, uses only CPU |

### Docker Variants

| Image Tag | Encoder | Base |
|-----------|---------|------|
| `base` | Software (libx264) | python:3.11-slim |
| `nvidia` | NVENC | python:3.11-slim + CUDA |
| `intel` | QSV | python:3.11-slim + Intel Media SDK |
| `openwrt` | Software | OpenWRT base |

---

## Deployment Guide

### Docker

Build using `Dockerfile` (base/nvidia/intel multi-target) or `Dockerfile.openwrt`:

```bash
# Build
docker build -t vrcbpp .

# Run (software encoding)
docker run -d --name vrcbpp -p 14515:14515 -p 8080:8080 \
  -v $(pwd)/data:/app/data -e API_TOKEN=your_token vrcbpp

# NVENC variant (requires nvidia-container-toolkit)
docker build -t vrcbpp:nvidia --target nvidia .
docker run -d --name vrcbpp --gpus all -p 14515:14515 -p 8080:8080 \
  -v $(pwd)/data:/app/data -e API_TOKEN=your_token vrcbpp:nvidia

# Intel QSV variant
docker build -t vrcbpp:intel --target intel .
docker run -d --name vrcbpp --device /dev/dri:/dev/dri \
  -p 14515:14515 -p 8080:8080 -v $(pwd)/data:/app/data vrcbpp:intel
```

### Docker Compose

```bash
docker compose up -d
```

See `docker-compose.yml` for the full service definition (builds from `Dockerfile.openwrt`, maps ports 14515+8080, persists `./data` volume).

### systemd (Linux)

```bash
# Copy the provided systemd service file, then:
sudo cp vrcbpp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vrcbpp
```

See the included `.service` file for the full unit definition. Environment variables (`API_TOKEN`, `FFMPEG_PATH`) are set in the `[Service]` section.

### Nginx Reverse Proxy

**Critical: `proxy_buffering off` is required** for real-time streaming.

```nginx
server {
    listen 14513;
    server_name vrchat.example.com;

    location / {
        proxy_pass http://127.0.0.1:14515;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;              # REQUIRED for streaming
        proxy_request_buffering off;
        proxy_max_temp_file_size 0;
        proxy_connect_timeout 30s;
        proxy_read_timeout 3600s;         # Long timeout for video
        proxy_send_timeout 3600s;
        add_header Access-Control-Allow-Origin * always;
    }
}

# HTTPS (:14514) variant adds ssl_certificate, ssl_protocols TLSv1.2 TLSv1.3
```

Set `public_base_url` (in dynamic settings) to the public URL (e.g. `https://vrchat.example.com:14514`) for correct URL generation in the dashboard.

### frp Tunnel (Remote Server to Local)

The production deployment uses frp to tunnel traffic from a remote server to a local instance:

```
aliWH1:14515 (nginx) -> frp server :14516 -> frp client -> localhost:14515 (VRCBPP)
```

Configure `frpc.ini` on the local machine to forward `:14516` on the remote server to `localhost:14515`.

### Windows Deployment

Batch scripts are located in `G:\VRCBPP\`:

| Script | Description |
|--------|-------------|
| `start.bat` | Launches the server with uv |
| `stop.bat` | Stops the running server |
| `status.bat` | Checks server status and port availability |

```batch
:: Install dependencies
uv venv
uv pip install -r requirements.txt

:: Start
start.bat

:: Or manually
uv run python main.py
```

On Windows, FFmpeg must be available in `PATH` or configured via the `ffmpeg_path` setting in the web dashboard.

---

## Bandwidth Optimization

### Auto-Downgrade

The server includes automatic bandwidth-based quality downgrade (see `utils/bandwidth.py`):

- Monitors real-time throughput during streaming
- Automatically falls back to a lower quality if bandwidth is insufficient
- Prevents buffering and stream interruption

### Multi-Thread Download

- aria2c downloads DASH streams using up to 64 parallel connections
- Adjust via API: `POST /api/aria2-settings {"connections": N}`
- Higher thread count means faster preload but higher network load
- Default: 32 threads

### Smart Caching

- DASH segments are cached locally with 12-hour TTL
- Cached videos play instantly from disk, no upstream CDN re-download
- Manage cache via dashboard or API (`/api/preload`, `/api/cache-stats`, `/api/cache/{key}`)

### Nginx Streaming Tuning

```nginx
# These are essential for real-time streaming:
proxy_buffering off;
proxy_request_buffering off;
proxy_max_temp_file_size 0;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
```

---

## Audio/Video Sync

The server handles DASH streams which deliver audio and video as separate tracks. These are merged by FFmpeg in real-time:

- **DASH demux** — FFmpeg reads separate audio/video streams from upstream CDN
- **Real-time mux** — Streams are muxed into a single fMP4 output
- **AVC preferred** — H.264 (codecid=7) streams are automatically selected for maximum player compatibility (Chrome/Firefox do not support HEVC)
- **Codec fallback** — If no H.264 stream is available, the first available stream is used

### Known Limitation

The initial fMP4 data arrival takes 10-15 seconds due to FFmpeg's synchronous stdout read blocking the event loop. The HTTP response headers arrive immediately, but video data starts after FFmpeg finishes connecting to the upstream CDN and initializing the transcoding pipeline. This is the primary performance bottleneck in the current release.

---

## Web Dashboard

Access the dashboard at `http://localhost:8080` (default credentials: `admin` / `password`).

| Page | Route | Description |
|------|-------|-------------|
| **Login** | `/` | Account authentication |
| **Console** | `/dashboard` | Real-time stats with Chart.js (active streams, CPU, memory, disk, uptime) |
| **Stats** | `/stats` | Detailed API call statistics |
| **Generator** | `/generator` | URL generator with quality and format presets |
| **Preload** | `/preload` | Cache manager: preload videos, view cached entries, delete cache |
| **Settings** | `/settings` | Hot-reloadable server configuration |
| **API Docs** | `/help` | Full API documentation page |
| **Web Login** | `/web_login` | Web panel authentication |

---

## License

MIT License — see [LICENSE](LICENSE) file.

---

## Disclaimers

> ⚠️ This project is for **educational purposes only**.

Users are solely responsible for complying with all applicable laws, regulations, and third-party terms of service. The project does not host, store, distribute, or make available any copyrighted content. It functions solely as a transcoding proxy for publicly accessible media streams.

**No Affiliation:** This project is not affiliated with, endorsed by, or connected to any streaming platform. All trademarks, service marks, and platform names are the property of their respective owners.

**No Liability:** Use this software at your own risk. The authors and contributors assume no liability for any misuse, damages, legal claims, or losses arising from the use of this software. You are responsible for ensuring your use complies with all applicable laws and the terms of service of any third-party services you access through this proxy.
