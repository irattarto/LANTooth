# Tink and kotlinx-coroutines ship their own consumer R8 rules, and the default
# proguard-android-optimize.txt already keeps native (JNI) method names
# (NativeOpus). Nothing app-specific needs keeping.
