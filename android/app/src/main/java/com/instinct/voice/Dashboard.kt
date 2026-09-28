package com.instinct.voice

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlin.math.roundToInt

private val Ink = Color(0xFFF4F7FA)
private val Soft = Color(0xB3F4F7FA)
private val Faint = Color(0x66F4F7FA)
private val Panel = Color(0x1FFFFFFF)
private val Accent = Color(0xFF72DCCA)
private val Amber = Color(0xFFF2C14E)
private val Red = Color(0xFFEF6F6C)

private val clockFormat = DateTimeFormatter.ofPattern("HH:mm")
private val dateFormat = DateTimeFormatter.ofPattern("EEEE, d MMMM")

private fun Double.deg() = "${roundToInt()}°"

/** Sky gradient that follows the sun: night, twilight around sunrise/sunset, day. */
private fun sky(now: LocalDateTime, weather: Weather?): Brush {
    val sunrise = weather?.sunrise ?: now.toLocalDate().atTime(7, 0)
    val sunset = weather?.sunset ?: now.toLocalDate().atTime(19, 0)
    val twilight = { t: LocalDateTime -> now.isAfter(t.minusMinutes(45)) && now.isBefore(t.plusMinutes(45)) }
    val colors = when {
        twilight(sunrise) || twilight(sunset) -> listOf(Color(0xFF2A2350), Color(0xFF7A4B6E), Color(0xFFC77D5A))
        now.isAfter(sunrise) && now.isBefore(sunset) -> listOf(Color(0xFF0E3A5C), Color(0xFF1D6A8F), Color(0xFF3B8FB0))
        else -> listOf(Color(0xFF070B1A), Color(0xFF111A33), Color(0xFF1C2745))
    }
    return Brush.linearGradient(colors)
}

@Composable
fun GlanceDashboard(
    state: Dashboard, weather: Weather?, weatherError: String?, events: List<Event>,
    calendarAllowed: Boolean, night: Boolean, onAllowCalendar: () -> Unit, onSettings: () -> Unit,
) {
    val now by produceState(LocalDateTime.now()) {
        while (true) {
            value = LocalDateTime.now()
            delay(1000 - System.currentTimeMillis() % 1000)
        }
    }
    // Keep the conversation card up for a while after the reply finishes playing.
    var conversation by remember { mutableStateOf(false) }
    LaunchedEffect(state.status) {
        if (state.status != "idle") conversation = true
        else if (conversation) { delay(20_000); conversation = false }
    }
    Box(Modifier.fillMaxSize().background(sky(now, weather))) {
        Column(Modifier.fillMaxSize().alpha(if (night) 0.55f else 1f).padding(horizontal = 28.dp, vertical = 18.dp)) {
            Row(Modifier.weight(1f)) {
                Column(Modifier.weight(1.1f).fillMaxHeight()) {
                    Text(now.format(clockFormat), color = Ink, fontSize = 92.sp, fontWeight = FontWeight.Light,
                        letterSpacing = (-2).sp, lineHeight = 92.sp)
                    Text(now.format(dateFormat), color = Soft, fontSize = 20.sp)
                    Spacer(Modifier.weight(1f))
                    WeatherBlock(weather, weatherError, now)
                }
                Spacer(Modifier.width(24.dp))
                AnimatedContent(conversation, Modifier.weight(1f).fillMaxHeight(),
                    transitionSpec = { fadeIn(tween(300)) togetherWith fadeOut(tween(300)) }, label = "panel") { talking ->
                    if (talking) ConversationPanel(state) else CalendarPanel(events, calendarAllowed, now, onAllowCalendar)
                }
            }
            Spacer(Modifier.height(10.dp))
            StatusBar(state, now, onSettings)
        }
    }
}

@Composable
private fun WeatherBlock(weather: Weather?, error: String?, now: LocalDateTime) {
    if (weather == null) {
        Text(if (error != null) "Weather unavailable" else "Loading weather…", color = Faint, fontSize = 16.sp)
        return
    }
    val (glyph, label) = describe(weather.code, weather.isDay)
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(glyph, fontSize = 44.sp)
        Spacer(Modifier.width(12.dp))
        Text(weather.tempC.deg(), color = Ink, fontSize = 44.sp, fontWeight = FontWeight.Light)
        Spacer(Modifier.width(14.dp))
        Column {
            Text(listOf(label, weather.place).filter { it.isNotBlank() }.joinToString(" · "),
                color = Ink, fontSize = 15.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text("H ${weather.highC.deg()}  L ${weather.lowC.deg()}  ·  feels ${weather.feelsC.deg()}",
                color = Soft, fontSize = 13.sp)
            Text("${weather.humidity}% humidity  ·  ${weather.windKmh.roundToInt()} km/h", color = Soft, fontSize = 13.sp)
        }
    }
    Spacer(Modifier.height(8.dp))
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        val nextHour = now.withMinute(0).withSecond(0).withNano(0).plusHours(1)
        for (hour in weather.hours.filter { !it.time.isBefore(nextHour) }.take(6)) {
            val sun = weather.sunrise?.let { rise -> weather.sunset?.let { set ->
                hour.time.toLocalTime().let { it >= rise.toLocalTime() && it < set.toLocalTime() } } } ?: true
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(hour.time.format(DateTimeFormatter.ofPattern("HH")), color = Faint, fontSize = 12.sp)
                Text(describe(hour.code, sun).first, fontSize = 18.sp)
                Text(hour.tempC.deg(), color = Ink, fontSize = 14.sp)
                Text(if (hour.precipPercent >= 20) "${hour.precipPercent}%" else " ", color = Accent, fontSize = 11.sp)
            }
        }
    }
}

@Composable
private fun CalendarPanel(events: List<Event>, allowed: Boolean, now: LocalDateTime, onAllow: () -> Unit) {
    Column(Modifier.fillMaxSize().clip(RoundedCornerShape(20.dp)).background(Panel).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp)) {
        if (!allowed) {
            Text("Calendar", color = Soft, fontSize = 13.sp, fontWeight = FontWeight.Medium)
            Text("Tap to allow calendar access", color = Ink, fontSize = 16.sp,
                modifier = Modifier.clip(RoundedCornerShape(12.dp)).clickable(onClick = onAllow)
                    .background(Panel).padding(horizontal = 14.dp, vertical = 10.dp))
            return@Column
        }
        val zone = ZoneId.systemDefault()
        val today = now.toLocalDate()
        val byDay = events.groupBy { Instant.ofEpochMilli(it.begin).atZone(zone).toLocalDate().coerceAtLeast(today) }
        // The panel fits about four event rows; the empty-today line takes the place of one.
        val cap = if (byDay[today].isNullOrEmpty()) 3 else 4
        var shown = 0
        for (day in listOf(today, today.plusDays(1))) {
            val list = byDay[day].orEmpty()
            if (day != today && (list.isEmpty() || shown >= cap)) continue
            Text(if (day == today) "Today" else "Tomorrow", color = Soft, fontSize = 13.sp, fontWeight = FontWeight.Medium)
            if (list.isEmpty()) Text("Nothing else scheduled", color = Faint, fontSize = 15.sp)
            for (event in list) {
                if (shown >= cap) break
                EventRow(event, zone, now)
                shown++
            }
        }
        if (events.size > shown) Text("+${events.size - shown} more", color = Faint, fontSize = 12.sp)
    }
}

/** University-style "Campus: X Building: Y Room: Z" locations shortened to "Y Z". */
private fun shortLocation(location: String): String {
    val building = location.substringAfter("Building:", "").trim()
    if (building.isEmpty()) return location
    return building.replace(Regex("""\s*Room:\s*"""), " ").trim()
}

private fun LocalDate.coerceAtLeast(other: LocalDate) = if (isBefore(other)) other else this

@Composable
private fun EventRow(event: Event, zone: ZoneId, now: LocalDateTime) {
    val begin = Instant.ofEpochMilli(event.begin).atZone(zone).toLocalDateTime()
    val end = Instant.ofEpochMilli(event.end).atZone(zone).toLocalDateTime()
    val happening = !event.allDay && !now.isBefore(begin) && now.isBefore(end)
    val time = when {
        event.allDay -> "All day"
        happening -> "Now"
        else -> begin.format(clockFormat)
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(width = 4.dp, height = 30.dp).clip(RoundedCornerShape(2.dp))
            .background(if (event.color != 0) Color(event.color) else Accent))
        Spacer(Modifier.width(10.dp))
        Text(time, color = if (happening) Accent else Soft, fontSize = 14.sp, modifier = Modifier.width(56.dp))
        Column {
            Text(event.title, color = Ink, fontSize = 15.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (event.location.isNotBlank()) Text(shortLocation(event.location), color = Faint, fontSize = 12.sp,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
    }
}

@Composable
private fun ConversationPanel(state: Dashboard) {
    val (title, color) = when (state.status) {
        "listening" -> "Listening…" to Accent
        "transcribing" -> "Transcribing…" to Accent
        "waiting" -> "Waiting for a reply…" to Amber
        "speaking" -> "Speaking" to Accent
        else -> "Done" to Soft
    }
    Column(Modifier.fillMaxSize().clip(RoundedCornerShape(20.dp)).background(Panel).padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            PulsingDot(color, pulse = state.status != "idle")
            Spacer(Modifier.width(10.dp))
            Text(title, color = color, fontSize = 18.sp, fontWeight = FontWeight.Medium)
        }
        if (state.transcript.isNotBlank()) {
            Text("You said", color = Faint, fontSize = 12.sp)
            Text("“${state.transcript}”", color = Ink, fontSize = 18.sp, maxLines = 3, overflow = TextOverflow.Ellipsis)
        }
        if (state.reply.isNotBlank() && state.status in listOf("speaking", "idle")) {
            Text("Reply", color = Faint, fontSize = 12.sp)
            Text(state.reply, color = Ink, fontSize = 16.sp, maxLines = 5, overflow = TextOverflow.Ellipsis)
        }
    }
}

@Composable
private fun PulsingDot(color: Color, pulse: Boolean) {
    val alpha = if (pulse) rememberInfiniteTransition(label = "pulse").animateFloat(0.35f, 1f,
        infiniteRepeatable(tween(700), RepeatMode.Reverse), label = "alpha").value else 1f
    Box(Modifier.size(10.dp).alpha(alpha).clip(CircleShape).background(color))
}

@Composable
private fun StatusBar(state: Dashboard, now: LocalDateTime, onSettings: () -> Unit) {
    val (voice, voiceColor) = when {
        !state.running -> "Voice off" to Red
        state.connection != "Connected" -> state.connection to Amber
        state.error.isNotBlank() -> state.error to Amber
        state.status == "idle" -> "Say “Hey Clippy”" to Accent
        else -> state.status.replaceFirstChar { it.uppercase() } to Accent
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        PulsingDot(voiceColor, pulse = state.running && state.connection == "Connected" && state.status != "idle")
        Spacer(Modifier.width(8.dp))
        Text(voice, color = Soft, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f))
        PiLabel(state.pi, state.connection == "Connected", now)
        Spacer(Modifier.width(14.dp))
        Text("⚙", color = Faint, fontSize = 18.sp, modifier = Modifier.clip(CircleShape)
            .clickable(onClick = onSettings).padding(horizontal = 8.dp))
    }
}

@Composable
private fun PiLabel(pi: PiStatus?, connected: Boolean, now: LocalDateTime) {
    val nowMs = now.atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
    val fresh = pi != null && connected && nowMs - pi.receivedAt < 90_000
    if (!fresh) {
        Text("Pi offline", color = Red, fontSize = 13.sp)
        return
    }
    val strained = (pi!!.memAvailableMb ?: Int.MAX_VALUE) < 300 || (pi.tempC ?: 0.0) >= 75
    val parts = listOfNotNull(
        pi.tempC?.let { "${it.roundToInt()}°C" },
        pi.memAvailableMb?.let { if (it >= 1024) "%.1f GB free".format(it / 1024.0) else "$it MB free" },
        pi.load1?.let { "load %.1f".format(it) },
    )
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(7.dp).clip(CircleShape).background(if (strained) Amber else Accent))
        Spacer(Modifier.width(6.dp))
        Text("Pi  " + parts.joinToString("  ·  "), color = Faint, fontSize = 13.sp)
    }
}
