## wmole v0.1.3 — In-app self-update

### Highlights
- `wmole update` now upgrades the **installed EXE in place** by fetching the latest GitHub release, downloading the setup, and running it silently. No more re-downloading the installer manually.
- Source checkouts keep the legacy `git pull` + `pip upgrade` behavior automatically.

### Updated CLI
```
wmole update                # checks GitHub, prompts before upgrading
wmole update --yes          # no prompt, just do it
wmole update --dry-run      # show what would happen, change nothing
wmole update --json         # machine-readable steps
```

### How it works (frozen exe)
1. Reads `__version__` baked into the exe.
2. `GET api.github.com/repos/palamut62/wmole/releases/latest`.
3. Compares semver; exits early if already current.
4. Downloads the `*-setup.exe` asset to `%TEMP%` with a percentage progress bar.
5. Launches it detached with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART` so the installer can overwrite the running exe after wmole exits.
6. Logs the upgrade to `~/.wmole/logs/operations.log` under the `self_update` action.

### Internals
- Pure-stdlib `urllib` fetch + download — no extra dependency.
- New helpers: `_parse_semver`, `fetch_latest_release`, `_pick_installer_asset`, `_download`.
- `--yes` flag promoted to a top-level CLI option for any future interactive prompts.
