From a Git checkout with Python 3.11+ and Copilot CLI installed,
run `python3 -m venv .venv && .venv/bin/pip install -e .`

Add this server to `~/.copilot/mcp-config.json`, using absolute paths:

```json
{
  "mcpServers": {
    "flamethrower": {
      "type": "local",
      "command": "<PATH_TO_FLAMETHROWER_CHECKOUT>/.venv/bin/flamethrower-mcp",
      "args": [
        "<PATH_TO_YOUR_PROJECT>"
      ]
    }
  }
}
```

Start `copilot --acp --port 64674` in another terminal, then run `.venv/bin/flamethrower --acp-port 64674 <PATH_TO_YOUR_PROJECT>` and open the printed URL.
