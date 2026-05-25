## wmole v0.3.0 - Complete Command Palette

### Highlights

- Expands the `/` command menu from core views to the application's interactive
  actions, including safe cleanup, large files, drives, selection, delete,
  permanent mode, Explorer open, refresh, leftovers, language, back, and quit.
- Makes the command list scrollable so actions beyond the first visible page
  can be reached with the keyboard.
- Fixes the open command panel clipping its bottom border at the terminal edge.
- Makes `/update` report an explicit visible status in the TUI, including
  `Sürüm güncel.` in Turkish when the installed release is current.

### Testing

- Covers the complete command inventory and navigation beyond the first page.
- Covers palette rendering within a fixed terminal height so the border remains
  visible.
- Covers silent update checks returning status for the TUI and Turkish current
  version messaging.

### Website

- Updates the static fallback release card and command overview for `v0.3.0`;
  live release data continues to load from GitHub Releases.
