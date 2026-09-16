// android_companion/ — the phone-side half of the BLE transport migration
// (see /home/aditya/.claude/plans/tranquil-drifting-stream.md, Phase 5).
// Right now this holds only Phase 2's throughput-spike test app; the real
// BLE-central + Tailscale-WebSocket-bridge app lands here later as its own
// module once Phase 2/3 are validated.
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "aigf-android-companion"
include(":app")
