package com.instinct.voice

import android.Manifest
import android.app.admin.DevicePolicyManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle

class MainActivity : ComponentActivity() {
    private val permissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED) startVoice()
        else State.connection("Stopped", "Microphone permission is required")
    }

    private fun startVoice() {
        Settings(this).enabled = true
        ContextCompat.startForegroundService(this, Intent(this, VoiceService::class.java))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val settings = Settings(this)
        setContent {
            val state by State.flow.collectAsStateWithLifecycle()
            var url by remember { mutableStateOf(settings.url) }
            var token by remember { mutableStateOf(settings.token) }
            var editing by remember { mutableStateOf(settings.token.isBlank()) }
            val owner = getSystemService(DevicePolicyManager::class.java).isDeviceOwnerApp(packageName)
            MaterialTheme(colorScheme = darkColorScheme(primary = Color(0xFF72DCCA),
                background = Color(0xFF10191E), surface = Color(0xFF1C292F))) {
                Column(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)
                    .verticalScroll(rememberScrollState()).padding(20.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text("Pi Voice Assistant", style = MaterialTheme.typography.headlineSmall,
                        color = MaterialTheme.colorScheme.primary)
                    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        StatusCard("CONNECTION", state.connection, Modifier.weight(1f))
                        StatusCard("ASSISTANT", state.status.replaceFirstChar { it.uppercase() }, Modifier.weight(1f))
                    }
                    StatusCard("YOU SAID", state.transcript.ifBlank { "Say your wake phrase, then your request." })
                    StatusCard("REPLY", state.reply.ifBlank { "The email reply will appear here." })
                    if (state.error.isNotBlank()) StatusCard("ATTENTION", state.error)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = {
                            if (!(url.startsWith("ws://") || url.startsWith("wss://")) || token.length < 32) {
                                State.connection("Stopped", "Enter a ws:// or wss:// URL and a token of at least 32 characters")
                            } else {
                                settings.url = url.trim(); settings.token = token.trim()
                                val needed = mutableListOf(Manifest.permission.RECORD_AUDIO)
                                if (Build.VERSION.SDK_INT >= 33) needed.add(Manifest.permission.POST_NOTIFICATIONS)
                                permissions.launch(needed.toTypedArray())
                            }
                        }, enabled = !state.running) { Text("Start") }
                        OutlinedButton(onClick = {
                            settings.enabled = false
                            stopService(Intent(this@MainActivity, VoiceService::class.java))
                        }, enabled = state.running) { Text("Stop") }
                        TextButton(onClick = { editing = !editing }) { Text("Connection settings") }
                    }
                    if (editing) {
                        OutlinedTextField(url, { url = it }, label = { Text("Pi WebSocket URL") },
                            singleLine = true, enabled = !state.running, modifier = Modifier.fillMaxWidth())
                        OutlinedTextField(token, { token = it }, label = { Text("Shared token") },
                            visualTransformation = PasswordVisualTransformation(), singleLine = true,
                            enabled = !state.running, modifier = Modifier.fillMaxWidth())
                    }
                    Text(if (owner || Build.VERSION.SDK_INT < 30) "Autostart enabled after Start; Stop disables it."
                        else "Boot microphone access requires device-owner setup (see README).",
                        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}

@Composable
private fun StatusCard(title: String, value: String, modifier: Modifier = Modifier) {
    Card(modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(title, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary)
            Text(value, style = MaterialTheme.typography.bodyLarge)
        }
    }
}
