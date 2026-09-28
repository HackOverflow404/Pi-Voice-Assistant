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
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.time.LocalTime

class MainActivity : ComponentActivity() {
    private var calendarAllowed by mutableStateOf(false)

    private val permissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        calendarAllowed = Glance.canReadCalendar(this)
        if (it.containsKey(Manifest.permission.RECORD_AUDIO)) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED) startVoice()
            else State.connection("Stopped", "Microphone permission is required")
        }
    }

    private fun startVoice() {
        Settings(this).enabled = true
        ContextCompat.startForegroundService(this, Intent(this, VoiceService::class.java))
    }

    // Without device ownership, Android 11+ blocks microphone services started from the
    // background, so the Pi's adb watcher restarts the service through this visible activity.
    // It only resumes a service the user already started; Stop still disables it.
    private fun handleStart(intent: Intent?) {
        if (intent?.action != ACTION_START) return
        val settings = Settings(this)
        if (!settings.enabled || settings.token.isBlank() || ContextCompat.checkSelfPermission(this,
                Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) return
        startVoice()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleStart(intent)
    }

    override fun onResume() {
        super.onResume()
        calendarAllowed = Glance.canReadCalendar(this)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) immersive()
    }

    // Always-on display: hide the status and navigation bars; a swipe from the edge shows them briefly.
    private fun immersive() = WindowInsetsControllerCompat(window, window.decorView).run {
        hide(WindowInsetsCompat.Type.systemBars())
        systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
    }

    private fun setNightBrightness(night: Boolean) {
        window.attributes = window.attributes.apply {
            screenBrightness = if (night) 0.03f else WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        val settings = Settings(this)
        handleStart(intent)
        calendarAllowed = Glance.canReadCalendar(this)
        setContent {
            val state by State.flow.collectAsStateWithLifecycle()
            var weather by remember { mutableStateOf<Weather?>(null) }
            var weatherError by remember { mutableStateOf<String?>(null) }
            var events by remember { mutableStateOf(emptyList<Event>()) }
            var showSettings by remember { mutableStateOf(settings.token.isBlank()) }
            var night by remember { mutableStateOf(false) }
            LaunchedEffect(Unit) {
                while (true) {
                    val result = withContext(Dispatchers.IO) { runCatching { Glance.weather(this@MainActivity) } }
                    result.onSuccess { weather = it; weatherError = null }
                        .onFailure { weatherError = it.message ?: it.javaClass.simpleName }
                    delay(if (result.isSuccess) 15 * 60_000L else 2 * 60_000L)
                }
            }
            LaunchedEffect(calendarAllowed) {
                while (true) {
                    events = withContext(Dispatchers.IO) {
                        runCatching { Glance.events(this@MainActivity) }.getOrDefault(events)
                    }
                    delay(60_000)
                }
            }
            LaunchedEffect(Unit) {
                while (true) {
                    val hour = LocalTime.now().hour
                    val isNight = hour >= 22 || hour < 7
                    if (isNight != night) { night = isNight; setNightBrightness(isNight) }
                    delay(30_000)
                }
            }
            MaterialTheme(colorScheme = darkColorScheme(primary = Color(0xFF72DCCA),
                background = Color(0xFF10191E), surface = Color(0xFF1C292F))) {
                GlanceDashboard(state, weather, weatherError, events, calendarAllowed, night,
                    onAllowCalendar = { permissions.launch(arrayOf(Manifest.permission.READ_CALENDAR)) },
                    onSettings = { showSettings = true })
                if (showSettings) SettingsDialog(settings, state, onDismiss = { showSettings = false })
            }
        }
        immersive()
    }

    @Composable
    private fun SettingsDialog(settings: Settings, state: Dashboard, onDismiss: () -> Unit) {
        var url by remember { mutableStateOf(settings.url) }
        var token by remember { mutableStateOf(settings.token) }
        val owner = getSystemService(DevicePolicyManager::class.java).isDeviceOwnerApp(packageName)
        AlertDialog(onDismissRequest = onDismiss,
            title = { Text("Voice assistant") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("${state.connection} · ${state.status}" + if (state.error.isNotBlank()) "\n${state.error}" else "",
                        style = MaterialTheme.typography.bodySmall)
                    OutlinedTextField(url, { url = it }, label = { Text("Pi WebSocket URL") },
                        singleLine = true, enabled = !state.running, modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(token, { token = it }, label = { Text("Shared token") },
                        visualTransformation = PasswordVisualTransformation(), singleLine = true,
                        enabled = !state.running, modifier = Modifier.fillMaxWidth())
                    Text(if (owner || Build.VERSION.SDK_INT < 30) "Autostart enabled after Start; Stop disables it."
                        else "Boot microphone access requires device-owner setup or the Pi's autostart timer (see README).",
                        style = MaterialTheme.typography.bodySmall)
                }
            },
            confirmButton = {
                if (!state.running) Button(onClick = {
                    if (!(url.startsWith("ws://") || url.startsWith("wss://")) || token.length < 32) {
                        State.connection("Stopped", "Enter a ws:// or wss:// URL and a token of at least 32 characters")
                    } else {
                        settings.url = url.trim(); settings.token = token.trim()
                        val needed = mutableListOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.READ_CALENDAR)
                        if (Build.VERSION.SDK_INT >= 33) needed.add(Manifest.permission.POST_NOTIFICATIONS)
                        permissions.launch(needed.toTypedArray())
                        onDismiss()
                    }
                }) { Text("Start") }
                else OutlinedButton(onClick = {
                    settings.enabled = false
                    stopService(Intent(this@MainActivity, VoiceService::class.java))
                }) { Text("Stop") }
            },
            dismissButton = { TextButton(onClick = onDismiss) { Text("Close") } })
    }
}

const val ACTION_START = "com.instinct.voice.START"
