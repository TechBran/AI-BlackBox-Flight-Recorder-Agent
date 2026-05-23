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
            // Drop fullscreen + always_on_top + no-decorations kiosk mode:
            // Computer Use agent needs full PC access (full screen + taskbar +
            // ability to bring other windows to front), and customer needs an
            // escape hatch (X button, Alt+F4) which decorations provide.
            // Setup mode opens MAXIMIZED so the wizard still feels prominent
            // on first-run, but is escapable. Manage mode opens default-sized.
            .maximized(is_setup_mode)
            // External-link interception (Brandon's E6 ask 2026-05-16): WebKitGTK
            // doesn't auto-delegate target=_blank to system browser. on_navigation
            // fires on every navigation attempt; we allow same-origin (the wizard
            // itself) and delegate everything else to the system default browser.
            // Single point of control — every future wizard step's external links
            // "just work" with bare <a target=_blank>. Maintainer-blessed pattern
            // per Tauri GitHub #11479 + #14113.
            //
            // E7 (Brandon hardware test 2026-05-16): tauri-plugin-opener::open_url
            // on Linux uses xdg-open which on Ubuntu 24.04 GNOME delegates to
            // `gio open`, which fails with "HTTP Error: Method Not Allowed" for
            // URL schemes (gio tries to HTTP-fetch the URL instead of dispatching
            // to a browser). The xdg-open exit code masks the failure so the
            // opener plugin returns Ok and we never see the error. Workaround:
            // invoke firefox directly via PATH lookup (always present on Ubuntu
            // Desktop installs as the snap-transitional /usr/bin/firefox), fall
            // back to opener plugin if firefox not on PATH. Subprocess inherits
            // the wizard's DISPLAY/XAUTHORITY/DBUS env so firefox renders cleanly
            // in the customer's session.
            .on_navigation(|url| {
                let is_internal = url.scheme() == "http"
                    && url.host_str() == Some("localhost")
                    && url.port() == Some(9091);
                if is_internal {
                    return true;  // allow in-webview load
                }
                let url_str = url.as_str();
                // E7 enhanced: explicitly set DBUS_SESSION_BUS_ADDRESS +
                // XDG_RUNTIME_DIR before spawning firefox. The wizard's
                // inherited env from desktop launch may be minimal (Tauri
                // appears to filter it down to DISPLAY+XAUTHORITY only),
                // but the snap-confined firefox needs DBUS to negotiate
                // with snapd's confinement layer. Computing these from
                // the current UID is portable across customer setups —
                // /run/user/<uid>/{bus,*} is the systemd-user-session
                // convention on all modern Linux distros.
                let uid = unsafe { libc::getuid() };
                let xdg_runtime = std::env::var("XDG_RUNTIME_DIR")
                    .unwrap_or_else(|_| format!("/run/user/{}", uid));
                let dbus_addr = std::env::var("DBUS_SESSION_BUS_ADDRESS")
                    .unwrap_or_else(|_| format!("unix:path={}/bus", xdg_runtime));
                // Try browsers in apt-first order so we prefer deterministic
                // launches (chromium-browser, google-chrome from apt) over
                // snap firefox, whose snap-confine commonly fails under non-
                // user-session cgroups returning rc=1 immediately. `.spawn()`
                // only reports fork+exec failure (not subsequent immediate
                // exits), so we PROBE each candidate with --version first;
                // an apt binary returns rc=0 with version string, a broken
                // snap binary exits non-zero, and we skip to the next.
                let mut opened = false;
                for browser in &["chromium-browser", "chromium", "google-chrome", "firefox"] {
                    let probe = std::process::Command::new(browser)
                        .arg("--version")
                        .env("DBUS_SESSION_BUS_ADDRESS", &dbus_addr)
                        .env("XDG_RUNTIME_DIR", &xdg_runtime)
                        .stdout(std::process::Stdio::null())
                        .stderr(std::process::Stdio::null())
                        .status();
                    if !matches!(probe, Ok(s) if s.success()) {
                        continue;
                    }
                    if std::process::Command::new(browser)
                        .arg(url_str)
                        .env("DBUS_SESSION_BUS_ADDRESS", &dbus_addr)
                        .env("XDG_RUNTIME_DIR", &xdg_runtime)
                        .stdout(std::process::Stdio::null())
                        .stderr(std::process::Stdio::null())
                        .spawn()
                        .is_ok()
                    {
                        opened = true;
                        break;
                    }
                }
                if !opened {
                    // Fall back to the opener plugin (xdg-open) only if no
                    // browser binary was viable. Note: on Ubuntu Desktop with
                    // drifted xdg-mime defaults this fallback routes to
                    // gnome-text-editor — that's a known failure mode and
                    // why we exhaust direct-spawn first.
                    if let Err(e) = tauri_plugin_opener::open_url(url_str, None::<&str>) {
                        eprintln!("[blackbox-setup] failed to open external URL {url}: {e}");
                    }
                }
                false  // cancel in-webview load; system browser handles it
            })
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
