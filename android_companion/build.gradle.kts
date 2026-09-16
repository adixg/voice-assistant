// AGP 9.0.1 pairs with Gradle 9.x (the installed conda gradle is 9.3.1).
// No separate Kotlin plugin: AGP 9 ships built-in Kotlin support and
// registers its own `kotlin` extension, so applying
// org.jetbrains.kotlin.android on top collides ("Cannot add extension with
// name 'kotlin', as there is an extension already registered with that
// name") -- hit this on the first build, confirmed against AGP 9's own
// migration notes (developer.android.com/build/migrate-to-built-in-kotlin).
plugins {
    id("com.android.application") version "9.0.1" apply false
}
