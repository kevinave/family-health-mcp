# Deployment

Two always-on services: the MCP server on loopback, and a tunnel in front of it.
Examples below are for macOS (launchd) and Cloudflare Tunnel; nothing in the
server depends on either choice.

## 1. The server

`com.example.family-health-mcp.plist` → `~/Library/LaunchAgents/`

```bash
cp deploy/com.example.family-health-mcp.plist ~/Library/LaunchAgents/
# edit the paths and environment variables inside first
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.family-health-mcp.plist
```

Restart after changing `tokens.json` — the token table is read once at startup:

```bash
launchctl kickstart -k gui/$(id -u)/com.example.family-health-mcp
```

## 2. The tunnel

A named Cloudflare tunnel keeps an outbound connection open, so the host needs
no inbound ports and no public IP.

```bash
cloudflared tunnel create family-health
cloudflared tunnel route dns family-health health.example.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-id>
credentials-file: /Users/<you>/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: health.example.com
    service: http://127.0.0.1:8787
  - service: http_status:404
```

Then run `cloudflared tunnel run family-health` as a second launchd agent with
`KeepAlive` enabled.

## 3. The client

Add the connector as `https://health.example.com/mcp-<path_token>` with the
bearer token from `tokens.json`.

After **any** change to the tool set, refresh the connector in the client and
start a new conversation — cached tool lists do not update on their own.
