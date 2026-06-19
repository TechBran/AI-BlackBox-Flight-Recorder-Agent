package com.aiblackbox.portal

import android.app.Application
import com.aiblackbox.portal.data.remote.RemoteTaskRunner
import com.aiblackbox.portal.data.remote.remoteTaskHandlerFactory

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
    }
}
