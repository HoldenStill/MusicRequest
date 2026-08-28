# MusicRequest

A self-hosted web UI for searching and downloading music albums via [streamrip](https://github.com/nathom/streamrip). Audio files are saved directly to the server's storage — nothing is ever sent to the browser.

![Dark-themed album search interface with glassmorphism design]

---

## Quick Start (Docker)

```bash
# Build
docker build -t musicrequest .

# Run
docker run -d \
  --name musicrequest \
  -p 8080:8080 \
  -v /path/to/your/music:/music \
  -v /path/to/your/config:/config \
  -e APP_PASSCODE=1099 \
  musicrequest
```

Then open `http://localhost:8080` and enter the passcode.

---

## Phase 2: Deployment Guide

### Step 1 — Streamrip `config.toml` Setup

Streamrip needs a `config.toml` with your Qobuz credentials. Create it on your host machine:

#### 1a. Generate a default config

The easiest way is to run streamrip once to generate the default config, then edit it:

```bash
# Run a temporary container to generate the default config
docker run --rm -v /mnt/pool/apps/musicrequest/config:/config musicrequest \
  bash -c "rip --config-path /config/config.toml config list"
```

This creates `/mnt/pool/apps/musicrequest/config/config.toml` with all default values.

#### 1b. Configure Qobuz credentials

Edit the `config.toml` file:

```bash
nano /mnt/pool/apps/musicrequest/config/config.toml
```

Update the `[qobuz]` section:

```toml
[qobuz]
quality = 3                          # 1: 320kbps MP3, 2: 16/44.1, 3: 24/≤96, 4: 24/≥96
download_booklets = true

use_auth_token = false
email_or_userid = "your-qobuz-email@example.com"
# MD5 hash of your Qobuz password:
password_or_token = "your_md5_hashed_password"

# Leave these empty — streamrip will auto-populate on first use
app_id = ""
secrets = []
```

**To get the MD5 hash of your password:**

```bash
echo -n "YourActualPassword" | md5sum
```

Copy the hash (without the trailing ` -`) into `password_or_token`.

#### 1c. Set the download folder

In the `[downloads]` section, set the folder to match the container mount point:

```toml
[downloads]
folder = "/music"
```

#### 1d. First-time Qobuz authentication

Run the container interactively to trigger the initial Qobuz login (this populates `app_id` and `secrets`):

```bash
docker run --rm -it \
  -v /mnt/pool/apps/musicrequest/config:/config \
  musicrequest \
  rip --config-path /config/config.toml search qobuz album "test"
```

This will authenticate with Qobuz and save the session tokens back to `config.toml`.

---

### Step 2 — TrueNAS SCALE Custom App Deployment

#### 2a. Prepare Host Paths

Create two datasets on your TrueNAS pool:

| Dataset | Purpose | Example Path |
|---------|---------|-------------|
| Config | `config.toml` storage | `/mnt/pool/apps/musicrequest/config` |
| Music | Downloaded music library | `/mnt/pool/media/music` |

Set permissions to UID/GID **568** (the TrueNAS `apps` user):

```bash
# SSH into TrueNAS
sudo chown -R 568:568 /mnt/pool/apps/musicrequest/config
sudo chown -R 568:568 /mnt/pool/media/music
```

#### 2b. Deploy as Custom App

1. In TrueNAS SCALE, go to **Apps → Discover → Custom App**
2. Configure:

| Setting | Value |
|---------|-------|
| **Application Name** | `musicrequest` |
| **Image Repository** | `musicrequest` (or your registry path, e.g., `ghcr.io/youruser/musicrequest`) |
| **Image Tag** | `latest` |

3. **Container Environment Variables:**

| Variable | Value |
|----------|-------|
| `APP_PASSCODE` | Your chosen passcode (default: `1099`) |
| `STREAMRIP_CONFIG_PATH` | `/config/config.toml` |
| `MUSIC_DIR` | `/music` |
| `SEARCH_LIMIT` | `10` |

4. **Port Forwarding:**

| Container Port | Node Port | Protocol |
|---------------|-----------|----------|
| `8080` | `8080` (or your preferred port) | TCP |

5. **Host Path Volumes:**

| Host Path | Mount Path | Description |
|-----------|-----------|-------------|
| `/mnt/pool/apps/musicrequest/config` | `/config` | Streamrip configuration |
| `/mnt/pool/media/music` | `/music` | Music download destination |

6. **Security Context:**
   - Run as User: `568`
   - Run as Group: `568`

7. Click **Install** and wait for the container to start.

#### 2c. Verify

Open `http://<truenas-ip>:8080` in your browser. You should see the passcode lock screen.

---

### Step 3 — Nginx Proxy Manager (Reverse Proxy + HTTPS)

If you're exposing MusicRequest to the internet, use Nginx Proxy Manager for HTTPS and additional access control.

#### 3a. Add Proxy Host

1. Open Nginx Proxy Manager (`http://<truenas-ip>:81`)
2. Go to **Proxy Hosts → Add Proxy Host**
3. Configure:

| Setting | Value |
|---------|-------|
| **Domain Names** | `music.yourdomain.com` |
| **Scheme** | `http` |
| **Forward Hostname / IP** | `<truenas-ip>` |
| **Forward Port** | `8080` |
| **Block Common Exploits** | ✅ |
| **Websockets Support** | ✅ (needed for SSE) |

4. **SSL Tab:**
   - Request a new SSL Certificate (Let's Encrypt)
   - Force SSL: ✅
   - HTTP/2 Support: ✅

#### 3b. Add Access List (Optional Extra Layer)

For defense-in-depth (HTTP Basic Auth on top of the app's passcode):

1. Go to **Access Lists → Add Access List**
2. Name: `MusicRequest Auth`
3. **Authorization Tab:** Add a username/password
4. **Access Tab:** Satisfy Any, allow `all`
5. Go back to your Proxy Host → edit → set **Access List** to `MusicRequest Auth`

This gives you two layers of protection:
1. **Nginx Basic Auth** — first barrier at the reverse proxy level
2. **App Passcode** — second barrier within the application itself

#### 3c. Custom Nginx Configuration

Add this to the **Advanced** tab of your Proxy Host to optimize SSE streaming:

```nginx
proxy_buffering off;
proxy_cache off;
proxy_set_header Connection '';
proxy_http_version 1.1;
chunked_transfer_encoding off;
```

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_PASSCODE` | `1099` | Passcode required to access the web UI |
| `STREAMRIP_CONFIG_PATH` | `/config/config.toml` | Path to streamrip configuration file |
| `MUSIC_DIR` | `/music` | Server directory where music is downloaded |
| `SEARCH_LIMIT` | `10` | Maximum number of search results returned |

---

## Architecture

```
Browser (client)          MusicRequest (server)          Qobuz API
    │                           │                           │
    │── GET / ─────────────────▶│                           │
    │◀── login.html ────────────│ (if not authenticated)    │
    │── POST /api/auth ────────▶│ verify passcode           │
    │◀── set session cookie ────│                           │
    │── GET / ─────────────────▶│                           │
    │◀── index.html ────────────│ (authenticated)           │
    │                           │                           │
    │── GET /api/search ───────▶│── album/search ──────────▶│
    │◀── JSON results ──────────│◀── JSON ──────────────────│
    │                           │                           │
    │── POST /api/download ────▶│                           │
    │◀── {job_id, queued} ──────│                           │
    │                           │── rip CLI subprocess ────▶│
    │                           │   (downloads to /music)   │
    │◀── SSE queue updates ─────│                           │
```

---

## License

MIT
