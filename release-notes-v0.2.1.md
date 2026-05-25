## wmole v0.2.1 - Visible slash menu fix

### Highlights

- Fixes the `/` command picker showing only its input border while command
  results were clipped below the visible terminal viewport.
- Reserves terminal rows dynamically while the command palette is open, so
  all operational choices remain visible in ordinary window heights.
- Keeps the `v0.2.0` command-input workflow and safety behavior unchanged.

### Testing

- Adds a regression test rendering a crowded filesystem view in a 40-line
  terminal and checking that the first and last slash commands are visible.

### Website

- Updates the static changelog fallback and download link to `v0.2.1`;
  live release data continues to load from GitHub Releases.
