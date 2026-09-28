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
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.ExperimentalTextApi
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontVariation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
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
private val Faint = Color(0x73F4F7FA)
private val Glass = Color(0x0FFFFFFF)
private val GlassEdge = Color(0x17FFFFFF)
private val Accent = Color(0xFF7EE6D3)
private val Amber = Color(0xFFF2C14E)
private val Red = Color(0xFFEF6F6C)

@OptIn(ExperimentalTextApi::class)
private fun inter(weight: Int) = Font(R.font.inter, FontWeight(weight),
    variationSettings = FontVariation.Settings(FontVariation.weight(weight)))

/** Inter (SIL OFL, bundled) with tabular digits so times and temperatures don't jitter. */
val Inter = FontFamily(inter(200), inter(300), inter(400), inter(500), inter(600))
private val Base = TextStyle(fontFamily = Inter, fontFeatureSettings = "tnum", color = Ink)

private val clockFormat = DateTimeFormatter.ofPattern("HH:mm")
private val secondsFormat = DateTimeFormatter.ofPattern("ss")
private val dateFormat = DateTimeFormatter.ofPattern("EEEE  d MMMM")
private val dayFormat = DateTimeFormatter.ofPattern("EEE")

private fun Double.deg() = "${roundToInt()}°"

private data class Palette(val top: Color, val bottom: Color, val glowA: Color, val glowB: Color)

/** Night, twilight (45 minutes either side of sunrise/sunset) or day. */
private fun palette(now: LocalDateTime, weather: Weather?): Palette {
    val sunrise = weather?.sunrise ?: now.toLocalDate().atTime(7, 0)
    val sunset = weather?.sunset ?: now.toLocalDate().atTime(19, 0)
    val near = { t: LocalDateTime -> now.isAfter(t.minusMinutes(45)) && now.isBefore(t.plusMinutes(45)) }
    return when {
        near(sunrise) || near(sunset) -> Palette(Color(0xFF120C22), Color(0xFF2A1330), Color(0xFFD0643F), Color(0xFF7A2E6B))
        now.isAfter(sunrise) && now.isBefore(sunset) -> Palette(Color(0xFF06223A), Color(0xFF0E4466), Color(0xFF3FA7D6), Color(0xFF1F7A6E))
        else -> Palette(Color(0xFF04060D), Color(0xFF0A1020), Color(0xFF3A2E8C), Color(0xFF0B5563))
    }
}

private fun Modifier.glass() = clip(RoundedCornerShape(28.dp)).background(Glass)
    .border(1.dp, GlassEdge, RoundedCornerShape(28.dp))

private fun Modifier.pill() = clip(RoundedCornerShape(50)).background(Glass)
    .border(1.dp, GlassEdge, RoundedCornerShape(50)).padding(horizontal = 12.dp, vertical = 6.dp)

@Composable
private fun Label(text: String, color: Color = Faint) =
    Text(text.uppercase(), color = color, fontSize = 11.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 2.sp)

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
    val colors = palette(now, weather)
    Box(Modifier.fillMaxSize().drawBehind {
        drawRect(Brush.verticalGradient(listOf(colors.top, colors.bottom)))
        val glowA = Offset(size.width * 0.82f, size.height * 0.05f)
        drawCircle(Brush.radialGradient(listOf(colors.glowA.copy(alpha = 0.38f), Color.Transparent), glowA, size.width * 0.5f),
            size.width * 0.5f, glowA)
        val glowB = Offset(size.width * 0.08f, size.height * 1.05f)
        drawCircle(Brush.radialGradient(listOf(colors.glowB.copy(alpha = 0.32f), Color.Transparent), glowB, size.width * 0.45f),
            size.width * 0.45f, glowB)
    }) {
        CompositionLocalProvider(LocalTextStyle provides Base) {
            Column(Modifier.fillMaxSize().alpha(if (night) 0.55f else 1f).padding(horizontal = 32.dp, vertical = 22.dp)) {
                Row(Modifier.weight(1f)) {
                    Column(Modifier.weight(1f).fillMaxHeight()) {
                        Label(now.format(dateFormat), Soft)
                        Row {
                            Text(now.format(clockFormat), fontSize = 112.sp, fontWeight = FontWeight.ExtraLight,
                                letterSpacing = (-5).sp, lineHeight = 112.sp, modifier = Modifier.alignByBaseline())
                            Text(now.format(secondsFormat), color = Accent, fontSize = 26.sp, fontWeight = FontWeight.Light,
                                modifier = Modifier.alignByBaseline().padding(start = 10.dp))
                        }
                        Spacer(Modifier.weight(1f))
                        WeatherNow(weather, weatherError)
                        Spacer(Modifier.height(6.dp))
                        if (weather != null) HourlyCurve(weather, now, colors.bottom, Modifier.fillMaxWidth().height(76.dp))
                    }
                    Spacer(Modifier.width(28.dp))
                    AnimatedContent(conversation, Modifier.width(300.dp).fillMaxHeight(),
                        transitionSpec = { fadeIn(tween(350)) togetherWith fadeOut(tween(350)) }, label = "panel") { talking ->
                        if (talking) ConversationCard(state) else AgendaCard(events, calendarAllowed, now, onAllowCalendar)
                    }
                }
                Spacer(Modifier.height(14.dp))
                StatusBar(state, now, onSettings)
            }
        }
    }
}

@Composable
private fun WeatherNow(weather: Weather?, error: String?) {
    if (weather == null) {
        Text(if (error != null) "Weather unavailable" else "Loading weather…", color = Faint, fontSize = 14.sp)
        return
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        WeatherIcon(weather.code, weather.isDay, Modifier.size(46.dp))
        Spacer(Modifier.width(14.dp))
        Text(weather.tempC.deg(), fontSize = 50.sp, fontWeight = FontWeight.Light, letterSpacing = (-2).sp)
        Spacer(Modifier.width(18.dp))
        Column {
            Text(condition(weather.code) + if (weather.place.isNotBlank()) "  ·  ${weather.place}" else "",
                fontSize = 15.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text("H ${weather.highC.deg()}   L ${weather.lowC.deg()}   Feels ${weather.feelsC.deg()}",
                color = Soft, fontSize = 13.sp)
        }
    }
}

/** Next hours as a smooth temperature line with a soft fill, values above and hours below. */
@Composable
private fun HourlyCurve(weather: Weather, now: LocalDateTime, backdrop: Color, modifier: Modifier) {
    val measurer = rememberTextMeasurer()
    val nextHour = now.withMinute(0).withSecond(0).withNano(0).plusHours(1)
    val hours = weather.hours.filter { !it.time.isBefore(nextHour) }.take(7)
    if (hours.size < 2) return
    val valueStyle = Base.copy(fontSize = 13.sp, fontWeight = FontWeight.Medium)
    val hourStyle = Base.copy(fontSize = 11.sp, color = Faint)
    val wetStyle = hourStyle.copy(color = Color(0xFF7CC6FF))
    Canvas(modifier) {
        val top = 24.dp.toPx()
        val bottom = size.height - 24.dp.toPx()
        val min = hours.minOf { it.tempC }
        val span = (hours.maxOf { it.tempC } - min).coerceAtLeast(1.0)
        val step = size.width / hours.size
        val points = hours.mapIndexed { i, h ->
            Offset(step * (i + 0.5f), (bottom - (h.tempC - min) / span * (bottom - top)).toFloat())
        }
        // Extend flat to both edges so the fill spans the width.
        val edge = listOf(Offset(0f, points.first().y)) + points + Offset(size.width, points.last().y)
        val line = Path().apply {
            moveTo(edge[0].x, edge[0].y)
            for (i in 1 until edge.size) {
                val (a, b) = edge[i - 1] to edge[i]
                val mid = (a.x + b.x) / 2
                cubicTo(mid, a.y, mid, b.y, b.x, b.y)
            }
        }
        val floor = size.height - 20.dp.toPx()
        val fill = Path().apply {
            addPath(line)
            lineTo(size.width, floor); lineTo(0f, floor); close()
        }
        drawPath(fill, Brush.verticalGradient(listOf(Accent.copy(alpha = 0.22f), Color.Transparent), top, floor))
        drawPath(line, Brush.horizontalGradient(listOf(Accent.copy(alpha = 0.15f), Accent, Accent.copy(alpha = 0.15f))),
            style = Stroke(2.dp.toPx(), cap = StrokeCap.Round))
        hours.forEachIndexed { i, hour ->
            val p = points[i]
            drawCircle(backdrop, 4.5.dp.toPx(), p)
            drawCircle(Accent, 3.dp.toPx(), p)
            val value = measurer.measure(hour.tempC.deg(), valueStyle)
            drawText(value, topLeft = Offset(p.x - value.size.width / 2f, p.y - 8.dp.toPx() - value.size.height))
            val wet = hour.precipPercent >= 30
            val label = measurer.measure(hour.time.format(clockFormat).take(2) + if (wet) " ${hour.precipPercent}%" else "",
                if (wet) wetStyle else hourStyle)
            drawText(label, topLeft = Offset(p.x - label.size.width / 2f, size.height - label.size.height))
        }
    }
}

private fun LocalDate.coerceAtLeast(other: LocalDate) = if (isBefore(other)) other else this

/** University-style "Campus: X Building: Y Room: Z" locations shortened to "Y Z". */
private fun shortLocation(location: String): String {
    val building = location.substringAfter("Building:", "").trim()
    if (building.isEmpty()) return location
    return building.replace(Regex("""\s*Room:\s*"""), " ").trim()
}

private fun relative(fromMs: Long, toMs: Long): String {
    val minutes = ((toMs - fromMs) / 60_000).coerceAtLeast(1)
    return when {
        minutes < 60 -> "$minutes min"
        minutes < 24 * 60 -> "${minutes / 60} h" + if (minutes % 60 != 0L) " ${minutes % 60} min" else ""
        else -> "${minutes / (24 * 60)} d"
    }
}

@Composable
private fun AgendaCard(events: List<Event>, allowed: Boolean, now: LocalDateTime, onAllow: () -> Unit) {
    Column(Modifier.fillMaxSize().glass().padding(22.dp)) {
        if (!allowed) {
            Label("Calendar")
            Spacer(Modifier.height(12.dp))
            Text("Allow calendar access", fontSize = 15.sp, fontWeight = FontWeight.Medium,
                modifier = Modifier.clickable(onClick = onAllow).pill())
            return@Column
        }
        val zone = ZoneId.systemDefault()
        val nowMs = now.atZone(zone).toInstant().toEpochMilli()
        val today = now.toLocalDate()
        val next = events.firstOrNull { !it.allDay }
        if (next == null) {
            Label("Up next")
            Spacer(Modifier.height(10.dp))
            Text("Nothing scheduled", fontSize = 22.sp, fontWeight = FontWeight.Light)
            Text("Today and tomorrow are clear", color = Faint, fontSize = 13.sp)
        } else {
            val begin = Instant.ofEpochMilli(next.begin).atZone(zone)
            val end = Instant.ofEpochMilli(next.end).atZone(zone)
            val happening = nowMs >= next.begin
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(7.dp).clip(CircleShape).background(if (next.color != 0) Color(next.color) else Accent))
                Spacer(Modifier.width(8.dp))
                Label(if (happening) "Now" else if (begin.toLocalDate() == today) "Up next" else "Tomorrow")
            }
            Spacer(Modifier.height(10.dp))
            Text(next.title, fontSize = 20.sp, fontWeight = FontWeight.SemiBold, lineHeight = 25.sp,
                maxLines = 2, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(6.dp))
            Row {
                Text("${begin.format(clockFormat)} – ${end.format(clockFormat)}", color = Soft, fontSize = 14.sp)
                Text(if (happening) "   ends in ${relative(nowMs, next.end)}" else "   in ${relative(nowMs, next.begin)}",
                    color = Accent, fontSize = 14.sp, fontWeight = FontWeight.Medium)
            }
            if (next.location.isNotBlank()) Text(shortLocation(next.location), color = Faint, fontSize = 12.sp,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        val later = events.filter { it !== next }
        if (later.isNotEmpty()) {
            Spacer(Modifier.weight(1f))
            Box(Modifier.fillMaxWidth().height(1.dp).background(GlassEdge))
            Spacer(Modifier.height(12.dp))
            Label("Later")
            Spacer(Modifier.height(4.dp))
            for (event in later.take(3)) {
                val start = Instant.ofEpochMilli(event.begin).atZone(zone)
                val day = start.toLocalDate().coerceAtLeast(today)
                val time = (if (day != today) start.format(dayFormat) + " " else "") +
                    (if (event.allDay) "All day" else start.format(clockFormat))
                Row(Modifier.padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(5.dp).clip(CircleShape).background(if (event.color != 0) Color(event.color) else Accent))
                    Spacer(Modifier.width(8.dp))
                    Text(time, color = Soft, fontSize = 13.sp, modifier = Modifier.width(78.dp))
                    Text(event.title, fontSize = 14.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
            if (later.size > 3) Text("+${later.size - 3} more", color = Faint, fontSize = 12.sp,
                modifier = Modifier.padding(start = 13.dp, top = 2.dp))
        }
    }
}

@Composable
private fun ConversationCard(state: Dashboard) {
    val (title, color) = when (state.status) {
        "listening" -> "Listening" to Accent
        "transcribing" -> "Transcribing" to Accent
        "waiting" -> "Waiting for reply" to Amber
        "speaking" -> "Speaking" to Accent
        else -> "Done" to Soft
    }
    Column(Modifier.fillMaxSize().glass().padding(22.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Equalizer(color, active = state.status in listOf("listening", "speaking"))
            Spacer(Modifier.width(10.dp))
            Label(title, color)
        }
        Spacer(Modifier.height(16.dp))
        if (state.transcript.isNotBlank()) Text("“${state.transcript}”", fontSize = 21.sp, fontWeight = FontWeight.Light,
            lineHeight = 27.sp, maxLines = 4, overflow = TextOverflow.Ellipsis)
        if (state.status == "waiting") Text("Sent by email, waiting for the reply", color = Faint, fontSize = 13.sp,
            modifier = Modifier.padding(top = 8.dp))
        if (state.reply.isNotBlank() && state.status in listOf("speaking", "idle")) {
            Spacer(Modifier.weight(1f))
            Box(Modifier.fillMaxWidth().height(1.dp).background(GlassEdge))
            Spacer(Modifier.height(12.dp))
            Text(state.reply, color = Soft, fontSize = 15.sp, lineHeight = 21.sp, maxLines = 5, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** Five bars that bounce while listening or speaking and sit still otherwise. */
@Composable
private fun Equalizer(color: Color, active: Boolean) {
    val transition = rememberInfiniteTransition(label = "eq")
    Row(Modifier.height(16.dp), horizontalArrangement = Arrangement.spacedBy(2.dp), verticalAlignment = Alignment.CenterVertically) {
        for (i in 0 until 5) {
            val level = if (!active) 0.35f else transition.animateFloat(0.25f, 1f,
                infiniteRepeatable(tween(380 + i * 90), RepeatMode.Reverse), label = "bar$i").value
            Box(Modifier.width(3.dp).fillMaxHeight(level).clip(RoundedCornerShape(2.dp)).background(color))
        }
    }
}

@Composable
private fun Dot(color: Color, pulse: Boolean) {
    val alpha = if (pulse) rememberInfiniteTransition(label = "pulse").animateFloat(0.35f, 1f,
        infiniteRepeatable(tween(700), RepeatMode.Reverse), label = "alpha").value else 1f
    Box(Modifier.size(7.dp).alpha(alpha).clip(CircleShape).background(color))
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
        Row(Modifier.weight(1f, fill = false).pill(), verticalAlignment = Alignment.CenterVertically) {
            Dot(voiceColor, pulse = state.running && state.connection == "Connected" && state.status != "idle")
            Spacer(Modifier.width(8.dp))
            Text(voice, color = Soft, fontSize = 12.sp, fontWeight = FontWeight.Medium, maxLines = 1,
                overflow = TextOverflow.Ellipsis)
        }
        Spacer(Modifier.weight(1f))
        PiPill(state.pi, state.connection == "Connected", now)
        Spacer(Modifier.width(8.dp))
        Box(Modifier.size(30.dp).clip(CircleShape).background(Glass).border(1.dp, GlassEdge, CircleShape)
            .clickable(onClick = onSettings), contentAlignment = Alignment.Center) {
            Icon(Icons.Outlined.Settings, contentDescription = "Settings", tint = Faint, modifier = Modifier.size(16.dp))
        }
    }
}

@Composable
private fun PiPill(pi: PiStatus?, connected: Boolean, now: LocalDateTime) {
    val nowMs = now.atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
    val fresh = pi != null && connected && nowMs - pi.receivedAt < 90_000
    Row(Modifier.pill(), verticalAlignment = Alignment.CenterVertically) {
        if (!fresh) {
            Dot(Red, pulse = false)
            Spacer(Modifier.width(8.dp))
            Text("Pi offline", color = Soft, fontSize = 12.sp, fontWeight = FontWeight.Medium)
            return@Row
        }
        val strained = (pi!!.memAvailableMb ?: Int.MAX_VALUE) < 300 || (pi.tempC ?: 0.0) >= 75
        Dot(if (strained) Amber else Accent, pulse = false)
        Spacer(Modifier.width(8.dp))
        Text("Pi", fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
        val parts = listOfNotNull(
            pi.tempC?.let { "${it.roundToInt()}°C" },
            pi.memAvailableMb?.let { if (it >= 1024) "%.1f GB free".format(it / 1024.0) else "$it MB free" },
            pi.load1?.let { "load %.1f".format(it) },
        )
        Text("   " + parts.joinToString("   "), color = Soft, fontSize = 12.sp)
    }
}
