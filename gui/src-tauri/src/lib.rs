use std::sync::Mutex;
use std::time::Duration;
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Emitter, Manager, State,
};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

mod updater;

struct Sidecar(Mutex<Option<CommandChild>>);

/// Sidecar çıktısı webview'e iletilecek sentetik olay: süreç beklenmedik şekilde öldü.
const SIDECAR_EXITED: &str =
    r#"{"id":null,"ev":"error","code":"sidecar_exited","message":"sidecar process exited"}"#;

/// Sidecar stdout'unu NDJSON satırlarına birleştirip webview'e ilet.
/// Uzun satırlar birden çok okumaya bölünebildiğinden '\n'e kadar tamponlanır;
/// süreç çıkışında kalan kısmi satır da flush edilir.
async fn pump_sidecar(handle: &tauri::AppHandle, mut rx: tauri::async_runtime::Receiver<CommandEvent>) {
    let mut buf = String::new();
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                buf.push_str(&String::from_utf8_lossy(&bytes));
                while let Some(pos) = buf.find('\n') {
                    let line: String = buf.drain(..=pos).collect();
                    let line = line.trim_end();
                    if !line.trim().is_empty() {
                        let _ = handle.emit("sidecar-event", line.to_string());
                    }
                }
            }
            CommandEvent::Stderr(bytes) => {
                eprintln!("wmole sidecar stderr: {}", String::from_utf8_lossy(&bytes).trim_end());
            }
            CommandEvent::Error(err) => {
                eprintln!("wmole sidecar error: {err}");
            }
            _ => {}
        }
    }
    if !buf.trim().is_empty() {
        let _ = handle.emit("sidecar-event", buf.trim().to_string());
    }
}

#[tauri::command]
fn send_request(state: State<Sidecar>, line: String) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|e| e.to_string())?;
    let child = guard.as_mut().ok_or("sidecar not started")?;
    child
        .write((line + "\n").as_bytes())
        .map_err(|e| e.to_string())?;
    Ok(())
}

/// Mevcut uygulamayı yönetici (UAC) olarak yeniden başlat, sonra mevcut örneği kapat.
#[tauri::command]
fn relaunch_admin(app: tauri::AppHandle) -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    // PowerShell single-quoted strings escape a literal quote by doubling it.
    let exe_str = exe.to_string_lossy().replace('\'', "''");
    std::process::Command::new("powershell")
        .args([
            "-WindowStyle",
            "Hidden",
            "-Command",
            &format!("Start-Process -FilePath '{}' -Verb RunAs", exe_str),
        ])
        .spawn()
        .map_err(|e| e.to_string())?;
    app.exit(0);
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .manage(Sidecar(Mutex::new(None)))
        .setup(|app| {
            // ── Python sidecar ──
            // Süreç beklenmedik şekilde ölürse webview'e bildirilir ve kısa bir
            // bekleme sonrası en fazla 3 kez yeniden başlatılır (kendini onaran).
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                const MAX_ATTEMPTS: u32 = 3;
                let mut attempts: u32 = 0;
                loop {
                    let spawned = handle
                        .shell()
                        .sidecar("wmole")
                        .ok()
                        .and_then(|cmd| cmd.args(["serve"]).spawn().ok());
                    match spawned {
                        Some((rx, child)) => {
                            attempts = 0;
                            handle.state::<Sidecar>().0.lock().unwrap().replace(child);
                            pump_sidecar(&handle, rx).await;
                            handle.state::<Sidecar>().0.lock().unwrap().take();
                            let _ = handle.emit("sidecar-event", SIDECAR_EXITED.to_string());
                        }
                        None => {
                            attempts += 1;
                            if attempts >= MAX_ATTEMPTS {
                                eprintln!("wmole sidecar: giving up after {attempts} failed spawn attempts");
                                break;
                            }
                        }
                    }
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            });

            // ── Sistem tepsisi (tray) ──
            let show = MenuItem::with_id(app, "show", "Göster", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Çıkış", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .tooltip("wmole")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            send_request,
            relaunch_admin,
            updater::check_update,
            updater::download_update,
            updater::install_update
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
