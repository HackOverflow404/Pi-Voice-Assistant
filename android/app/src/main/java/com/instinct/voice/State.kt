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
    /** Seconds the Pi waits before sending, so a false trigger can be cancelled. */
    val sendDelay: Int = 0,
    /** When the current status began (for the send countdown). */
    val statusSince: Long = 0,
    val running: Boolean = false,
    val pi: PiStatus? = null,
    /** Events the Pi reads from the configured iCal links; null until it sends any. */
    val calendar: List<Event>? = null
)

/** Pi health from the server's periodic `system` message; null fields were unavailable. */
data class PiStatus(
    val memAvailableMb: Int?, val memTotalMb: Int?, val swapUsedMb: Int?,
    val load1: Double?, val uptimeSeconds: Long?, val tempC: Double?,
    val cpuPercent: Double?, val memPercent: Double?,
    val receivedAt: Long = System.currentTimeMillis()
)

object State {
    private val mutable = MutableStateFlow(Dashboard())
    val flow = mutable.asStateFlow()
    fun connection(value: String, error: String = "") = mutable.update {
        it.copy(connection = value, error = error)
    }
    fun running(value: Boolean) = mutable.update { it.copy(running = value) }
    fun calendar(json: JSONObject) = mutable.update {
        val list = json.getJSONArray("events")
        it.copy(calendar = (0 until list.length()).map { i ->
            val e = list.getJSONObject(i)
            Event(e.optString("title", "(No title)"), e.getLong("begin"), e.getLong("end"),
                e.optBoolean("all_day"), e.optLong("color", 0).toInt(), e.optString("location"))
        })
    }
    fun system(json: JSONObject) = mutable.update {
        fun int(key: String) = if (json.has(key)) json.optInt(key) else null
        fun double(key: String) = if (json.has(key)) json.optDouble(key) else null
        it.copy(pi = PiStatus(int("mem_available_mb"), int("mem_total_mb"), int("swap_used_mb"),
            double("load1"), if (json.has("uptime_s")) json.optLong("uptime_s") else null, double("temp_c"),
            double("cpu_percent"), double("mem_percent")))
    }
    fun event(json: JSONObject) = mutable.update {
        val status = json.optString("status", "idle")
        it.copy(status = status, sendDelay = json.optInt("send_delay", 0),
            statusSince = if (status != it.status) System.currentTimeMillis() else it.statusSince,
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
    /** Media volume the service holds, 0-1 of the stream's range; set by voice commands. */
    var mediaVolume: Float
        get() = prefs.getFloat("media_volume", 1f)
        set(value) { prefs.edit().putFloat("media_volume", value.coerceIn(0f, 1f)).apply() }
}
