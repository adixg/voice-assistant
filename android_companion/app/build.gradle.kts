plugins {
    id("com.android.application")
    // No org.jetbrains.kotlin.android -- AGP 9's built-in Kotlin support
    // covers it; see the note in the root build.gradle.kts.
}

android {
    namespace = "com.aigf.blespike"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.aigf.blespike"
        // 31 (Android 12) is where the BLUETOOTH_SCAN/BLUETOOTH_CONNECT
        // runtime permission model starts -- see AndroidManifest.xml. No
        // need to support anything older for a throwaway spike targeting
        // one specific phone (Galaxy A36, Android 16 / SDK 36).
        minSdk = 31
        targetSdk = 36
        versionCode = 1
        versionName = "phase2-spike"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    // kotlinOptions{} belonged to the old org.jetbrains.kotlin.android plugin
    // we just removed; AGP 9's built-in Kotlin support derives jvmTarget
    // from compileOptions above.
}

dependencies {
    // Deliberately minimal -- BLE central APIs (android.bluetooth.le.*) are
    // part of the Android framework itself, no library needed. Plain
    // View/findViewById, no Compose, to keep this spike's build fast and
    // its dependency surface small.
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
