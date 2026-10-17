# Register App with Portal

Register a web application with the AI BlackBox Portal so it appears in the Running Apps menu and can be accessed through the reverse proxy.

## Quick Reference

```bash
# Register an app
curl -X POST http://localhost:9091/agent/apps/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "App Name",
    "port": 8065,
    "directory": "/path/to/app",
    "operator": "system"
  }'

# List registered apps
curl http://localhost:9091/agent/apps

# Unregister an app
curl -X DELETE http://localhost:9091/agent/apps/{app_id}
```

## Complete App Creation Workflow

When creating a web application for the user, follow these steps:

### Step 1: Create the App Directory

Always create apps in the persistent `Apps/` directory:

```bash
mkdir -p /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/{app-name}
```

### Step 2: Create the App Files

At minimum, create:
- `index.html` - The main HTML file
- `server.py` - A simple Python HTTP server

**Example server.py:**
```python
#!/usr/bin/env python3
"""Simple HTTP server for the app."""
import http.server
import socketserver
import os

PORT = 8065  # Choose an available port (8060-8099 recommended)
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

if __name__ == "__main__":
    os.chdir(DIRECTORY)
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"App running at http://localhost:{PORT}")
        httpd.serve_forever()
```

### Step 3: Start the App Server

Run the server in the background:

```bash
python3 /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/{app-name}/server.py &
```

Verify it's running:
```bash
ss -tlnp | grep {PORT}
```

### Step 4: Register with the Portal

```bash
curl -X POST http://localhost:9091/agent/apps/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Human Readable App Name",
    "port": {PORT},
    "directory": "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/{app-name}",
    "operator": "system"
  }'
```

**Registration Parameters:**
- `name` (required): Display name shown in the Running Apps menu
- `port` (required): The port the app is running on
- `directory` (optional): Full path to the app directory
- `operator` (optional): Set to "system" for apps available to all users, or specific operator name

### Step 5: Verify Registration

```bash
curl http://localhost:9091/agent/apps
```

The app should now appear in the Portal under Menu → Running Apps.

## Accessing the App

Once registered, the app is accessible via:

1. **Portal Menu**: Menu (☰) → Running Apps → Click "Open"
2. **Direct Proxy URL**: `http://localhost:9091/app-proxy/{PORT}/`
3. **Via Tailscale**: `https://{tailscale-hostname}/app-proxy/{PORT}/`

**Proxied apps are sandboxed.** `/app-proxy/{PORT}/` only serves registered
ports (anything else is 403) and sends `Content-Security-Policy: sandbox`, so
the app runs in an opaque origin wherever it is opened through the proxy. It
can still call its OWN server with relative URLs and load media from absolute
`http://localhost:9091/ui/uploads/...` URLs, but it cannot call BlackBox APIs
(`/chat`, `/tts`, `/ws/...`), and `localStorage` is an in-memory stand-in that
clears on reload. An app that needs the BlackBox API must be opened on its own
port (`http://<host>:{PORT}/`); the Orchestrator trusts that origin.

## Port Allocation

Recommended port ranges:
- `8060-8099`: User-created apps
- `8065`: Reserved for test/demo apps
- `9091`: Reserved for the Orchestrator API

Before starting an app, check if the port is available:
```bash
ss -tlnp | grep {PORT}
```

## Troubleshooting

**App not appearing in menu:**
1. Verify the server is running: `ss -tlnp | grep {PORT}`
2. Check registration: `curl http://localhost:9091/agent/apps`
3. Hard refresh the portal: Ctrl+Shift+R

**App not loading in preview:**
1. Test direct access: `curl http://localhost:{PORT}/`
2. Test proxy: `curl http://localhost:9091/app-proxy/{PORT}/`
3. Check for CORS issues in browser console

**Port already in use:**
```bash
# Find what's using the port
ss -tlnp | grep {PORT}
# Kill the process if needed
kill {PID}
```

## Example: Complete App Creation

```bash
# 1. Create directory
mkdir -p /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/my-app

# 2. Create index.html (use Write tool)

# 3. Create server.py (use Write tool)

# 4. Start server
python3 /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/my-app/server.py &

# 5. Wait for startup
sleep 2

# 6. Verify running
ss -tlnp | grep 8066

# 7. Register
curl -X POST http://localhost:9091/agent/apps/register \
  -H "Content-Type: application/json" \
  -d '{"name": "My App", "port": 8066, "operator": "system"}'

# 8. Confirm
curl http://localhost:9091/agent/apps
```

## Important Notes

- Apps with `operator: "system"` are visible to ALL users
- Apps are stored in memory and will be lost on service restart
- Always use the `Apps/` directory for persistent app files
- The reverse proxy handles all HTTP methods (GET, POST, PUT, DELETE, etc.)
- Static file servers work best; for dynamic apps, ensure proper CORS headers
