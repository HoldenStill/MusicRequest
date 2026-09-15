# MusicRequest

[![Built with Gemini](https://img.shields.io/badge/Built%20with-Google%20Gemini-4285F4?style=flat-square&logo=google&logoColor=white)](https://gemini.google.com)

> [!NOTE]
> **AI Disclaimer**: This application and codebase were created almost entirely by Google Gemini agents.

A sleek, self-hosted web application that acts as a front-end for [streamrip](https://github.com/nathom/streamrip), enabling you to easily search, queue, and download high-resolution music albums directly to your server. 
MusicRequest makes it effortless to build your digital library—while ensuring your audio files are saved securely on your host machine without passing through the browser.


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
  -e APP_PASSCODE=your_secure_passcode \
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
docker run --rm -v /path/to/your/config:/config musicrequest \
  bash -c "rip --config-path /config/config.toml config list"
```

This creates `/path/to/your/config/config.toml` with all default values.

#### 1b. Configure Qobuz credentials

Because email/password authentication is often unreliable with Qobuz, it is recommended to use authentication tokens extracted directly from your browser.

Edit the `config.toml` file:

```bash
nano /path/to/your/config/config.toml
```

Update the `[qobuz]` section to enable token authentication:

```toml
[qobuz]
quality = 3                          # 1: 320kbps MP3, 2: 16/44.1, 3: 24/≤96, 4: 24/≥96
download_booklets = true

use_auth_token = true
email_or_userid = "your_user_id"     # Found in Qobuz API requests
password_or_token = "your_x_user_auth_token"

# Leave these empty — streamrip will auto-populate on first use
app_id = ""
secrets = []
```

**To extract your authentication token and User ID:**
1. Log into the Qobuz web player (`play.qobuz.com`) in your web browser.
2. Open your browser's Developer Tools (usually `F12` or right-click -> `Inspect`).
3. Go to the **Network** tab and filter by `api.qobuz.com`.
4. Refresh the page or start playing a song.
5. Click on one of the API requests in the list (e.g., `get` or `getUser`).
6. In the **Request Headers** section, find the `x-user-auth-token` header and copy its value. Paste this into `password_or_token`.
7. To find your `user_id`, look at the **Payload** (or Query String Parameters) for that same API request. You will see a `user_id` parameter. Copy its value and paste it into `email_or_userid`.
   *(Note: Both values can often also be found in the **Application** / **Storage** tab under **Local Storage** for `https://play.qobuz.com`)*

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
  -v /path/to/your/config:/config \
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
| Config | `config.toml` storage | `/mnt/your_pool/apps/musicrequest/config` |
| Music | Downloaded music library | `/mnt/your_pool/media/music` |

Set permissions to UID/GID **568** (the TrueNAS `apps` user):

```bash
# SSH into TrueNAS
sudo chown -R 568:568 /mnt/your_pool/apps/musicrequest/config
sudo chown -R 568:568 /mnt/your_pool/media/music
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
| `APP_PASSCODE` | Your chosen passcode |
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
| `/mnt/your_pool/apps/musicrequest/config` | `/config` | Streamrip configuration |
| `/mnt/your_pool/media/music` | `/music` | Music download destination |

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
| `APP_PASSCODE` | `your_secure_passcode` | Passcode required to access the web UI |
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
