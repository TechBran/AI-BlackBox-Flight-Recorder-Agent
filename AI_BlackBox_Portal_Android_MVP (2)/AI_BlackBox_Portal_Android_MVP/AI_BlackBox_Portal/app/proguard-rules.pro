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

# Keep XR overlay classes (uses reflection for Spatial APIs)
-keep class com.aiblackbox.portal.overlay.** { *; }

# Keep data classes used in JSON serialization
-keep class com.aiblackbox.portal.models.** { *; }

# (M8.5) Android XR SplitEngine references the platform XR system-extension classes
# (com.android.extensions.xr.*), which are provided by the XR device runtime, NOT bundled in
# the APK — so R8 sees them as "missing classes" and fails the release minify. They are only
# reached on an XR headset at runtime; suppress the warnings (the same rules R8 generated in
# build/outputs/mapping/release/missing_rules.txt). Sideload/enterprise release build (M8.5).
-dontwarn com.android.extensions.xr.**
