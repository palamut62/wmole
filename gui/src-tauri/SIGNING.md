# Güncelleme İmzalama (minisign / ed25519)

`src/updater.rs` indirilen `-setup.exe` dosyasını iki katmanda doğrular:

1. **SHA-256** (`<setup>.sha256` asset'i) — ucuz erken eleme, sadece bozuk indirmeyi yakalar.
2. **Minisign imzası** (`<setup>.minisig` asset'i) — asıl güvenlik katmanı. Secret key olmadan
   üretilemediği için release'i ele geçiren biri geçerli imza üretemez.

> Not: Bu dosya `gui/src-tauri/` altında tutulur; kök `README.md` bu bölüme referans verebilir.

## 1. Anahtar çifti üret (bir kez, çevrimdışı makinede)

```sh
minisign -G -p wmole.pub -s wmole.key
```

- `wmole.pub` iki satırdır. **İkinci satır** base64 public key gövdesidir (`RWQ...` ile başlar).
- Bu ikinci satırı `src/updater.rs` içindeki `UPDATE_PUBKEY` sabitine yapıştır ve
  `PUBKEY_PLACEHOLDER` sabitiyle artık eşleşmediğinden emin ol.
- `wmole.key` (secret key) **asla repoya girmez**. Parolasını da ayrı sakla.

`UPDATE_PUBKEY` placeholder olduğu sürece imza doğrulaması zorunlu değildir: uygulama
kullanıcıya `update-unsigned-warning` event'i ile uyarı gösterir ama indirmeyi engellemez.
Gerçek anahtar yazıldığı anda doğrulama otomatik olarak zorunlu olur — `.minisig` yoksa
`signature-missing`, imza tutmuyorsa `signature-verification-failed` döner ve indirilen
dosya diskten silinir.

## 2. CI'da imzala

Repo secret'ları:

- `MINISIGN_SECRET_KEY` — `wmole.key` dosyasının içeriği
- `MINISIGN_PASSWORD` — anahtar parolası

Release job'ında installer üretildikten sonra:

```sh
printf '%s' "$MINISIGN_SECRET_KEY" > minisign.key
echo "$MINISIGN_PASSWORD" | minisign -S -s minisign.key -m wmole-X.Y.Z-setup.exe
sha256sum wmole-X.Y.Z-setup.exe > wmole-X.Y.Z-setup.exe.sha256
rm -f minisign.key
```

Üretilen `wmole-X.Y.Z-setup.exe.minisig` ve `wmole-X.Y.Z-setup.exe.sha256` dosyaları
release asset'i olarak yüklenmelidir. Uygulama yardımcı dosya adlarını setup asset'inin
adından türetir (`<setup adı> + ".minisig"` / `+ ".sha256"`), bu yüzden adlandırma birebir
olmalıdır.

## 3. Yerel doğrulama

```sh
minisign -Vm wmole-X.Y.Z-setup.exe -P "RWQ...<public key>"
```
