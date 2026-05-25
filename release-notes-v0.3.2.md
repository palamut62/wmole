## wmole v0.3.2 - Interactive Self-Update & Robust Downloader

### Highlights

- **Interactive Update TUI Modal**: Selecting `/update` in the Command Input now displays a fully interactive modal screen that lets you search, download, and install updates without leaving the TUI.
- **Live Download Progress Bar**: Displays a beautiful Unicode progress bar (`██░░`) inside the update modal showing live download percentage and size.
- **Restart Prompt**: Upon successful download, wmole prompts you to restart immediately. Pressing `Enter` will detachedly launch the setup installer and quit wmole, completing the in-place upgrade instantly.
- **Robust Background Downloader Throttling**: Fixed the 6-hour check lockout bug. Background checks now only write their throttling timestamp to `update_check.json` upon successful completion or when already up to date. If a background download is interrupted or aborted, wmole will retarget the check immediately on next launch.
- **Developer/Source Update Mode**: Running `/update` in developer checkouts automatically initiates synchronous `git pull` and `pip install` upgrades inside the TUI with visual status reports.

### Testing

- Verifies that `git pull` triggers and executes seamlessly under source checkouts.
- Unit tests pass with `28 tests OK`.
- Checked and validated on Windows terminals.
