// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn wait_for_server(url: &str, timeout_secs: u64) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed().as_secs() < timeout_secs {
        if reqwest::blocking::get(url).map(|r| r.status().is_success()).unwrap_or(false) {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    false
}

fn main() {
    if !wait_for_server("http://localhost:9091/health", 180) {
        eprintln!("Orchestrator failed to come up within 180s — aborting blackbox-setup launch");
        std::process::exit(1);
    }
    eprintln!("[blackbox-setup] BlackBox Orchestrator is healthy; determining launch mode");

    // T3.5.2: Mode detection.
    // --first-run flag from autostart .desktop → setup mode (fullscreen, no decorations).
    // No flag (manual launch from persistent .desktop) → check is_complete:
    //   - true  → manage mode (windowed, decorations on, ?mode=manage)
    //   - false → setup mode (legitimate first-run, autostart was missed)
    let args: Vec<String> = std::env::args().collect();
    let first_run = args.iter().any(|a| a == "--first-run");
    let mode = if first_run {
        "setup"
    } else {
        let state = installer_lib::probe_state();
        if state["is_complete"].as_bool().unwrap_or(false) { "manage" } else { "setup" }
    };
    let url = format!("http://localhost:9091/onboarding/?mode={}", mode);
    eprintln!("[blackbox-setup] launching mode={mode} url={url}");

    // Hand off to the Tauri builder in lib.rs, which constructs the webview
    // programmatically with mode-aware settings (fullscreen for setup,
    // windowed+decorated for manage). The on_window_event close-handler in
    // lib.rs is preserved.
    installer_lib::run_with_url(&url, mode);
}
