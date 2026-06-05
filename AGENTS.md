# AGENTS.md — wmole

Bu, **Tauri masaüstü uygulaması** (Svelte frontend + Python sidecar `mole.py`).

## Versiyonlama Kuralı (GLOBAL — DEĞİŞTİRİLEMEZ)
- **Büyük değişiklik / yeni özellik** → versiyon artırılır (minor/major).
- **Hata düzeltme (bug-fix) / küçük UI rötuşu** → versiyon **AYNI KALIR**.
- Versiyon tek kaynaktan üç yerde tutulur ve daima senkron olmalı:
  - `gui/package.json`
  - `gui/src-tauri/tauri.conf.json`
  - `gui/src-tauri/Cargo.toml`
  - (ayrıca `installer/wmole.iss` release script tarafından güncellenir)

## Release Akışı
Her büyük değişiklik veya yeniden yayınlamada şu adımlar uygulanır:
1. **EXE derle** (Tauri):
   - Sidecar hazır: `gui/src-tauri/binaries/wmole-x86_64-pc-windows-msvc.exe`
     (sadece `mole.py` değiştiyse PyInstaller ile yeniden üret:
     `scripts/build-windows-release.ps1` → `dist/wmole.exe` → bu yola kopyala).
   - `cd gui && npm run tauri build` → MSI/NSIS çıktıları
     `gui/src-tauri/target/release/bundle/` altında.
2. **GitHub push**: `git add -A && git commit && git push`.
3. **Release push**: `gh release` ile tag + artefaktlar. Aynı versiyon yeniden
   yayınlanıyorsa mevcut tag/release silinip yeniden oluşturulur.
4. **Web sayfası**: `index.html` changelog + indirme butonu GitHub Releases
   API'sinden çalışma anında otomatik güncellenir — elle düzenleme gerekmez.

## UI Kuralı
- Native `alert/confirm/prompt` YASAK. Daima in-app modal/toast (`ConfirmModal`,
  `GenericConfirm`, `Toast`) kullan.
