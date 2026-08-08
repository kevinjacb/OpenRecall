package com.opensapien.relay.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.widget.Toast

/**
 * Copy [value] to the system clipboard and toast a confirmation.
 *
 * Shared by the read-only `SettingsValueRow`s on Settings and Device — both
 * carry identifiers (relay URL, token, BLE address) that are only useful if
 * you can get them out of the phone. Blank values are a no-op so an em-dash
 * placeholder row never claims to have copied anything.
 */
fun copyToClipboard(context: Context, label: String, value: String) {
    if (value.isBlank()) return
    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    clipboard.setPrimaryClip(ClipData.newPlainText(label, value))
    Toast.makeText(context, "$label copied", Toast.LENGTH_SHORT).show()
}
