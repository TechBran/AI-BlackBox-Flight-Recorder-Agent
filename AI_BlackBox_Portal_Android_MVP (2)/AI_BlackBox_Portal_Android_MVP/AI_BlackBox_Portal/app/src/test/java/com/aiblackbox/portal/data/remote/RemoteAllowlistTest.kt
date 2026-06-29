package com.aiblackbox.portal.data.remote

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RemoteAllowlistTest {

    @Test fun safe_device_actions_are_allowed() {
        for (name in listOf(
            "read_screen", "tap", "swipe", "scroll", "open_app", "back", "home",
            "show_map", "flashlight_on", "flashlight_off", "open_url",
            "open_wifi_settings", "open_settings_panel", "take_photo", "set_timer",
        )) {
            assertTrue("expected '$name' allowed", RemoteAllowlist.isAllowedRemote(name))
        }
    }

    @Test fun high_consequence_and_outbound_actions_are_refused() {
        for (name in listOf(
            "send_sms", "send_email", "dial", "create_contact",
            "create_calendar_event", "set_alarm", "share_text",
        )) {
            assertFalse("expected '$name' refused", RemoteAllowlist.isAllowedRemote(name))
        }
    }

    @Test fun type_actuator_is_refused() {
        // `type` composes arbitrary text (could fill a Send field) -> refused remotely.
        assertFalse(RemoteAllowlist.isAllowedRemote("type"))
    }

    @Test fun cloud_bridge_tools_are_refused() {
        assertFalse(RemoteAllowlist.isAllowedRemote("find_blackbox_tool"))
        assertFalse(RemoteAllowlist.isAllowedRemote("run_blackbox_tool"))
        assertFalse(RemoteAllowlist.isAllowedRemote("search_tools"))
        // web_search is now a HEADLESS cloud bridge tool (not a device action) -> refused
        // for device-only remote control.
        assertFalse(RemoteAllowlist.isAllowedRemote("web_search"))
    }

    @Test fun unknown_tool_is_default_denied() {
        assertFalse(RemoteAllowlist.isAllowedRemote("rm_minus_rf"))
        assertFalse(RemoteAllowlist.isAllowedRemote(""))
    }
}
