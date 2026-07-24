use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::Write;
use tauri::{AppHandle, Emitter, Manager};

const GH_REPO: &str = "palamut62/wmole";
const UA: &str = "wmole-updater";
const RELEASE_URL_PREFIX: &str = "https://github.com/palamut62/wmole/releases/download/";

/// Minisign (ed25519) public key — güncelleme paketlerinin gerçekliğini doğrular.
///
/// TODO: Buraya gerçek public key'i yaz. Üretmek için: `minisign -G -p wmole.pub -s wmole.key`
/// ardından `wmole.pub` dosyasının İKİNCİ satırını (base64 gövde, "RWQ..." ile başlar)
/// buraya kopyala. Secret key CI'da `MINISIGN_KEY` secret'ı olarak tutulur ve release
/// job'ı `minisign -S -s wmole.key -m wmole-X.Y.Z-setup.exe` ile `.minisig` üretir.
/// Detay: README "Güncelleme imzalama" bölümü.
///
/// Placeholder olduğu sürece (aşağıdaki `PUBKEY_PLACEHOLDER` sabiti ile aynı) imza
/// doğrulaması ZORUNLU DEĞİLDİR: kullanıcıya "update-unsigned-warning" event'i emit edilir
/// ama akış bozulmaz. Gerçek anahtar yazıldığı an doğrulama otomatik olarak zorunlu olur
/// (derleme zamanı `cfg` yerine çalışma zamanı karşılaştırması, böylece unutulan bir
/// feature flag yüzünden doğrulama sessizce kapalı kalamaz).
const UPDATE_PUBKEY: &str = "RWQPLACEHOLDER00000000000000000000000000000000000000000";
const PUBKEY_PLACEHOLDER: &str = "RWQPLACEHOLDER00000000000000000000000000000000000000000";

fn pubkey_is_placeholder() -> bool {
    UPDATE_PUBKEY == PUBKEY_PLACEHOLDER || UPDATE_PUBKEY.trim().is_empty()
}

fn validate_release_url(url: &str, expected_suffix: &str) -> Result<(), String> {
    if url.starts_with(RELEASE_URL_PREFIX) && url.ends_with(expected_suffix) {
        Ok(())
    } else {
        Err("invalid-update-url".into())
    }
}

#[derive(Deserialize)]
struct GhAsset {
    name: String,
    browser_download_url: String,
    size: u64,
}

#[derive(Deserialize)]
struct GhRelease {
    tag_name: String,
    body: String,
    #[serde(default)]
    prerelease: bool,
    assets: Vec<GhAsset>,
}

#[derive(Serialize, Clone)]
pub struct UpdateInfo {
    available: bool,
    current: String,
    latest: String,
    notes: String,
    download_url: String,
    sha256_url: String,
    sig_url: String,
    size: u64,
}

#[derive(Serialize, Clone)]
struct Progress {
    downloaded: u64,
    total: u64,
    pct: u8,
}

/// GitHub Releases API ile en son sürümü kontrol et.
/// Hata = ağ/parse sorunu (UI'de toast'lanır).
#[tauri::command]
pub async fn check_update(app: AppHandle) -> Result<UpdateInfo, String> {
    let current = app.package_info().version.to_string();
    let url = format!("https://api.github.com/repos/{GH_REPO}/releases/latest");

    let rel: GhRelease = reqwest::Client::new()
        .get(&url)
        .header("User-Agent", UA)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("network: {e}"))?
        .error_for_status()
        .map_err(|e| format!("http: {e}"))?
        .json()
        .await
        .map_err(|e| format!("parse: {e}"))?;

    let latest = rel.tag_name.trim_start_matches('v').to_string();

    // semver ile karşılaştır; parse edilemezse string eşitliğine düş.
    let newer = match (
        semver::Version::parse(&latest),
        semver::Version::parse(&current),
    ) {
        (Ok(l), Ok(c)) => l > c,
        _ => latest != current,
    };

    // -setup.exe asset'i + AYNI dosyaya ait .sha256 / .minisig yardımcı dosyaları.
    // (Eskiden ilk ".sha256" ile biten asset alınıyordu; birden fazla asset olan
    //  release'lerde yanlış dosyanın hash'i çekilebiliyordu — adı setup'tan türetiyoruz.)
    let setup = rel.assets.iter().find(|a| a.name.ends_with("-setup.exe"));
    let (setup_name, download_url, size) = match setup {
        Some(a) => (a.name.clone(), a.browser_download_url.clone(), a.size),
        None => (String::new(), String::new(), 0),
    };

    let find_named = |suffix: &str| -> String {
        if setup_name.is_empty() {
            return String::new();
        }
        let want = format!("{setup_name}{suffix}");
        rel.assets
            .iter()
            .find(|a| a.name == want)
            .map(|a| a.browser_download_url.clone())
            .unwrap_or_default()
    };
    let sha256_url = find_named(".sha256");
    let sig_url = find_named(".minisig");

    // sha256 dosyası yoksa indirme zaten "invalid-update-url" ile patlardı;
    // güncellemeyi "mevcut" göstermek yerine baştan eleriz.
    Ok(UpdateInfo {
        available: newer && !rel.prerelease && !download_url.is_empty() && !sha256_url.is_empty(),
        current,
        latest,
        notes: rel.body,
        download_url,
        sha256_url,
        sig_url,
        size,
    })
}

/// Setup.exe'yi indir → ilerleme emit et → SHA-256 (ucuz erken eleme) →
/// minisign/ed25519 imza doğrula → dosya yolunu döndür.
#[tauri::command]
pub async fn download_update(
    app: AppHandle,
    url: String,
    sha256_url: String,
    sig_url: String,
    total: u64,
) -> Result<String, String> {
    validate_release_url(&url, "-setup.exe")?;
    validate_release_url(&sha256_url, ".sha256")?;
    if !sig_url.is_empty() {
        validate_release_url(&sig_url, ".minisig")?;
    }
    let dir = app.path().temp_dir().map_err(|e| format!("disk: {e}"))?;
    let target = dir.join("wmole-update-setup.exe");

    let client = reqwest::Client::new();
    let resp = client
        .get(&url)
        .header("User-Agent", UA)
        .send()
        .await
        .map_err(|e| format!("network: {e}"))?
        .error_for_status()
        .map_err(|e| format!("http: {e}"))?;

    // yetersiz alan / izin hatası burada yakalanır
    let mut file = std::fs::File::create(&target).map_err(|e| format!("disk: {e}"))?;
    let mut hasher = Sha256::new();
    let mut downloaded: u64 = 0;
    let mut last_pct: u8 = 255;
    let mut stream = resp.bytes_stream();

    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("stream: {e}"))?;
        file.write_all(&chunk).map_err(|e| format!("disk: {e}"))?; // disk dolu → hata
        hasher.update(&chunk);
        downloaded += chunk.len() as u64;
        let pct = if total > 0 {
            ((downloaded * 100 / total) as u8).min(100)
        } else {
            0
        };
        if pct != last_pct {
            last_pct = pct;
            let _ = app.emit(
                "update-progress",
                Progress {
                    downloaded,
                    total,
                    pct,
                },
            );
        }
    }
    file.flush().ok();
    drop(file);

    if total > 0 && downloaded != total {
        let _ = std::fs::remove_file(&target);
        return Err(format!("size-mismatch:{downloaded}:{total}"));
    }

    // SHA-256 doğrulama (bozuk/eksik indirme tespiti)
    let got = format!("{:x}", hasher.finalize());
    let expected_raw = client
        .get(&sha256_url)
        .header("User-Agent", UA)
        .send()
        .await
        .map_err(|e| format!("network: {e}"))?
        .text()
        .await
        .map_err(|e| format!("network: {e}"))?;
    // .sha256 formatı: "<hash>  wmole-X.Y.Z-setup.exe"
    let expected = expected_raw
        .split_whitespace()
        .next()
        .unwrap_or("")
        .to_lowercase();

    if expected.is_empty() || got != expected {
        let _ = std::fs::remove_file(&target); // bozuk dosyayı sil
        return Err(format!("checksum-mismatch:{got}:{expected}"));
    }

    // --- Asimetrik imza doğrulaması (asıl güvenlik katmanı) ---
    // SHA-256 yalnızca bozuk indirmeyi yakalar; release'i ele geçiren biri hem exe'yi
    // hem .sha256'yı değiştirebilir. Minisign imzası secret key olmadan üretilemez.
    if pubkey_is_placeholder() {
        // TODO: Gerçek UPDATE_PUBKEY yazıldığında bu dal ölür ve imza zorunlu olur.
        let _ = app.emit("update-unsigned-warning", "update-signature-not-enforced");
    } else {
        let sig_url = if sig_url.is_empty() {
            let _ = std::fs::remove_file(&target);
            return Err("signature-missing".into());
        } else {
            sig_url
        };

        let sig_text = client
            .get(&sig_url)
            .header("User-Agent", UA)
            .send()
            .await
            .map_err(|e| format!("network: {e}"))?
            .error_for_status()
            .map_err(|e| format!("http: {e}"))?
            .text()
            .await
            .map_err(|e| format!("network: {e}"))?;

        let verified = (|| -> Result<(), String> {
            let pk = minisign_verify::PublicKey::from_base64(UPDATE_PUBKEY)
                .map_err(|e| format!("pubkey: {e}"))?;
            let sig = minisign_verify::Signature::decode(&sig_text)
                .map_err(|e| format!("sig-decode: {e}"))?;
            let data = std::fs::read(&target).map_err(|e| format!("disk: {e}"))?;
            pk.verify(&data, &sig, false)
                .map_err(|e| format!("verify: {e}"))
        })();

        if let Err(reason) = verified {
            // Doğrulanamayan installer'ı diskte bırakma.
            let _ = std::fs::remove_file(&target);
            eprintln!("update signature verification failed: {reason}");
            return Err("signature-verification-failed".into());
        }
    }

    Ok(target.to_string_lossy().to_string())
}

/// İndirilen Tauri NSIS setup.exe'yi sessizce çalıştır, uygulamayı kapat.
/// `/S` sessiz kurulum; NSIS şablonu çalışan örneği kapatır ve kurulum
/// sonrası uygulamayı yeniden başlatır.
#[tauri::command]
pub fn install_update(app: AppHandle, setup_path: String) -> Result<(), String> {
    let expected = app
        .path()
        .temp_dir()
        .map_err(|e| format!("disk: {e}"))?
        .join("wmole-update-setup.exe");
    let requested = std::fs::canonicalize(&setup_path).map_err(|_| "setup-missing")?;
    let expected = std::fs::canonicalize(expected).map_err(|_| "setup-missing")?;
    if requested != expected {
        return Err("invalid-setup-path".into());
    }
    if !requested.is_file() {
        return Err("setup-missing".into());
    }
    std::process::Command::new(&requested)
        .arg("/S")
        .spawn()
        .map_err(|e| format!("spawn: {e}"))?; // kurulum başlatılamadı
                                              // Dosya kilidini bırak; installer kurup uygulamayı yeniden başlatacak.
    app.exit(0);
    Ok(())
}
