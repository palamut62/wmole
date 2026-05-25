## wmole v0.1.4 — Silent background auto-update

wmole now updates itself the way Claude Code does: silently, in the background, applied on the next launch. No prompts, no setup wizard popping up, no babysitting.

### How it works
1. **On every launch**, a daemon thread checks `api.github.com/repos/palamut62/wmole/releases/latest`.
2. The check is **throttled to once every 6 hours** via `~/.wmole/update_check.json` — no API spam.
3. If a newer version exists, the installer asset is **downloaded silently** to `%TEMP%` and recorded in `~/.wmole/pending_update.json`.
4. When wmole exits, an `atexit` hook launches the staged installer **detached with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`** so it can overwrite the just-exited exe.
5. Next time you run any wmole command, you're on the new version. The only user-facing trace is a single stderr line: `(wmole: installing update vX.Y.Z in background)`.

### Opt-out
Auto-update is on by default. Three equivalent ways to turn it off:

```powershell
wmole update --disable-auto        # persists to ~/.wmole/config.json
$env:WMOLE_NO_AUTO_UPDATE = "1"    # per-session
# or edit ~/.wmole/config.json: { "auto_update": false }
```

Re-enable:
```powershell
wmole update --enable-auto
```

Manual `wmole update` / `wmole update -y` from v0.1.3 still work for on-demand upgrades.

### Safety
- Auto-update is **frozen-exe only** — running from a source checkout never triggers it.
- Every failure path (no network, GitHub down, install denied) is swallowed silently; auto-update can never crash the host command.
- Throttle file + size-checked download cache prevent re-downloading the same installer.
- Operation log entry written under `auto_update` in `~/.wmole/logs/operations.log`.

### Internals
- Stdlib `urllib` only — no new deps.
- New files: `~/.wmole/update_check.json`, `~/.wmole/pending_update.json`.
- New functions: `start_auto_update_check`, `_auto_update_worker`, `apply_pending_update`, `_auto_update_disabled`.
- `main_cli` kicks the worker; `__main__` `finally` block triggers the installer launch.
