## wmole v0.2.2 - Auto-update disable fix

### Highlights

- Confirms that `/update` is available from the command input and performs an
  immediate GitHub Releases update check in installed builds.
- Confirms automatic updates: installed builds check in the background, stage
  a newer installer, and run it silently when wmole exits.
- Fixes `wmole update --disable-auto` so it does not begin one final
  background update check before persisting the disabled setting.

### Testing

- Adds coverage ensuring the disable command does not start an automatic
  background update check.
- Keeps the visible slash-menu viewport regression coverage introduced in
  `v0.2.1`.

### Website

- Updates the static changelog fallback and download link to `v0.2.2`;
  live release data continues to load from GitHub Releases.
