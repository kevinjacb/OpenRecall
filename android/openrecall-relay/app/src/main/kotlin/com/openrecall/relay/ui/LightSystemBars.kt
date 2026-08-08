package com.openrecall.relay.ui

import android.graphics.Color
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge

/**
 * Go edge-to-edge with system bars pinned to the light treatment.
 *
 * The bare `enableEdgeToEdge()` defaults to [SystemBarStyle.auto], which picks
 * icon colour from the *system* dark-mode flag. Since the app is light-only
 * (see `core/ui/OpenRecallTheme.kt`), a phone in dark mode would get light
 * status- and navigation-bar icons drawn over our near-white canvas —
 * invisible. [SystemBarStyle.light] forces dark icons regardless.
 *
 * Both scrims are transparent so the canvas runs under the bars, which is the
 * point of going edge-to-edge. The second argument is the fallback scrim used
 * on API levels that can't draw dark icons on that bar; transparent is right
 * here because minSdk is 26 and both bars support dark icons from 23 (status)
 * and 26 (navigation).
 */
fun ComponentActivity.enableEdgeToEdgeLight() {
    val light = SystemBarStyle.light(Color.TRANSPARENT, Color.TRANSPARENT)
    enableEdgeToEdge(statusBarStyle = light, navigationBarStyle = light)
}
