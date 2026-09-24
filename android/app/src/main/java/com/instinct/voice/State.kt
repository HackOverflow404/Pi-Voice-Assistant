package com.instinct.voice

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.json.JSONObject

data class Dashboard(
    val connection: String = "Stopped",
    val status: String = "idle",
    val transcript: String = "",
    val reply: String = "",
    val error: String = "",
    val running: Boolean = false
)

object State {
    private val mutable = MutableStateFlow(Dashboard())
    val flow = mutable.asStateFlow()
    fun connection(value: String, error: String = "") = mutable.update {
        it.copy(connection = value, error = error)
    }
    fun running(value: Boolean) = mutable.update { it.copy(running = value) }
    fun event(json: JSONObject) = mutable.update {
        it.copy(status = json.optString("status", "idle"),
            transcript = json.optString("last_transcript", ""),
            reply = json.optString("last_reply", ""),
            error = if (json.isNull("error")) "" else json.optString("error", ""))
    }
}

class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("connection", Context.MODE_PRIVATE)
    var url: String
        get() = prefs.getString("url", "ws://raspberrypi.local:8765")!!
        set(value) { prefs.edit().putString("url", value).apply() }
    var token: String
        get() = prefs.getString("token", "")!!
        set(value) { prefs.edit().putString("token", value).apply() }
    var enabled: Boolean
        get() = prefs.getBoolean("enabled", false)
        set(value) { prefs.edit().putBoolean("enabled", value).apply() }
}
