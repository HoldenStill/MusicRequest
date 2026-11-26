# Built-in MusicRequest MCP Server (Streamable HTTP)

MusicRequest includes a native **Model Context Protocol (MCP)** server hosted directly inside the FastAPI application at **`/mcp`** using the **Streamable HTTP** transport.

This allows **Hermes Agent** to connect directly over HTTP/HTTPS without spawning external Python subprocesses or managing local scripts.

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

## Authentication

The `/mcp` endpoint is protected by **Bearer token authentication** (`Authorization: Bearer <token>`):

- **`MCP_API_TOKEN`** *(recommended)*: Dedicated token for MCP clients.
- **`APP_PASSCODE`** *(fallback)*: Used if `MCP_API_TOKEN` is unset.
- If neither is configured (empty), `/mcp` fails closed and rejects all requests with `401 Unauthorized`.

---

## Hermes Agent MCP Configuration

Configure Hermes Agent to connect to your MusicRequest server's `/mcp` URL using Streamable HTTP transport and the `Authorization` header.

### JSON Configuration
```json
{
  "mcpServers": {
    "musicrequest-api": {
      "transport": "streamable_http",
      "url": "https://music.yourdomain.com/mcp",
      "headers": {
        "Authorization": "Bearer your_mcp_api_token"
      }
    }
  }
}
```

### YAML Configuration
```yaml
mcpServers:
  musicrequest-api:
    transport: streamable_http
    url: "https://music.yourdomain.com/mcp"
    headers:
      Authorization: "Bearer your_mcp_api_token"
```

*(For local/LAN access without TLS, use `http://<server-ip>:8080/mcp`.)*

---

## Reverse Proxy Notes (Nginx Proxy Manager)

If MusicRequest is exposed behind **Nginx Proxy Manager**:
- Standard HTTP/HTTPS forwarding works out of the box for `/mcp`.
- Ensure **proxy buffering is disabled** (`proxy_buffering off;` in the Advanced tab, as already recommended in the main [README.md](../README.md)) so Streamable HTTP SSE responses stream immediately without buffering delays.
- If you enabled an **Nginx Access List (HTTP Basic Auth)** on the proxy host, either bypass it for `/mcp` or configure your MCP client accordingly, since `/mcp` already enforces `Authorization: Bearer` authentication.

---

## Recommended Hermes Agent Workflow & System Prompt

To coordinate **Jellyfin** and **MusicRequest** seamlessly:

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
