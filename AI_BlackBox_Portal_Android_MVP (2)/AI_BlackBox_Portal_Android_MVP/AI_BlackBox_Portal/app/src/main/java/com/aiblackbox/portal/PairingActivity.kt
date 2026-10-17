package com.aiblackbox.portal

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.google.zxing.integration.android.IntentIntegrator
import org.json.JSONObject

class PairingActivity : AppCompatActivity() {
    private val prefs by lazy { getSharedPreferences("bbx_prefs", MODE_PRIVATE) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // A blank origin is unpaired too (PortalActivity bounces a blank one back here).
        if (currentLaunchTarget() != LaunchTarget.PAIRING) {
            launchMainActivity()
            finish(); return
        }

        setContentView(R.layout.activity_pairing)

        // Enable edge-to-edge display (transparent status and navigation bars)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.setDecorFitsSystemWindows(false)
            window.statusBarColor = android.graphics.Color.TRANSPARENT
            window.navigationBarColor = android.graphics.Color.TRANSPARENT
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (
                android.view.View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            )
            window.statusBarColor = android.graphics.Color.TRANSPARENT
            window.navigationBarColor = android.graphics.Color.TRANSPARENT
        }

        findViewById<Button>(R.id.btnScan).setOnClickListener {
            val integrator = IntentIntegrator(this)
            integrator.setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
            integrator.setPrompt("Scan AI BlackBox pairing QR")
            integrator.setBeepEnabled(false)
            integrator.setOrientationLocked(false)
            integrator.initiateScan()
        }
    }

    override fun onActivityResult(reqCode: Int, resCode: Int, data: Intent?) {
        val result = IntentIntegrator.parseActivityResult(reqCode, resCode, data)
        if (result != null && result.contents != null) {
            try {
                val json = JSONObject(result.contents)
                val origin = json.optString("origin")
                if (origin.isNullOrBlank()) {
                    Toast.makeText(this, "Invalid QR: missing origin", Toast.LENGTH_SHORT).show()
                } else {
                    prefs.edit().putString("origin", origin).apply()
                    json.optString("operator").takeIf { it.isNotBlank() }?.let {
                        prefs.edit().putString("operator", it).apply()
                    }
                    launchMainActivity()
                    finish(); return
                }
            } catch (e: Exception) {
                Toast.makeText(this, "Invalid pairing QR", Toast.LENGTH_SHORT).show()
            }
        }
        super.onActivityResult(reqCode, resCode, data)
    }

    private fun launchMainActivity() {
        // The shared launch rule (LaunchRouting.kt) — a notification tap follows the same one.
        val target = currentLaunchTarget()
        if (target == LaunchTarget.PAIRING) return
        startActivity(Intent(this, target.activityClass()))
    }
}
