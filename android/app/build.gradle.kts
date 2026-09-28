import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
}

// Release signing: create android/keystore.properties (gitignored) with
//   storeFile=/path/to/lantooth.jks
//   storePassword=...
//   keyAlias=...
//   keyPassword=...
// App version: single source of truth is the repo-root VERSION file (x.y.z).
// versionCode = x*10000 + y*100 + z, so it always increases with the version.
val appVersion = rootProject.file("../VERSION").readText().trim()
val appVersionCode = appVersion.split(".").map { it.toInt() }.let { (major, minor, patch) ->
    major * 10_000 + minor * 100 + patch
}

val keystoreProps = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "com.lantooth"
    compileSdk = 37
    // Native libopus (src/main/cpp). Gradle downloads this NDK/CMake on first build.
    ndkVersion = "30.0.16248370"

    defaultConfig {
        applicationId = "com.lantooth"
        minSdk = 28
        targetSdk = 36
        versionCode = appVersionCode
        versionName = appVersion

        ndk {
            // 64/32-bit ARM phones + x86_64 emulators
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64")
        }
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            // 3.x: libopus' CMake files predate CMake 4's removal of old-policy compat
            version = "3.31.6"
        }
    }

    signingConfigs {
        if (keystoreProps.containsKey("storeFile")) {
            create("release") {
                storeFile = file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfigs.findByName("release")?.let { signingConfig = it }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.material)
    implementation(libs.constraintlayout)
    implementation(libs.androidx.media)
    implementation(libs.tink.android)
    implementation(libs.coroutines.android)
}
