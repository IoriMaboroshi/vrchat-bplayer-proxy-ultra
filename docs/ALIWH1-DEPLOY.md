# aliWH1 — nginx TS Caching Deployment Guide

> **Goal**: Deploy `nginx-aliwh1-cache.conf` on the aliWH1 server so TS segments are cached locally, drastically reducing repeated bandwidth through the frp tunnel.

---

## 1. Prerequisites

| Component | Status | Check Command |
|---|---|---|
| **nginx** ≥ 1.18 installed on aliWH1 | Required | `nginx -v` |
| **frps** running on aliWH1:7000 (control) | Required | `systemctl status frps` or `ps aux \| grep frps` |
| **frps** tunnel on aliWH1:14516 → local machine:14515 | Required | `ss -tlnp \| grep 14516` |
| **local frpc** connected and tunnel healthy | Required | Check frpc logs on local machine |
| **VRCBPP service** running on localhost:14515 | Required | `curl http://127.0.0.1:14515/api/health` |
| SSH access to aliWH1 (`root` or sudo-capable user) | Required | — |

**If frps is not yet deployed on aliWH1**, set it up first:
```bash
# On aliWH1
tar -xzf frp_*.tar.gz
cp frps /usr/local/bin/
# Create /etc/frp/frps.toml with bind_port = 7000
# systemctl enable --now frps
```

---

## 2. Copy the Config to aliWH1

From your local machine, copy `nginx-aliwh1-cache.conf` to aliWH1:

```bash
scp nginx-aliwh1-cache.conf root@8.148.64.28:/etc/nginx/sites-available/vrchat-cache.conf
```

### 2a. Nginx Config Layout (depends on distro)

**Ubuntu / Debian:**
```bash
# If sites-available/sites-enabled exist:
ln -sf /etc/nginx/sites-available/vrchat-cache.conf /etc/nginx/sites-enabled/vrchat-cache.conf

# Ensure no conflicting configs on port 14515
nginx -T 2>&1 | grep "listen 14515"    # should show only this config
```

**CentOS / Alibaba Cloud Linux / RHEL:**
```bash
# These distros use conf.d instead of sites-enabled:
cp nginx-aliwh1-cache.conf /etc/nginx/conf.d/vrchat-cache.conf
```

### 2b. Cache Directory

Create the cache directory with correct permissions:
```bash
mkdir -p /var/cache/nginx/vrchat
chown -R nginx:nginx /var/cache/nginx/vrchat
# or on some distros:
# chown -R www-data:www-data /var/cache/nginx/vrchat
```

Check the user nginx runs as:
```bash
ps aux | grep "nginx: worker" | head -1 | awk '{print $1}'
```

### 2c. Deduplicate `proxy_cache_path`

The `proxy_cache_path` directive can only be defined **once** across all nginx configs. If you already have it in `/etc/nginx/nginx.conf` or another file, remove the duplicate from `nginx-aliwh1-cache.conf`. Search for existing definitions:

```bash
grep -r "proxy_cache_path.*vrchat" /etc/nginx/
```

If found elsewhere, comment out the `proxy_cache_path` block in `nginx-aliwh1-cache.conf`.

---

## 3. Validate & Reload nginx

```bash
# Syntax check (must pass with no errors)
nginx -t

# If OK, reload without dropping connections
systemctl reload nginx

# Verify the server is listening
ss -tlnp | grep 14515
```

Expected output of `nginx -t`:
```
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
```

---

## 4. Verify Cache Behavior

### 4a. First request (MISS)

```bash
# Replace with an actual TS segment URL from your proxy
curl -s -o /dev/null -D - "http://8.148.64.28:14515/path/to/segment-0.ts"
```

Expected response header:
```
X-Proxy-Cache: MISS
```

### 4b. Second request to same URL (HIT)

```bash
curl -s -o /dev/null -D - "http://8.148.64.28:14515/path/to/segment-0.ts"
```

Expected response header:
```
X-Proxy-Cache: HIT
```

### 4c. M3U8 request (never cached)

```bash
curl -s -o /dev/null -D - "http://8.148.64.28:14515/path/to/playlist.m3u8"
```

Response will NOT have `X-Proxy-Cache` set (bypassed), or show `BYPASS`.

### 4d. Check cache usage

```bash
du -sh /var/cache/nginx/vrchat/
ls /var/cache/nginx/vrchat/ | head -20
```

---

## 5. Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `nginx -t` fails: `proxy_cache_path` duplicate | Already defined in another config | Comment out the `proxy_cache_path` block in `nginx-aliwh1-cache.conf` |
| `nginx -t` fails: `unknown directive "proxy_cache_path"` | Missing `ngx_http_proxy_module` | Rebuild nginx with `--with-http_proxy_module` (it's usually included by default) |
| `connect() failed (111: Connection refused)` | frps tunnel on :14516 is not running | `ss -tlnp \| grep 14516` — if empty, restart frps or check tunnel mapping |
| Requests hang / timeout | frp tunnel broken or local proxy down | Check frpc on local machine: `frpc status -c frpc.ini` |
| `X-Proxy-Cache: MISS` on every request | Cache bypassing for all URLs | Check map regex: `grep "vrchat_ts_no_cache" /etc/nginx/sites-enabled/vrchat-cache.conf` |
| Cache never grows | `proxy_cache_valid any 0` blocking non-200, or permissions on `/var/cache/nginx/vrchat` | `chown nginx:nginx /var/cache/nginx/vrchat` |
| `403 Forbidden` | SELinux blocking nginx write to cache dir | `setenforce 0` (temporary) or `chcon -R -t httpd_cache_t /var/cache/nginx/vrchat` |

### Debugging commands

```bash
# View nginx error log in real time
tail -f /var/log/nginx/error.log

# Test a single request with full debug
curl -v "http://127.0.0.1:14515/some-ts-file.ts" 2>&1 | head -50

# Check if frp tunnel is passing traffic
curl -v "http://127.0.0.1:14516/" 2>&1

# View nginx cache status for a specific URL
curl -s -o /dev/null -D - "http://8.148.64.28:14515/path/to/segment.ts" | grep X-Proxy-Cache
```

---

## 6. Firewall / Security Group

aliWH1 is on Alibaba Cloud — security group rules must allow inbound port **14515**:

1. Log in to [Alibaba Cloud ECS Console](https://ecs.console.aliyun.com/)
2. Navigate to **Security Groups** → your aliWH1 instance's security group
3. Add inbound rule:
   - **Protocol**: TCP
   - **Port Range**: `14515/14515`
   - **Source**: `0.0.0.0/0` (public) or specific VRChat user IPs
   - **Description**: `VRChat BPlayer Proxy — nginx TS cache`

Also check the **OS-level firewall** (iptables / firewalld):
```bash
# firewalld (CentOS / Alibaba Cloud Linux)
firewall-cmd --add-port=14515/tcp --permanent
firewall-cmd --reload

# iptables
iptables -A INPUT -p tcp --dport 14515 -j ACCEPT
service iptables save

# ufw (Ubuntu)
ufw allow 14515/tcp
```

> ⚠️ Port **14516** (frp tunnel) should remain internal-only — do NOT open it in the security group.

---

## 7. Rollback

If something goes wrong, revert immediately:

```bash
# Remove or disable the config
rm /etc/nginx/sites-enabled/vrchat-cache.conf
# or
mv /etc/nginx/sites-enabled/vrchat-cache.conf /etc/nginx/sites-enabled/vrchat-cache.conf.disabled

# Reload nginx
systemctl reload nginx
```

---

## Reference: Full Architecture

```
 ┌──────────────────────────────────────────────────────┐
 │  aliWH1 (8.148.64.28)                                │
 │                                                      │
 │  ┌─────────────────────────────────────────────┐     │
 │  │  nginx :14515                                │     │
 │  │  ├─ /var/cache/nginx/vrchat/  (50GB, 12h)   │     │
 │  │  │  ├─ TS segment cache HIT → return         │     │
 │  │  │  └─ TS segment cache MISS ─┐              │     │
 │  │  └─ M3U8 pass-through ────────┤              │     │
 │  └────────────────────────────────┼─────────────┘     │
 │                                   │                   │
 │  ┌────────────────────────────────┼─────────────┐     │
 │  │  frps :7000 (control)         │              │     │
 │  │  frps :14516 (tunnel) ←───────┘              │     │
 │  └──────────────┬───────────────────────────────┘     │
 └─────────────────┼─────────────────────────────────────┘
                   │  frp tunnel
 ┌─────────────────┼─────────────────────────────────────┐
 │  Local Machine   │                                    │
 │  ┌───────────────┴──────────────────────────────┐     │
 │  │  frpc → aliWH1:7000                          │     │
 │  │  tunnel: remote :14516 → localhost:14515     │     │
 │  └──────────────────────────────────────────────┘     │
 │                                                      │
 │  ┌──────────────────────────────────────────────┐     │
 │  │  VRCBPP (Python) :14515                      │     │
 │  │  ├─ HLS transcoding (DASH → M3U8+TS)         │     │
 │  │  ├─ Multi-threaded download (aria2c)          │     │
 │  │  └─ Web dashboard :8080                       │     │
 │  └──────────────────────────────────────────────┘     │
 └──────────────────────────────────────────────────────┘
```
