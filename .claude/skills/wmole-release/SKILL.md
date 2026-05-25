---
name: wmole-release
description: Use whenever the user asks to ship, release, publish, or "deploy a new version" of wmole — or right after landing a noticeable feature/fix on main. Bumps version, commits, pushes, rebuilds the EXE + Inno Setup installer, creates a GitHub release with both artifacts attached, and lets the static site pick up the new release automatically via the GitHub API.
---

# wmole release pipeline

This repo ships as a Windows EXE + Inno Setup installer. The marketing site
(`index.html`, deployed at https://wmole.vercel.app) reads
`GET https://api.github.com/repos/palamut62/wmole/releases` on every page load,
so **the changelog and hero download button update themselves** once a new
GitHub release exists. You do **not** edit `index.html` per release.

## When to invoke

- User says "release", "new version", "ship it", "exe çıkar", "github release"
- You just landed a user-visible feature/fix on `main` and the user said
  "bunu yayınla" / "this should auto-release on big changes"
- Skip for docs-only or tiny refactor commits unless asked

## The pipeline (single command)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/release.ps1 `
  -Version <X.Y.Z> `
  -NotesFile release-notes-v<X.Y.Z>.md
```

`scripts/release.ps1` does, in order:
1. Rewrites `#define MyAppVersion` in `installer/wmole.iss`.
2. `git add -A && git commit -m "release: vX.Y.Z" && git push` (skip with `-SkipPush`).
3. Calls `scripts/build-windows-release.ps1 -Version X.Y.Z` →
   `dist/wmole.exe` + `release/wmole-X.Y.Z-setup.exe` (skip with `-SkipBuild`).
4. `git tag -f vX.Y.Z && git push origin vX.Y.Z --force`.
5. `gh release create vX.Y.Z <installer> <exe> --title "wmole vX.Y.Z" --notes-file …`
   (re-creates if the tag already had a release).

Add `-Prerelease` for beta builds.

## Your workflow

1. **Pick the next semver bump** — read `installer/wmole.iss` for the current
   `MyAppVersion`. Default to a patch bump; minor for new commands/features;
   major only on breaking changes. Confirm with the user if ambiguous.
2. **Write release notes** to `release-notes-v<X.Y.Z>.md` at repo root.
   Mirror the structure of `release-notes-v0.1.2.md`:
   `## wmole vX.Y.Z — <short title>` → `### Highlights` → optional
   `### New CLI` fenced block → `### Internals`. Keep it markdown; the site's
   renderer supports headings, lists, bold, italic, inline code, links.
3. **Run the pipeline** with that version + notes file. Stream the output;
   watch for PyInstaller / ISCC / gh errors.
4. **Verify**:
   - `gh release view vX.Y.Z` shows the two assets
     (`wmole-X.Y.Z-setup.exe`, `wmole.exe`).
   - The site update is hands-off; mention the live URL
     `https://wmole.vercel.app/#changelog`.
5. **Do not** redeploy Vercel just for a release — the site is dynamic.
   Only redeploy if `index.html` / `assets/` actually changed.

## Prerequisites (already installed on this machine)

- `py -3` + PyInstaller (`C:\Users\umuti\AppData\Local\Programs\Python\Python312\Scripts\pyinstaller.exe`)
- Inno Setup 6 (`%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`)
- `gh` CLI authenticated against `palamut62/wmole`
- Working tree should be clean before bumping; commit unrelated WIP first.

## Failure modes seen before

- `gh release view` returning non-zero printed "release not found" to stderr
  and tripped `$ErrorActionPreference = Stop`. Fixed in `release.ps1` by
  toggling EAP around the probe and checking `$LASTEXITCODE`. If you ever
  regress this, the symptom is: tag gets pushed but no release is created —
  run `gh release create …` manually with the already-built artifacts in
  `release/` and `dist/`.
- PyInstaller can leave `build/` / `__pycache__/` dirty; safe to ignore but
  do not commit them (already gitignored).
