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
