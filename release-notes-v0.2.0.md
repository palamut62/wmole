## wmole v0.2.0 - Command input navigation

### Highlights

- Adds an always-visible command input line to the TUI.
- Type `/` to select operational views from a focused menu: `analyze`, `categories`, `purge`, `installers`, `uninstall`, `optimize`, `status`, `ports`, `update`, and `help`.
- Removes direct top-level mode switching through `Shift+A/C/P/I/U/M/S`; navigation now happens through deliberate command selection.
- Keeps contextual actions and safety controls in place, including recycle-bin deletion by default, permanent mode, and high-risk confirmations.

### Website

- Updates the public interaction guide to describe slash-command navigation and contextual controls.
- Refreshes the static changelog fallback to `v0.2.0`; live release entries and installer links still load from GitHub Releases.

### Internals

- Reuses the existing Rich palette dispatcher with a restricted operation catalog.
- Adds regression tests for palette scope, the idle command input, and removed mode-shortcut documentation.
