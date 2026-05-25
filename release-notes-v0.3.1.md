## wmole v0.3.1 - Closed Scrolling Command Frame

### Highlights

- Fixes the `/` menu bottom border still being clipped in shorter terminal
  windows by sizing visible command rows from the actual available height.
- Keeps commands scrolling inside one closed panel instead of extending beyond
  the lower edge of the terminal.
- Makes the active command obvious with a visible `>` cursor and highlighted
  row; pressing Enter runs the highlighted command after scrolling.

### Testing

- Adds coverage for closed-border rendering in 30-row and 22-row terminals.
- Adds coverage that scrolling to a command below the first page keeps it
  visible and executable with Enter.
- Verifies palette framing and highlighted selection from 21 through 40
  terminal rows.

### Website

- Updates the static fallback release card and setup link to `v0.3.1`;
  live release data continues to load from GitHub Releases.
