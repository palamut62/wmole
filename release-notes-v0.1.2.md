## wmole v0.1.2 — Developer Ports Manager

### Highlights
- New `ports` command: detect listening developer servers on localhost (Vite, Next, Django, Flask, Postgres, Redis, Ollama, …) and kill them by port, PID, or all at once.
- Web changelog now syncs **automatically** from GitHub Releases — no more hand-editing the site after every release.
- Hero "Download Installer" button always points at the latest published installer asset.

### New CLI
```
wmole ports                       # list listening localhost ports + PIDs
wmole ports --kill 5173           # kill whatever is bound to :5173
wmole ports --kill 12345          # kill by PID
wmole ports --kill all --dry-run  # preview killing every dev listener
wmole ports --json                # machine-readable
wmole ports --all-binds           # include non-localhost listeners
```

### Internals
- `psutil.net_connections` with localhost / 0.0.0.0 / IPv6 wildcard filtering.
- Well-known dev port hint table (3000, 5173, 8000, 27017, 11434, …).
- All kills logged to `~/.wmole/logs/operations.log` under the `port_kill` action.
- Added one-shot release pipeline at `scripts/release.ps1` so future releases ship with a single command.
