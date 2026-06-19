package com.aiblackbox.portal.data.remote

/**
 * The REMOTE-control allowlist (control_phone). When a frontier model drives the
 * on-device Gemma remotely (no user present to confirm), only these SAFE device
 * actions run; everything else is REFUSED. This set IS the blast radius —
 * **default-deny**: any tool not explicitly listed (unknown names, the
 * credential-bearing `type`, and every high-consequence / outbound action) is
 * refused for remote control.
 *
 * Names mirror [com.aiblackbox.portal.data.local.ResidentTools] (the on-device tool
 * catalog). This is the validated design's safe/refused split verbatim
 * (docs/plans/2026-06-18-frontier-to-phone-control-design.md).
 *
 * Residual-risk note: `tap`/`swipe`/`scroll` are GENERIC actuators — in principle a
 * UI path could reach a high-consequence control (e.g. tapping a "Send" button).
 * Mitigations: the direct high-consequence INTENTS (send_sms/send_email/dial/...)
 * are refused here; `type` (composing arbitrary text) is refused; the remote
 * actuator runs with the credential handoff DECLINED so password entry can never
 * proceed; and operator-scope auth (Task 8) + the Tailscale perimeter bound WHO can
 * reach the listener at all.
 */
object RemoteAllowlist {

    /** Safe device actions that run remotely (YOLO, no confirm). Exactly the design's set. */
    val SAFE_REMOTE: Set<String> = setOf(
        // Navigation / inspection actuators. NOTE: `type` is deliberately EXCLUDED —
        // composing arbitrary text is the riskiest actuator.
        "read_screen", "tap", "swipe", "scroll", "open_app", "back", "home",
        // Parameterized, non-destructive intent actions.
        "show_map", "flashlight_on", "flashlight_off", "open_url",
        "open_wifi_settings", "open_settings_panel", "take_photo", "set_timer",
        "web_search",
    )

    /** True iff [toolName] is safe to run under remote control. Default-deny. */
    fun isAllowedRemote(toolName: String): Boolean = toolName in SAFE_REMOTE
}
