package com.instinct.voice

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CalendarContract.Attendees
import android.provider.CalendarContract.Events
import android.provider.CalendarContract.Instances
import androidx.core.content.ContextCompat
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.util.Locale
import java.util.concurrent.TimeUnit

data class Hour(val time: LocalDateTime, val tempC: Double, val code: Int, val precipPercent: Int)

data class Weather(
    val place: String, val tempC: Double, val feelsC: Double, val humidity: Int, val windKmh: Double,
    val code: Int, val isDay: Boolean, val highC: Double, val lowC: Double,
    val sunrise: LocalDateTime?, val sunset: LocalDateTime?, val hours: List<Hour>
)

data class Event(
    val title: String, val begin: Long, val end: Long, val allDay: Boolean,
    val color: Int, val location: String
)

/** At-a-glance data fetched on the Echo itself: Open-Meteo weather located by IP, and
 *  events from calendars already synced to this device. Nothing here touches the Pi. */
object Glance {
    private const val DAY_MS = 24 * 60 * 60 * 1000L
    private val http = OkHttpClient.Builder().connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS).build()

    private fun get(url: String): JSONObject =
        http.newCall(Request.Builder().url(url).build()).execute().use { response ->
            check(response.isSuccessful) { "HTTP ${response.code} from ${response.request.url.host}" }
            JSONObject(response.body!!.string())
        }

    private data class Place(val lat: Double, val lon: Double, val city: String)

    /** Approximate location from the public IP, cached for a day. If every lookup fails,
     *  a stale cached location is still better than no weather. */
    private fun place(context: Context): Place {
        val prefs = context.getSharedPreferences("glance", Context.MODE_PRIVATE)
        val cached = prefs.getString("lat", null)?.let {
            Place(it.toDouble(), prefs.getString("lon", "0")!!.toDouble(), prefs.getString("city", "")!!)
        }
        if (cached != null && System.currentTimeMillis() - prefs.getLong("located_at", 0) < DAY_MS) return cached
        val lookups = listOf<() -> Place>(
            { get("https://ipapi.co/json/").let { Place(it.getDouble("latitude"), it.getDouble("longitude"), it.optString("city")) } },
            { get("https://get.geojs.io/v1/ip/geo.json").let {
                Place(it.getString("latitude").toDouble(), it.getString("longitude").toDouble(), it.optString("city"))
            } },
        )
        for (lookup in lookups) {
            val found = runCatching(lookup).getOrNull() ?: continue
            prefs.edit().putString("lat", found.lat.toString()).putString("lon", found.lon.toString())
                .putString("city", found.city).putLong("located_at", System.currentTimeMillis()).apply()
            return found
        }
        return cached ?: error("Location lookup failed")
    }

    /** Blocking; call from Dispatchers.IO. */
    fun weather(context: Context): Weather {
        val place = place(context)
        val json = get(String.format(Locale.US,
            "https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f" +
            "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day" +
            "&hourly=temperature_2m,weather_code,precipitation_probability" +
            "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset&timezone=auto&forecast_days=2",
            place.lat, place.lon))
        val current = json.getJSONObject("current")
        val daily = json.getJSONObject("daily")
        val hourly = json.getJSONObject("hourly")
        val times = hourly.getJSONArray("time")
        val nextHour = LocalDateTime.now().withMinute(0).withSecond(0).withNano(0).plusHours(1)
        val hours = (0 until times.length()).asSequence()
            .map { i -> i to LocalDateTime.parse(times.getString(i)) }
            .filter { (_, time) -> !time.isBefore(nextHour) }
            .take(6)
            .map { (i, time) ->
                Hour(time, hourly.getJSONArray("temperature_2m").getDouble(i),
                    hourly.getJSONArray("weather_code").getInt(i),
                    hourly.getJSONArray("precipitation_probability").optInt(i, 0))
            }.toList()
        fun dailyTime(key: String) = runCatching { LocalDateTime.parse(daily.getJSONArray(key).getString(0)) }.getOrNull()
        return Weather(
            place = place.city,
            tempC = current.getDouble("temperature_2m"),
            feelsC = current.getDouble("apparent_temperature"),
            humidity = current.getInt("relative_humidity_2m"),
            windKmh = current.getDouble("wind_speed_10m"),
            code = current.getInt("weather_code"),
            isDay = current.getInt("is_day") == 1,
            highC = daily.getJSONArray("temperature_2m_max").getDouble(0),
            lowC = daily.getJSONArray("temperature_2m_min").getDouble(0),
            sunrise = dailyTime("sunrise"), sunset = dailyTime("sunset"),
            hours = hours,
        )
    }

    fun canReadCalendar(context: Context) = ContextCompat.checkSelfPermission(context,
        Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED

    /** Today's and tomorrow's events that have not ended, skipping cancelled and declined ones.
     *  Blocking; call from Dispatchers.IO. */
    fun events(context: Context): List<Event> {
        if (!canReadCalendar(context)) return emptyList()
        val zone = ZoneId.systemDefault()
        val start = LocalDate.now().atStartOfDay(zone)
        // All-day instances are stored at UTC midnight, so widen the window by a day each side.
        val uri = Instances.CONTENT_URI.buildUpon().also {
            ContentUris.appendId(it, start.minusDays(1).toInstant().toEpochMilli())
            ContentUris.appendId(it, start.plusDays(3).toInstant().toEpochMilli())
        }.build()
        val projection = arrayOf(Instances.TITLE, Instances.BEGIN, Instances.END, Instances.ALL_DAY,
            Instances.DISPLAY_COLOR, Instances.EVENT_LOCATION)
        val selection = "${Instances.VISIBLE} = 1 AND ${Instances.STATUS} != ${Events.STATUS_CANCELED}" +
            " AND ${Instances.SELF_ATTENDEE_STATUS} != ${Attendees.ATTENDEE_STATUS_DECLINED}"
        val windowEnd = start.plusDays(2).toInstant().toEpochMilli()
        val now = System.currentTimeMillis()
        val events = mutableListOf<Event>()
        context.contentResolver.query(uri, projection, selection, null, "${Instances.BEGIN} ASC")?.use { c ->
            while (c.moveToNext()) {
                val allDay = c.getInt(3) == 1
                // Re-anchor all-day dates from UTC midnight to local midnight.
                fun local(ms: Long) = if (!allDay) ms else
                    Instant.ofEpochMilli(ms).atOffset(ZoneOffset.UTC).toLocalDate().atStartOfDay(zone).toInstant().toEpochMilli()
                val begin = local(c.getLong(1))
                val end = local(c.getLong(2))
                if (end <= now || begin >= windowEnd) continue
                events += Event(c.getString(0) ?: "(No title)", begin, end, allDay, c.getInt(4), c.getString(5) ?: "")
            }
        }
        return events.sortedWith(compareBy({ !it.allDay }, { it.begin }))
    }
}

/** WMO weather code to a glyph and label. */
fun describe(code: Int, isDay: Boolean = true): Pair<String, String> = when (code) {
    0 -> (if (isDay) "☀️" else "🌙") to "Clear"
    1 -> (if (isDay) "🌤️" else "🌙") to "Mainly clear"
    2 -> (if (isDay) "⛅" else "☁️") to "Partly cloudy"
    3 -> "☁️" to "Overcast"
    45, 48 -> "🌫️" to "Fog"
    51, 53, 55, 56, 57 -> "🌦️" to "Drizzle"
    61, 63, 65, 66, 67 -> "🌧️" to "Rain"
    71, 73, 75, 77 -> "🌨️" to "Snow"
    80, 81, 82 -> "🌦️" to "Showers"
    85, 86 -> "🌨️" to "Snow showers"
    95, 96, 99 -> "⛈️" to "Thunderstorm"
    else -> "🌡️" to "—"
}
