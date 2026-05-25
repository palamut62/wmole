# Command Input Navigation And v0.2.0 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the persistent slash-command input the sole top-level TUI operation launcher, update the website/docs for that UX, and publish Windows release `v0.2.0`.

**Architecture:** Reuse wmole's existing Rich TUI command palette and action dispatcher, limiting the catalog and rendering an idle command prompt on every view. Remove only redundant top-level mode hotkeys while retaining contextual navigation and destructive-operation safeguards. The static website remains backed by GitHub Releases at runtime, with source copy and fallback content updated to accurately describe `v0.2.0`.

**Tech Stack:** Python 3 / Rich / unittest, static HTML/Tailwind CDN site, PyInstaller, Inno Setup, Git/GitHub CLI, Vercel static deployment.

---

### Task 1: Specify command-input behavior in tests

**Files:**
- Modify: `tests/test_mole.py`
- Test: `tests/test_mole.py`

- [ ] **Step 1: Add failing tests for the approved command catalog and visible idle prompt**

Add tests asserting `palette_commands()` returns:

```python
["analyze", "categories", "purge", "installers", "uninstall",
 "optimize", "status", "ports", "update", "help"]
```

Assert that removed commands (`lang`, `permanent`, `quit`) do not match palette
filtering. Add a render assertion for a new `render_command_input()` function:

```python
prompt = mole.render_command_input()
self.assertIn("/", prompt.plain)
self.assertIn("operations", prompt.plain.lower())
```

- [ ] **Step 2: Run tests and confirm the new behavior is absent**

Run:

```powershell
py -3 -m unittest discover -s tests -v
```

Expected: FAIL because the old catalog still contains general actions and
`render_command_input()` is not yet defined.

### Task 2: Implement the TUI command-input navigation

**Files:**
- Modify: `mole.py`
- Test: `tests/test_mole.py`

- [ ] **Step 1: Restrict the slash command catalog**

Keep the existing entries through `help`, and remove these catalog entries:

```python
{"name": "lang", ...}
{"name": "permanent", ...}
{"name": "quit", ...}
```

Remove their now-unreachable `exec:*` palette dispatcher branches.

- [ ] **Step 2: Render a persistent idle input line**

Add:

```python
def render_command_input() -> Panel:
    hint = "İşlemleri açmak için / yazın" if LANG == "tr" else "Type / to open operations"
    line = Text()
    line.append(" > ", style="bold bright_magenta")
    line.append(hint, style="grey62")
    return Panel(line, border_style="grey35", padding=(0, 1))
```

When the palette is closed, append `render_command_input()` in `render()`;
when it is open, continue to append the filterable `render_palette(...)`.

- [ ] **Step 3: Remove redundant top-level mode keyboard handlers**

Delete only the `Shift+A/C/P/I/U/M/S` handler block from `run_tui()`. Preserve
`K`, `?`, `\`, list traversal, deletion, confirmation, filesystem extras,
leftovers, refresh, and exit behavior.

- [ ] **Step 4: Update in-app help and footer content**

Replace footer mode shortcut tokens with a command-input cue and contextual
back/help controls. Replace the built-in help top-level mode section with
slash commands such as:

```text
/analyze          Browse the filesystem with live folder sizes
/categories       Category breakdown of disk usage
...
/help             Open this help screen
```

- [ ] **Step 5: Run tests until green**

Run:

```powershell
py -3 -m py_compile mole.py
py -3 -m unittest discover -s tests -v
```

Expected: both commands exit `0`.

### Task 3: Update public documentation and website data

**Files:**
- Modify: `README.md`
- Modify: `index.html`
- Create: `release-notes-v0.2.0.md`

- [ ] **Step 1: Update README interaction documentation**

Replace the top-level mode shortcut documentation with the persistent command
input flow and the ten approved slash commands. Leave contextual controls and
safety notes documented.

- [ ] **Step 2: Update the website's interaction copy and fallback release**

In `index.html`, revise the interactive TUI section so it advertises slash
input navigation rather than top-level shortcut control. Change the static
fallback changelog card from `v0.1.0` to `v0.2.0`, with its download URL and
summary describing command-input navigation. Runtime GitHub Releases loading
continues unchanged.

- [ ] **Step 3: Add release notes**

Create `release-notes-v0.2.0.md` documenting:

```markdown
## wmole v0.2.0 - Command input navigation

### Highlights
- Visible command input for operation access.
- Slash menu limited to operational views and help.
- Direct top-level mode hotkeys replaced by command selection.

### Website
- Updated public interaction guidance and release fallback.
```

- [ ] **Step 4: Inspect static references**

Run:

```powershell
rg -n "Shift\+[ACPIUMS]|v0\.1\.0|quick navigation shortcuts|rapid keys" README.md index.html mole.py
```

Expected: no removed mode-navigation or stale fallback marketing references.

### Task 4: Build, publish, and verify release v0.2.0

**Files:**
- Modify through release pipeline: `installer/wmole.iss`
- Artifacts: `dist/wmole.exe`, `release/wmole-0.2.0-setup.exe`

- [ ] **Step 1: Verify the implementation before publication**

Run:

```powershell
py -3 -m py_compile mole.py
py -3 -m unittest discover -s tests -v
```

Expected: compilation passes and all tests pass.

- [ ] **Step 2: Build Windows artifacts once before publishing**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows-release.ps1 -Version 0.2.0
```

Expected: `dist/wmole.exe` and `release/wmole-0.2.0-setup.exe` exist.

- [ ] **Step 3: Run the repository release pipeline**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/release.ps1 -Version 0.2.0 -NotesFile release-notes-v0.2.0.md
```

Expected: source changes and version bump are committed and pushed to `main`,
the final installer/executable are rebuilt, tag `v0.2.0` is pushed, and the
GitHub release is created with both assets.

- [ ] **Step 4: Verify GitHub publication**

Run:

```powershell
gh release view v0.2.0
git status --short --branch
git ls-remote --heads origin main
```

Expected: GitHub reports the `v0.2.0` installer and `wmole.exe`; local `main`
is synchronized with `origin/main`.

- [ ] **Step 5: Verify or trigger web deployment**

Because `index.html` changes, verify the Vercel production page after the
GitHub push. If Git integration has not deployed the commit, use the linked
Vercel project with `npx vercel --prod --yes`, then verify the public page
contains command-input navigation and resolves the `v0.2.0` release content.

