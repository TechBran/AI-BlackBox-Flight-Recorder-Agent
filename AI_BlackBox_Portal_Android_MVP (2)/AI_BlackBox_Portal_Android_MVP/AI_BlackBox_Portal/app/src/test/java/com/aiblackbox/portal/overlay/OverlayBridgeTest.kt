package com.aiblackbox.portal.overlay

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (M6.3) Unit tests for the pure [OverlayBridge] seam that drives the XR consent surface:
 * the additive `controlSessionActive` state flag (rendered by the in-headset panel banner) and
 * the `stopRemoteControl` kill-switch command (routed by the Service to `RemoteSessionBus.stop`).
 * Framework-free (StateFlow + a plain listener), so it runs on the JVM without Android.
 */
class OverlayBridgeTest {

    /** Records which commands the bridge dispatched to the registered listener. */
    private class RecordingListener : OverlayBridge.CommandListener {
        var stopCalls = 0
        override fun toggleConnect() {}
        override fun toggleMic() {}
        override fun toggleScreenShare() {}
        override fun toggleExpanded() {}
        override fun selectModel(model: String) {}
        override fun selectVoice(voice: String) {}
        override fun selectOperator(operator: String) {}
        override fun openCamera() {}
        override fun openPortal() {}
        override fun minimize() {}
        override fun stopRemoteControl() { stopCalls++ }
    }

    @Test fun `controlSessionActive defaults false`() {
        assertFalse(OverlayBridge.OverlayState().controlSessionActive)
    }

    @Test fun `updateState surfaces controlSessionActive for the panel banner`() {
        OverlayBridge.reset()
        OverlayBridge.updateState { it.copy(controlSessionActive = true) }
        assertTrue(OverlayBridge.state.value.controlSessionActive)
        OverlayBridge.updateState { it.copy(controlSessionActive = false) }
        assertFalse(OverlayBridge.state.value.controlSessionActive)
    }

    @Test fun `stopRemoteControl dispatches to the registered command listener`() {
        OverlayBridge.reset()
        val listener = RecordingListener()
        OverlayBridge.registerCommandListener(listener)
        OverlayBridge.stopRemoteControl()
        assertEquals("STOP must reach the Service's kill-switch handler", 1, listener.stopCalls)
        OverlayBridge.unregisterCommandListener()
        // After unregister the command is a safe no-op (no crash, no dispatch).
        OverlayBridge.stopRemoteControl()
        assertEquals(1, listener.stopCalls)
    }
}
