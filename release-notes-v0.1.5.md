## wmole v0.1.5 — Slash command palette + Help screen

Two UX wins for the TUI: a Claude Code-style `/` command palette, and a full Help / Features screen that documents every mode, key, and CLI command in TR + EN.

### Slash command palette
Press **`/`** anywhere in the TUI to open a filterable command picker.

- Type to fuzzy-filter (matches name or description)
- `↑/↓` move the cursor, `Enter` runs the command, `Esc` cancels, `Backspace` edits
- 13 commands wired in: `analyze`, `categories`, `purge`, `installers`, `uninstall`, `optimize`, `status`, `ports`, `update`, `help`, `lang`, `permanent`, `quit`
- `view:*` commands switch the active TUI panel; `exec:*` commands run an action (toggle lang, toggle permanent-mode, snapshot listening ports, kick a self-update check, exit)

### Help & Features screen
Press **`?`** or **`Shift+H`** to open.

Sections (both languages):
- What is wmole? (purpose & safety defaults)
- Top-level modes (Shift+A/C/P/I/U/M/S)
- List navigation (↑/↓, Enter, Space, D, K, O, R, Q/Esc)
- FS-mode extras (G, V) + Uninstall extras (L)
- Safety model (Recycle Bin default, permanent mode, whitelist/denylist, protected paths, operations log)
- All CLI commands with one-line summaries
- Auto-update behavior + opt-out instructions
- Tips & issue tracker link

### Footer polish
- Now shows `/` Komutlar and `H Yardım` (TR) / `/` Commands and `H Help` (EN)
- Header includes the running version (`v0.1.5`)

### Internals
- New `View(kind="help")` with `render_help()` producing a bilingual section list.
- New `palette_commands()`, `filter_palette()`, `render_palette()`.
- `render()` accepts an optional `palette=(query, cursor)` tuple and renders the picker as an overlay panel under the footer.
- Key loop in `run_tui` intercepts every keystroke when `palette_open`; `/` from any other view opens the palette.
- Action dispatcher maps `view:*` to view-stack pushes (creating a fresh `Scanner` where needed) and `exec:*` to direct function calls.
- New T() keys: `title_help`, `hint_help`, `help_key`.
