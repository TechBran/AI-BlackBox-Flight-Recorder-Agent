# Keep JavaScript bridge interfaces (Portal WebView communication)
-keepclassmembers class com.aiblackbox.portal.PortalActivity$WebAppInterface {
    @android.webkit.JavascriptInterface <methods>;
}
-keepclassmembers class com.aiblackbox.portal.PortalActivity$FilePickerInterface {
    @android.webkit.JavascriptInterface <methods>;
}

# Keep any class with @JavascriptInterface methods (future-proof)
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}

# Keep the device-control package whole: its services and activities, the actuators, and the
# @Serializable posture, UI-tree and capability payloads it sends to the BlackBox. No code
# here uses reflection since the androidx.xr alphas were dropped (audit
# X02-deps-android-web-06); narrowing this rule is a release-build change that needs a
# device walk.
-keep class com.aiblackbox.portal.overlay.** { *; }

# Keep data classes used in JSON serialization
-keep class com.aiblackbox.portal.models.** { *; }
