# MusicRequest MCP Server for Hermes Agent

A Model Context Protocol (MCP) server that links your self-hosted **MusicRequest** instance into **Hermes Agent**.

This allows Hermes Agent to search the high-resolution Qobuz music catalog, inspect tracklists, enqueue album or track downloads, check download progress, and automate music acquisition alongside your existing Jellyfin server.

---

## Tools Exposed

| Tool Name | Description | Key Parameters |
|-----------|-------------|----------------|
| `musicrequest_search` | Search Qobuz for albums, tracks, or artists | `query`, `type` (`album` / `track` / `artist`), `limit` |
| `musicrequest_get_album_tracks` | Fetch album tracklist and durations | `album_id` |
| `musicrequest_download` | Enqueue an album or single track for download | `album_id`, `title`, `artist`, `download_type`, `track_id`, `quality` |
| `musicrequest_queue_status` | Inspect active, queued, and completed jobs | `active_only` |
| `musicrequest_quick_download` | Search and enqueue the best match in one step | `query`, `artist`, `download_type`, `quality` |

---

## Configuration & Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MUSICREQUEST_URL` | No | `http://localhost:8080` | Base URL of your MusicRequest instance |
| `MUSICREQUEST_PASSCODE` | Yes | `1099` | Passcode configured on MusicRequest (`APP_PASSCODE`) |
| `MUSICREQUEST_DEFAULT_QUALITY` | No | *(Server default)* | Optional default quality override (1: 320k MP3, 2: 16/44.1 CD FLAC, 3: 24/≤96 Hi-Res, 4: 24/≥96 Hi-Res) |

---

## Hermes Agent MCP Setup

You can configure the MCP server in your Hermes Agent MCP config file (e.g. `config.json` or `config.yaml`).

### Option A: Running with `uv` (Recommended)

Since `musicrequest_mcp.py` uses PEP 723 inline script metadata, `uv` will automatically provision `mcp` and `httpx` dependencies without manual virtualenv management.

#### JSON Configuration:
```json
{
  "mcpServers": {
    "musicrequest-api": {
      "command": "uv",
      "args": [
        "run",
        "/path/to/MusicRequest/mcp/musicrequest_mcp.py"
      ],
      "env": {
        "MUSICREQUEST_URL": "http://192.168.1.50:8080",
        "MUSICREQUEST_PASSCODE": "your_secure_passcode",
        "MUSICREQUEST_DEFAULT_QUALITY": "3"
      }
    }
  }
}
```

#### YAML Configuration:
```yaml
mcpServers:
  musicrequest-api:
    command: uv
    args:
      - run
      - /path/to/MusicRequest/mcp/musicrequest_mcp.py
    env:
      MUSICREQUEST_URL: "http://192.168.1.50:8080"
      MUSICREQUEST_PASSCODE: "your_secure_passcode"
      MUSICREQUEST_DEFAULT_QUALITY: "3"
```

---

### Option B: Running with standard Python inside a venv

If your Hermes environment runs inside a dedicated virtual environment:

```json
{
  "mcpServers": {
    "musicrequest-api": {
      "command": "/path/to/venv/bin/python",
      "args": [
        "/path/to/MusicRequest/mcp/musicrequest_mcp.py"
      ],
      "env": {
        "MUSICREQUEST_URL": "http://192.168.1.50:8080",
        "MUSICREQUEST_PASSCODE": "your_secure_passcode"
      }
    }
  }
}
```

---

## Recommended Hermes Agent Workflow & System Prompt

To achieve smooth, autonomous media management between **Jellyfin** and **MusicRequest**, provide instructions in your Hermes Agent system prompt or persona guidelines:

```markdown
### Music Library & Acquisition Workflow

You have access to both your local music server (`jellyfin-api`) and your music acquisition server (`musicrequest-api`).
When the user asks to play, find, or download music:

1. **Check Existing Library**:
   - Always call `jellyfin_search_library(query="...")` first to see if the album or track is already in the local collection.
   - If found, fulfill the user's request (e.g., provide track info, queue, or add to playlist).

2. **Download Missing Music**:
   - If the requested music is not in Jellyfin, inform the user and call `musicrequest_quick_download(query="...", artist="...")` with `download_type="album"` (or `"track"` if they only asked for a single song).
   - Alternatively, use `musicrequest_search` if you need to let the user select between multiple editions or versions.

3. **Verify Download Status**:
   - You can call `musicrequest_queue_status(active_only=True)` to confirm the download has started or completed.

4. **Trigger Jellyfin Rescan**:
   - Once a download completes, call `jellyfin_trigger_scan()` so Jellyfin scans the music directory and makes the newly acquired songs available immediately.
```

---

## Testing & Manual Run

To test the MCP server manually from terminal:
```bash
# Using uv:
MUSICREQUEST_URL="http://localhost:8080" MUSICREQUEST_PASSCODE="1099" uv run mcp/musicrequest_mcp.py

# Using python directly:
pip install "mcp>=1.0.0" "httpx>=0.27.0"
python mcp/musicrequest_mcp.py
```
