use std::sync::Mutex;
use tauri::{Emitter, Manager, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

struct Sidecar(Mutex<Option<CommandChild>>);

#[tauri::command]
fn send_request(state: State<Sidecar>, line: String) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|e| e.to_string())?;
    let child = guard.as_mut().ok_or("sidecar not started")?;
    child
        .write((line + "\n").as_bytes())
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .manage(Sidecar(Mutex::new(None)))
        .setup(|app| {
            let sidecar = app.shell().sidecar("wmole").unwrap().args(["serve"]);
            let (mut rx, child) = sidecar.spawn().expect("failed to spawn sidecar");
            app.state::<Sidecar>().0.lock().unwrap().replace(child);

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Stdout(bytes) = event {
                        let text = String::from_utf8_lossy(&bytes);
                        for line in text.lines() {
                            if line.trim().is_empty() {
                                continue;
                            }
                            let _ = handle.emit("sidecar-event", line.to_string());
                        }
                    }
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![send_request])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
