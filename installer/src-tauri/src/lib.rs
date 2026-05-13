// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

/// 2-second-timeout state probe used by both main.rs (mode detection) and
/// the close-event handler below. Returns a Value with safe-default
/// is_complete:false if the request fails. Folds in the T3.5.1 reviewer's
/// timeout fix (default reqwest::blocking::get is ~30s, which would freeze
/// the GUI on shutdown if Orchestrator hangs). Onboarding state is a
/// local-loopback request — 2s is generous.
pub fn probe_state() -> serde_json::Value {
    let client = match reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(_) => return serde_json::json!({"is_complete": false}),
    };
    client
        .get("http://localhost:9091/onboarding/state")
        .send()
        .and_then(|r| r.json())
        .unwrap_or_else(|_| serde_json::json!({"is_complete": false}))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run_with_url(url: &str, mode: &str) {
    let url_owned = url.to_string();
    let is_setup_mode = mode == "setup";

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![greet])
        .setup(move |app| {
            // Tauri 2.x API: WebviewWindowBuilder + WebviewUrl::External
            // (NOT v1's WindowBuilder + WindowUrl — original plan example
            // used v1 names; corrected in audit commit 6066900.)
            let _window = tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::External(url_owned.parse().expect("invalid url")),
            )
            .title("AI BlackBox Setup")
            .inner_size(1280.0, 800.0)
            .fullscreen(is_setup_mode)       // fullscreen for first-run, windowed for manage
            .decorations(!is_setup_mode)     // no decorations for first-run, decorated for manage
            .always_on_top(is_setup_mode)    // always-on-top only for first-run
            .build()?;
            Ok(())
        })
        .on_window_event(|_window, event| {
            // T3.5.1: When user closes the Tauri window after completing onboarding
            // (clicked "Open Portal" in done.js → window closes), check is_complete.
            // If true, remove ONLY the autostart .desktop. The persistent launcher
            // in ~/.local/share/applications/ stays so user can re-launch for
            // credential management (manage mode).
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let state = probe_state();
                if state["is_complete"].as_bool().unwrap_or(false) {
                    let autostart = dirs::config_dir()
                        .map(|d| d.join("autostart").join("blackbox-setup.desktop"));
                    if let Some(p) = autostart {
                        match std::fs::remove_file(&p) {
                            Ok(_) => eprintln!("[blackbox-setup] removed autostart entry at {}; persistent launcher in ~/.local/share/applications/ remains for re-launch", p.display()),
                            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                                // First run on a dev box may have no autostart yet — that's fine.
                                eprintln!("[blackbox-setup] no autostart entry to remove (already absent or never installed)");
                            }
                            Err(e) => eprintln!("[blackbox-setup] failed to remove autostart entry: {e}"),
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
