# Tink and kotlinx-coroutines ship their own consumer R8 rules, and the default
# proguard-android-optimize.txt already keeps native (JNI) method names
# (NativeOpus). Nothing app-specific needs keeping.

# Release builds must not log PC names/IPs or handshake details to logcat.
-assumenosideeffects class android.util.Log {
    public static int d(...);
    public static int v(...);
}
