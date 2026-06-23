package com.aiblackbox.portal

import android.app.Application
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import com.aiblackbox.portal.data.remote.RemoteTaskRunner
import com.aiblackbox.portal.data.remote.remoteTaskHandlerFactory
import com.aiblackbox.portal.data.voice.AudioPlaybackManager
import com.aiblackbox.portal.ui.cli_agent.TerminalSessionManager

/**
 * Process-wide startup wiring. Registers the control_phone remote task handler
 * factory so LocalModelService's inbound listener can run remote tasks via the
 * on-device Gemma. The factory is null until set here, which is what keeps the
 * listener OFF until the allowlist-enforcing runner is deliberately wired (Task 6).
 */
class PortalApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        remoteTaskHandlerFactory = { ctx -> RemoteTaskRunner(ctx) }

        // Wire the process-lived terminal-session manager to the Application context
        // so it can drive the TerminalForegroundService (Phase 3): start the FGS when
        // the first terminal opens and stop it when the last is killed, keeping live
        // terminal WebSockets warm while the app is backgrounded.
        TerminalSessionManager.init(this)

        // Pause the playback Visualizer's audio-output capture while the whole
        // app is backgrounded (saves CPU + avoids a background output tap with
        // no on-screen consumer). Playback itself is unaffected; capture resumes
        // on return to foreground if still playing.
        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStop(owner: LifecycleOwner) { AudioPlaybackManager.onAppBackground() }
            override fun onStart(owner: LifecycleOwner) { AudioPlaybackManager.onAppForeground() }
        })
    }
}
