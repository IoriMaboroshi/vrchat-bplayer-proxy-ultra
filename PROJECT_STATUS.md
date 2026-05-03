# VRChat BPlayer Proxy — 项目现状

## 基本信息

| 项目 | 值 |
|------|-----|
| 名称 | VRChat BPlayer Proxy |
| 用途 | B 站视频代理，为 VRChat/PotPlayer/浏览器提供实时视频流 |
| 语言 | Python 3.9（FastAPI + uvicorn） |
| 本地路径 | `C:\Users\TheCh\OneDrive\OpenCodeSpace\bilibili-proxy` |
| 版本 | v1.1.0（开发中，未稳定） |

## 目录结构

```
bilibili-proxy/
├── main.py              # FastAPI 入口（双端口：API + Web，含Cookie自动检测）
├── config.py            # 配置中心（质量映射、动态设置）
├── bilibili/
│   ├── video.py         # B 站 API：视频信息、播放地址、分P、番剧
│   │   └── 修改：AVC/H.264 (codecid=7) 优先于 HEVC (codecid=12) 选取
│   ├── auth.py          # QR 扫码登录 + Cookie 管理 + 自动过期检测
│   │   └── 修改：新增 check_cookie_valid() + _cookie_expiry_estimate()
│   └── wbi.py           # WBI 签名算法
├── api/
│   └── routes.py        # API 端点——大量修改
├── web/
│   └── routes.py        # Web 面板路由：登录/控制台/设置/生成器
├── templates/
│   ├── dashboard.html   # 控制台（Chart.js 统计图表）
│   ├── settings.html    # 设置页面
│   ├── generator.html   # URL 生成器（Jinja2 模板, 含 api_token）
│   ├── stats.html       # 调用统计详情
│   ├── help.html        # API 文档
│   │   └── 修改：更新了所有参数、端点、format 对比表
│   ├── web_login.html   # Web 登录页
│   └── login.html       # B 站扫码登录页
├── db/
│   ├── database.py      # SQLite 初始化
│   └── models.py        # 数据库查询
├── utils/
│   ├── middleware.py     # Token 验证 + Session 管理
│   ├── geo.py           # IP 属地查询
│   │   └── 修改：多源 fallback 链（ip.sb → ip-api → ipip → httpbin）
│   └── bandwidth.py     # 带宽自动降级控制
├── backup.sh            # 数据库每日备份脚本
├── requirements.txt     # 依赖（含 psutil）
└── data/
    ├── cookies.json     # B 站 Cookie（.gitignore 建议添加）
    ├── bilibili_proxy.db # 数据库（.gitignore 建议添加）
    ├── proxy.log        # 应用日志（.gitignore 建议添加）
    └── backups/         # 数据库备份目录（.gitignore 建议添加）
```

## API 端点

| 端点 | 参数 | 说明 |
|------|------|------|
| `/play` | bvid, ep_id, qx, format, token, page, cid, t, enable_hdr/hires, allow_4k/8k, dis | 视频播放 |
| `/info` | bvid, token | 视频信息 |
| `/pages` | bvid, token | 分P列表 |
| `/episode` | ep_id, token | 番剧剧集信息 |
| `/season` | season_id, token | 番剧整季信息 |
| `/qualities` | bvid, token | 可用清晰度 |
| `/health` | — | 健康检查 + 系统指标 + Cookie状态 |
| `/api/token-info` | — | 当前 API Token 信息 |
| `/api/stream-status/{id}` | — | 编码进度 |
| `/segments/{id}/{file}` | — | HLS 分段文件（暂保留但未使用） |
| `/video-summary` | bvid/ep_id, token | 视频摘要（URL 生成器用） |

## 播放参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `bvid` | — | B 站 BV 号 |
| `ep_id` | — | 番剧剧集 ID |
| `qx` | `1080p` | 清晰度：360p/480p/720p/1080p/1080p+/1080p60/4k/hdr/8k |
| `format` | `fmp4` | 输出格式：`fmp4`（流式）/ `mp4`（完整文件）（M3U8 已移除） |
| `dis` | `no` | 调试模式：`yes` 显示 HTML 调试面板，`no` 直接返回视频流 |
| `token` | — | API Token |
| `page` | — | 分P编号 |
| `t` | — | 起始时间（秒） |
| `enable_hdr` | 0 | 启用 HDR |
| `enable_hires` | 1 | 启用 Hi-Res 无损音质 |
| `allow_4k` | 1 | 允许 4K |
| `allow_8k` | 0 | 允许 8K |

## 调试面板

`dis=yes` 参数返回一个 HTML 页面，包含：
- `<video>` 播放器（指向当前视频的流）
- 标题、BV号、时长、清晰度、格式
- DASH 编码详情表（qn, codecid, 编码名称, 分辨率, 码率, codecs）
- 编码兼容性说明（AVC vs HEVC vs AV1）

示例：`/play?bvid=BV1GJ411x7h7&token=YOUR_TOKEN&dis=yes`

## 当前状态：各文件修改汇总

### `api/routes.py` — 大量修改

**已添加/修改的功能：**
- `dis=yes` 调试页面（HTML + 视频播放器 + DASH 编码表）
- Range 206 响应（`Content-Range` 头）
- `X-Content-Duration` 响应头
- `Accept-Ranges: bytes` 响应头
- nginx 缓冲已关闭的假设（需确认部署环境配置）
- 无并发限制（用户要求）
- 无 M3U8 格式（已移除整个分支）
- `/api/token-info` 和 `/api/stream-status/{id}` 端点

**已知问题：**
1. fMP4 流首次数据到达需要 **10-15 秒**（FFmpeg 连接 B站 CDN 的延迟）
2. 阻塞式 `process.stdout.read(65536)` 在 stream() 中进行，阻塞期间事件循环无法处理其他请求
3. `asyncio.run_in_executor` 替代方案不可靠（lambda 版本有时返回 0 字节）

**遗留的未解决问题：**
- 浏览器/播放器点击播放后需要等待 10-15 秒才能开始播放
- 这不是"秒开"

### `bilibili/video.py` — 小修改

- `get_play_url()` 和 `get_play_url_comprehensive()` 中的视频流选择：
  - 优先选取 `codecid=7`（AVC/H.264）的流
  - 如果没有 AVC 流，再取列表第一个
- 这样浏览器可以原生播放（Chrome/Firefox 不支持 HEVC）

### `bilibili/auth.py` — 小修改

- 新增 `check_cookie_valid()`：调用 B站 `/x/web-interface/nav` 端点验证 Cookie
- 新增 `_cookie_expiry_estimate()`：返回 Cookie 过期状态字符串
- `/health` 端点使用此信息

### `utils/geo.py` — 完全重写

- 多源 fallback 链：`ip.sb` → `ip-api.com` → `ipip.net` → `httpbin.org`
- 每源 3 秒超时
- 24 小时缓存成功结果，1 小时缓存失败结果

### `main.py` — 小修改

- Cookie 检测后台任务：每 6 小时调用 `check_cookie_valid()`
- Python logging 配置（输出到 stdout + `data/proxy.log`）

### `templates/help.html` — 更新

- 添加所有参数（`format`, `ep_id`, `t`, `dis` 等）
- format 对比表
- Range 和 206 支持文档
- `/api/token-info` 和 `/api/stream-status/{id}` 文档

## 代码中涉及编码的坑

### B站 DASH 流编码（codecid 含义）

| codecid | 编码 | 兼容性 |
|---------|------|--------|
| 7 | AVC/H.264 | 所有浏览器和播放器 ✅ |
| 12 | HEVC/H.265 | ❌ Chrome/Firefox 不支持 |
| 13 | AV1 | Chrome/Firefox 支持，需硬件解码 |

当前 `video.py` 已优化：优先选 codecid=7。

### FFmpeg 版本依赖

目标 FFmpeg 版本：5.x
禁止使用 `-max_muxing_queue_size` 作为全局选项——在 FFmpeg 5.1.8 上导致输出 0 字节。

## 当前核心瓶颈：fMP4 首次数据延迟

### 现象
- 服务返回 200 OK 响应头是即时的
- 但第一个视频数据块（ftyp + moov）到达需要 10-15 秒
- 之后后续数据持续流式输出，速度正常

### 原因
- `process.stdout.read(65536)` 是**同步阻塞调用**
- FFmpeg 需要联网访问 B站 CDN + 初始化转封装管线
- 这 10-15 秒的阻塞发生在 uvicorn 的 asyncio 事件循环中
- 阻塞期间不仅视频数据不送达，其他 HTTP 请求也无法处理

### 已尝试的方案
1. **`loop.run_in_executor(None, lambda: process.stdout.read(65536))`**
   - 有时返回 2.7MB/12s ✅
   - 有时返回 0 字节 ❌
   - 不稳定，原因不明

2. **预缓冲首字节（`first_chunk = process.stdout.read(65536)`）**
   - 导致 HTTP 响应头需要 10-15 秒才发出
   - 浏览器超时，完全看不到视频播放器 ❌

### 推荐的修复方向
- **使用 `asyncio.to_thread`**：Python 3.9 原生支持，替代 `run_in_executor`
- **或使用 `asyncio.create_subprocess_exec`**：asyncio 原生子进程 API，支持真正的非阻塞管道读写
- **或使用线程池**：单独的线程读取 FFmpeg stdout，`asyncio.Queue` 桥接

## 已知但未修复的问题

1. **fMP4 首次数据延迟 10-15 秒**（核心问题，见上）
2. **`run_in_executor` 不稳定**：有时返回 0 字节，有时正常工作
3. **FFmpeg 无错误日志**：`stderr=subprocess.DEVNULL` 导致 FFmpeg 失败时难以排查
4. **M3U8 格式已移除**（用户要求），但 `/segments` 端点和相关代码残留
5. **并发请求支持有限**：同步 `process.stdout.read()` 阻塞事件循环，多用户同时请求时性能差
6. **调试页面 DASH 编码表空白**：`dis=yes` 页面的编码详情表因 try/except 静默失败，通常为空

## 写给接手的人

### 启动方式
```bash
pip install -r requirements.txt
python main.py
```

### 并发两端口
- API: `0.0.0.0:14515`（通过 nginx 反向代理对外暴露）
- Web: `0.0.0.0:8080`（管理面板）

### 后端 API Token
默认 `thechwinlyu`，通过 `config.py` 中的 `API_TOKEN` 设置，也可通过环境变量 `API_TOKEN` 覆盖。

### 关键文件
- `api/routes.py`：核心逻辑，`/play` 端点的 fMP4 流实现在 `async def stream()` 中
- `bilibili/video.py`：B站 API 调用 + 视频流选取（AVC 优先）
- `config.py`：编码映射、动态设置、FFmpeg 路径
- 部署到 nginx 时需要 `proxy_buffering off`，否则流无法实时送达

### 要修改的核心代码位置
`api/routes.py` 中的 `async def stream()` (约 654 行)：
```python
async def stream():
    try:
        while True:
            chunk = process.stdout.read(65536)  # <-- 这行是瓶颈
            if not chunk:
                break
            yield chunk
            await asyncio.sleep(0)
    finally:
        process.kill()
```
需要把 `process.stdout.read(65536)` 改为非阻塞调用。

### 推荐的完整测试清单
1. `curl -s "http://YOUR_HOST:14515/play?bvid=BV1GJ411x7h7&token=thechwinlyu&format=fmp4&qx=1080p&dis=yes"` → 查看调试面板
2. `curl -s -o /tmp/test.mp4 -w 'SIZE=%{size_download} TIME=%{time_total}s' --max-time 20 "http://YOUR_HOST:14515/play?bvid=BV1GJ411x7h7&token=thechwinlyu"` → 验证下载
3. `ffprobe /tmp/test.mp4` → 验证视频有效
4. 浏览器直接打开 fMP4 链接 → 验证播放
5. PotPlayer/VLC 直接打开 → 验证桌面播放器兼容
