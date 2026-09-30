package com.instinct.voice

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
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
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.PlayArrow
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
import androidx.compose.ui.input.pointer.pointerInput
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

// Bento palette: near-black page, one colour family per tile.
private val Page = Color(0xFF09090C)
private val Graphite = Brush.verticalGradient(listOf(Color(0xFF1E1F26), Color(0xFF141519)))
private val TealTile = Brush.verticalGradient(listOf(Color(0xFF12332F), Color(0xFF0B211F)))
private val VioletTile = Brush.verticalGradient(listOf(Color(0xFF2B2242), Color(0xFF1B1529)))
private val AmberTile = Brush.verticalGradient(listOf(Color(0xFF3B2A0E), Color(0xFF261B08)))
private val Mint = Color(0xFF5EEAD4)
private val Lilac = Color(0xFFC4B5FD)
private val Tangerine = Color(0xFFFFB86B)
private val Divider = Color(0x1FFFFFFF)

/** Weather tile colour follows the sky: day blue, night indigo, twilight orange-pink
 *  (45 minutes either side of sunrise and sunset). */
private fun sky(now: LocalDateTime, weather: Weather?): Brush {
    val sunrise = weather?.sunrise ?: now.toLocalDate().atTime(7, 0)
    val sunset = weather?.sunset ?: now.toLocalDate().atTime(19, 0)
    val near = { t: LocalDateTime -> now.isAfter(t.minusMinutes(45)) && now.isBefore(t.plusMinutes(45)) }
    val colors = when {
        near(sunrise) || near(sunset) -> listOf(Color(0xFFF7853A), Color(0xFFC0306E))
        now.isAfter(sunrise) && now.isBefore(sunset) -> listOf(Color(0xFF4C9EF5), Color(0xFF1F5FD6))
        else -> listOf(Color(0xFF4338CA), Color(0xFF1E1B4B))
    }
    return Brush.linearGradient(colors)
}

private fun Modifier.tile(brush: Brush) = clip(RoundedCornerShape(26.dp)).background(brush)

@Composable
private fun Label(text: String, color: Color = Faint) =
    Text(text.uppercase(), color = color, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 2.sp)

@Composable
fun GlanceDashboard(
    state: Dashboard, weather: Weather?, weatherError: String?, events: List<Event>,
    calendarAllowed: Boolean, onAllowCalendar: () -> Unit, onSettings: () -> Unit,
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
    // No settings button on the display; a long press anywhere opens the connection settings.
    Box(Modifier.fillMaxSize().background(Page).pointerInput(Unit) { detectTapGestures(onLongPress = { onSettings() }) }) {
        CompositionLocalProvider(LocalTextStyle provides Base) {
            // 800 x 400 dp: clock over weather | agenda (or conversation) over voice and Pi.
            Row(Modifier.fillMaxSize().padding(14.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Column(Modifier.width(330.dp).fillMaxHeight(), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    ClockTile(now, Modifier.height(150.dp).fillMaxWidth())
                    // Timers and the stopwatch take the weather tile's place while any exist.
                    AnimatedContent(state.clockItems.isNotEmpty(), Modifier.weight(1f).fillMaxWidth(),
                        transitionSpec = { fadeIn(tween(300)) togetherWith fadeOut(tween(300)) }, label = "left") { active ->
                        if (active) TimersTile(state.clockItems, Modifier.fillMaxSize())
                        else WeatherTile(weather, weatherError, now, Modifier.fillMaxSize())
                    }
                }
                Column(Modifier.weight(1f).fillMaxHeight(), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    AnimatedContent(conversation, Modifier.weight(1f).fillMaxWidth(),
                        transitionSpec = { fadeIn(tween(350)) togetherWith fadeOut(tween(350)) }, label = "panel") { talking ->
                        if (talking) ConversationCard(state, now) else AgendaCard(events, calendarAllowed, now, onAllowCalendar)
                    }
                    Row(Modifier.height(80.dp).fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        VoiceTile(state, Modifier.weight(1f).fillMaxHeight())
                        PiTile(state.pi, state.connection == "Connected", now, Modifier.weight(1f).fillMaxHeight())
                    }
                }
            }
        }
    }
}

/** Date and seconds on the top line, the clock below, and a thin line along the bottom that
 *  fills over each minute. */
@Composable
private fun ClockTile(now: LocalDateTime, modifier: Modifier) {
    Box(modifier.tile(Graphite)) {
        Column(Modifier.fillMaxSize().padding(horizontal = 22.dp, vertical = 16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Label(now.format(dateFormat), Soft)
                Spacer(Modifier.weight(1f))
                Text(now.format(secondsFormat), color = Tangerine, fontSize = 16.sp, fontWeight = FontWeight.Medium)
            }
            Spacer(Modifier.weight(1f))
            Text(now.format(clockFormat), fontSize = 88.sp, fontWeight = FontWeight.ExtraLight,
                letterSpacing = (-4).sp, lineHeight = 88.sp, maxLines = 1, softWrap = false)
        }
        val progress by animateFloatAsState((now.second + 1) / 60f,
            if (now.second == 59) snap() else tween(1000, easing = LinearEasing), label = "minute")
        Box(Modifier.align(Alignment.BottomStart).fillMaxWidth(progress).height(3.dp)
            .background(Brush.horizontalGradient(listOf(Tangerine.copy(alpha = 0.2f), Tangerine))))
    }
}

/** Now and the next hours together on the sky-coloured tile. */
@Composable
private fun WeatherTile(weather: Weather?, error: String?, now: LocalDateTime, modifier: Modifier) {
    Column(modifier.tile(sky(now, weather)).padding(horizontal = 18.dp, vertical = 14.dp)) {
        if (weather == null) {
            Text(if (error != null) "Weather unavailable" else "Loading weather…", fontSize = 17.sp)
            return@Column
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            WeatherIcon(weather.code, weather.isDay, Modifier.size(44.dp))
            Spacer(Modifier.width(12.dp))
            Text(weather.tempC.deg(), fontSize = 46.sp, fontWeight = FontWeight.Light, letterSpacing = (-2).sp,
                lineHeight = 48.sp)
            Spacer(Modifier.width(14.dp))
            Column {
                Text(condition(weather.code) + if (weather.place.isNotBlank()) "  ·  ${weather.place}" else "",
                    fontSize = 16.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text("H ${weather.highC.deg()}  L ${weather.lowC.deg()}  Feels ${weather.feelsC.deg()}",
                    fontSize = 14.sp, color = Ink.copy(alpha = 0.85f), maxLines = 1)
            }
        }
        HourlyCurve(weather, now, Color(0xFF2F6FDB), Ink, Modifier.fillMaxSize().padding(top = 2.dp))
    }
}

/** Next hours as a smooth temperature line with a soft fill, values above and hours below. */
@Composable
private fun HourlyCurve(weather: Weather, now: LocalDateTime, backdrop: Color, accent: Color, modifier: Modifier) {
    val measurer = rememberTextMeasurer()
    val nextHour = now.withMinute(0).withSecond(0).withNano(0).plusHours(1)
    val hours = weather.hours.filter { !it.time.isBefore(nextHour) }.take(6)
    if (hours.size < 2) return
    val valueStyle = Base.copy(fontSize = 17.sp, fontWeight = FontWeight.Medium)
    val hourStyle = Base.copy(fontSize = 14.sp, color = Ink.copy(alpha = 0.7f))
    val wetStyle = hourStyle.copy(color = Color(0xFF7CC6FF))
    Canvas(modifier) {
        val top = 28.dp.toPx()
        val bottom = size.height - 28.dp.toPx()
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
        val floor = size.height - 24.dp.toPx()
        val fill = Path().apply {
            addPath(line)
            lineTo(size.width, floor); lineTo(0f, floor); close()
        }
        drawPath(fill, Brush.verticalGradient(listOf(accent.copy(alpha = 0.25f), Color.Transparent), top, floor))
        drawPath(line, Brush.horizontalGradient(listOf(accent.copy(alpha = 0.2f), accent, accent.copy(alpha = 0.2f))),
            style = Stroke(2.dp.toPx(), cap = StrokeCap.Round))
        hours.forEachIndexed { i, hour ->
            val p = points[i]
            drawCircle(backdrop, 4.5.dp.toPx(), p)
            drawCircle(accent, 3.dp.toPx(), p)
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
private fun AgendaCard(allEvents: List<Event>, allowed: Boolean, now: LocalDateTime, onAllow: () -> Unit) {
    Column(Modifier.fillMaxSize().tile(VioletTile).padding(horizontal = 22.dp, vertical = 18.dp)) {
        if (!allowed) {
            Label("Calendar")
            Spacer(Modifier.height(12.dp))
            Text("Allow calendar access", fontSize = 18.sp, fontWeight = FontWeight.Medium,
                modifier = Modifier.clickable(onClick = onAllow).clip(RoundedCornerShape(50))
                    .background(Divider).padding(horizontal = 14.dp, vertical = 8.dp))
            return@Column
        }
        val zone = ZoneId.systemDefault()
        val nowMs = now.atZone(zone).toInstant().toEpochMilli()
        val today = now.toLocalDate()
        val events = allEvents.filter { it.end > nowMs }  // drop events the moment they end
        val next = events.firstOrNull { !it.allDay }
        if (next == null) {
            Label("Up next")
            Spacer(Modifier.height(10.dp))
            Text("Nothing scheduled", fontSize = 26.sp, fontWeight = FontWeight.Light)
            Text("Today and tomorrow are clear", color = Faint, fontSize = 16.sp)
        } else {
            val begin = Instant.ofEpochMilli(next.begin).atZone(zone)
            val end = Instant.ofEpochMilli(next.end).atZone(zone)
            val happening = nowMs >= next.begin
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(9.dp).clip(CircleShape).background(if (next.color != 0) Color(next.color) else Accent))
                Spacer(Modifier.width(8.dp))
                Label(if (happening) "Now" else if (begin.toLocalDate() == today) "Up next" else "Tomorrow", Lilac)
            }
            Spacer(Modifier.height(8.dp))
            Text(next.title, fontSize = 23.sp, fontWeight = FontWeight.SemiBold, lineHeight = 27.sp,
                maxLines = 2, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(4.dp))
            Row {
                Text("${begin.format(clockFormat)} – ${end.format(clockFormat)}", color = Soft, fontSize = 18.sp)
                Text(if (happening) "   ends in ${relative(nowMs, next.end)}" else "   in ${relative(nowMs, next.begin)}",
                    color = Lilac, fontSize = 18.sp, fontWeight = FontWeight.Medium, maxLines = 1)
            }
            if (next.location.isNotBlank()) Text(shortLocation(next.location), color = Faint, fontSize = 15.sp,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(10.dp))
        }
        val later = events.filter { it !== next }
        if (later.isNotEmpty()) {
            Box(Modifier.fillMaxWidth().height(1.dp).background(Divider))
            Spacer(Modifier.height(10.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Label("Later", Lilac.copy(alpha = 0.7f))
                Spacer(Modifier.weight(1f))
                Text(if (later.size == 1) "1 event" else "${later.size} events", color = Faint, fontSize = 14.sp)
            }
            Spacer(Modifier.height(2.dp))
            // Every later event, scrollable; the edges fade so rows slide out softly.
            val scroll = rememberScrollState()
            Box(Modifier.weight(1f).fillMaxWidth()) {
                Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(bottom = 14.dp)) {
                    for (event in later) {
                        val start = Instant.ofEpochMilli(event.begin).atZone(zone)
                        val day = start.toLocalDate().coerceAtLeast(today)
                        val time = (if (day != today) start.format(dayFormat) + " " else "") +
                            (if (event.allDay) "All day" else start.format(clockFormat))
                        Row(Modifier.padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
                            Box(Modifier.size(8.dp).clip(CircleShape)
                                .background(if (event.color != 0) Color(event.color) else Accent))
                            Spacer(Modifier.width(10.dp))
                            Text(time, color = Soft, fontSize = 17.sp, modifier = Modifier.width(96.dp))
                            Text(event.title, fontSize = 17.sp, fontWeight = FontWeight.Medium, maxLines = 1,
                                overflow = TextOverflow.Ellipsis)
                        }
                    }
                }
                Box(Modifier.align(Alignment.BottomCenter).fillMaxWidth().height(16.dp)
                    .background(Brush.verticalGradient(listOf(Color.Transparent, Color(0xFF1B1529)))))
                if (scroll.value > 0) Box(Modifier.align(Alignment.TopCenter).fillMaxWidth().height(14.dp)
                    .background(Brush.verticalGradient(listOf(Color(0xFF211A33), Color.Transparent))))
            }
        }
    }
}

@Composable
private fun ConversationCard(state: Dashboard, now: LocalDateTime) {
    val nowMs = now.atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
    val cancellable = state.status in listOf("confirming", "waiting")
    val (title, color) = when (state.status) {
        "listening" -> "Listening" to Mint
        "transcribing" -> "Transcribing" to Mint
        "confirming" -> {
            val left = ((state.sendDelay * 1000L - (nowMs - state.statusSince) + 999) / 1000).coerceAtLeast(1)
            "Sending in $left" to Amber
        }
        "waiting" -> "Waiting for reply" to Amber
        "speaking" -> "Speaking" to Mint
        else -> "Done" to Soft
    }
    Column(Modifier.fillMaxSize().tile(if (cancellable) AmberTile else TealTile)
        .then(if (cancellable) Modifier.clickable { VoiceService.cancelRequest() } else Modifier)
        .padding(22.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Equalizer(color, active = state.status in listOf("listening", "speaking"))
            Spacer(Modifier.width(10.dp))
            Label(title, color)
        }
        Spacer(Modifier.height(16.dp))
        if (state.transcript.isNotBlank()) Text("“${state.transcript}”", fontSize = 26.sp, fontWeight = FontWeight.Light,
            lineHeight = 32.sp, maxLines = 4, overflow = TextOverflow.Ellipsis)
        if (cancellable) {
            Spacer(Modifier.weight(1f))
            Text(if (state.status == "confirming") "Tap to cancel" else "Tap to cancel and unsend",
                color = Faint, fontSize = 16.sp)
        }
        if (state.reply.isNotBlank() && state.status in listOf("speaking", "idle")) {
            Spacer(Modifier.weight(1f))
            Box(Modifier.fillMaxWidth().height(1.dp).background(Divider))
            Spacer(Modifier.height(12.dp))
            Text(state.reply, color = Soft, fontSize = 19.sp, lineHeight = 25.sp, maxLines = 4, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** Five bars that bounce while listening or speaking and sit still otherwise. */
@Composable
private fun Equalizer(color: Color, active: Boolean) {
    val transition = rememberInfiniteTransition(label = "eq")
    Row(Modifier.height(20.dp), horizontalArrangement = Arrangement.spacedBy(3.dp), verticalAlignment = Alignment.CenterVertically) {
        for (i in 0 until 5) {
            val level = if (!active) 0.35f else transition.animateFloat(0.25f, 1f,
                infiniteRepeatable(tween(380 + i * 90), RepeatMode.Reverse), label = "bar$i").value
            Box(Modifier.width(4.dp).fillMaxHeight(level).clip(RoundedCornerShape(2.dp)).background(color))
        }
    }
}

@Composable
private fun Dot(color: Color, pulse: Boolean) {
    val alpha = if (pulse) rememberInfiniteTransition(label = "pulse").animateFloat(0.35f, 1f,
        infiniteRepeatable(tween(700), RepeatMode.Reverse), label = "alpha").value else 1f
    Box(Modifier.size(9.dp).alpha(alpha).clip(CircleShape).background(color))
}

/** Voice state as a small tile that lights up: teal while listening or speaking, amber
 *  while sending or waiting, red when the microphone service is off. */
@Composable
private fun VoiceTile(state: Dashboard, modifier: Modifier) {
    val active = state.running && state.connection == "Connected" && state.status != "idle"
    val (text, brush, ink) = when {
        !state.running -> Triple("Voice off", Brush.linearGradient(listOf(Color(0xFF4A1F24), Color(0xFF301418))), Red)
        state.connection != "Connected" -> Triple(state.connection, AmberTile, Amber)
        state.status in listOf("confirming", "waiting") -> Triple(
            if (state.status == "waiting") "Waiting for reply" else "Sending…",
            Brush.linearGradient(listOf(Color(0xFFF7B733), Color(0xFFE08A12))), Color(0xFF2A1A00))
        active -> Triple(state.status.replaceFirstChar { it.uppercase() },
            Brush.linearGradient(listOf(Color(0xFF2DD4BF), Color(0xFF0E9F8E))), Color(0xFF032520))
        state.error.isNotBlank() -> Triple(state.error, Graphite, Amber)
        else -> Triple("Say “Hey Clippy”", Graphite, Mint)
    }
    Row(modifier.tile(brush).padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
        if (active) Equalizer(ink, active = state.status in listOf("listening", "speaking"))
        else Dot(ink, pulse = false)
        Spacer(Modifier.width(12.dp))
        Text(text, color = if (brush == Graphite) Ink else ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
            maxLines = 2, overflow = TextOverflow.Ellipsis, lineHeight = 19.sp)
    }
}

/** Pi health as three stats: hottest sensor, CPU and RAM. */
@Composable
private fun PiTile(pi: PiStatus?, connected: Boolean, now: LocalDateTime, modifier: Modifier) {
    val nowMs = now.atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
    val fresh = pi != null && connected && nowMs - pi.receivedAt < 90_000
    Row(modifier.tile(Graphite).padding(horizontal = 14.dp), verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween) {
        if (!fresh) {
            Dot(Red, pulse = false)
            Spacer(Modifier.width(10.dp))
            Text("Pi offline", fontSize = 16.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            return@Row
        }
        val strained = (pi!!.memPercent ?: 0.0) >= 85 || (pi.tempC ?: 0.0) >= 75
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Dot(if (strained) Amber else Mint, pulse = false)
            Spacer(Modifier.height(4.dp))
            Text("PI", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Faint, letterSpacing = 1.sp)
        }
        Stat(pi.tempC?.let { "${it.roundToInt()}°" }, "Temp")
        Stat(pi.cpuPercent?.let { "${it.roundToInt()}%" }, "CPU")
        Stat(pi.memPercent?.let { "${it.roundToInt()}%" }, "RAM")
    }
}

@Composable
private fun Stat(value: String?, label: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(value ?: "–", fontSize = 21.sp, fontWeight = FontWeight.Medium, lineHeight = 23.sp)
        Text(label.uppercase(), fontSize = 10.sp, fontWeight = FontWeight.SemiBold, color = Faint, letterSpacing = 1.sp)
    }
}

// ---------------------------------------------------------------- timers and the stopwatch

private val WarmTile = Brush.linearGradient(listOf(Color(0xFF3F2712), Color(0xFF22150A)))
private val RingTile = Brush.linearGradient(listOf(Color(0xFFFFB85C), Color(0xFFF26A3D)))
private val RingInk = Color(0xFF2B1204)

private fun clockText(ms: Long, tenths: Boolean = false): String {
    val total = ms / 1000
    val h = total / 3600
    val m = (total % 3600) / 60
    val sec = total % 60
    val base = if (h > 0) "%d:%02d:%02d".format(h, m, sec) else "%d:%02d".format(m, sec)
    return if (tenths) base + ".%d".format((ms % 1000) / 100) else base
}

private fun ClockItem.title() = when {
    kind == "stopwatch" -> "Stopwatch"
    label.isNotBlank() -> label.replaceFirstChar { it.uppercase() } + " timer"
    else -> "Timer"
}

private fun ClockItem.accent() = if (kind == "stopwatch") Mint else Tangerine

/** Time shown, and how full the ring is: a timer's ring drains, the stopwatch's sweeps each minute. */
private fun ClockItem.reading(now: Long): Pair<String, Float> =
    if (kind == "stopwatch") {
        val elapsed = elapsed(now)
        clockText(elapsed, tenths = true) to (elapsed % 60_000) / 60_000f
    } else {
        val left = remaining(now)
        clockText(left + 999) to if (durationMs > 0) left.toFloat() / durationMs else 0f
    }

@Composable
private fun TimersTile(items: List<ClockItem>, modifier: Modifier) {
    // The stopwatch shows tenths, so tick faster than the dashboard's once-a-second clock.
    val fast = items.any { it.kind == "stopwatch" && it.state == "running" }
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(fast) {
        while (true) {
            now = System.currentTimeMillis()
            delay(if (fast) 100 else 250)
        }
    }
    val ringing = items.filter { it.state == "ringing" }
    if (ringing.isNotEmpty()) {
        RingingTile(ringing, modifier)
        return
    }
    Column(modifier.tile(if (items.all { it.kind == "stopwatch" }) TealTile else WarmTile)
        .padding(horizontal = 20.dp, vertical = 16.dp)) {
        if (items.size == 1) BigClockItem(items[0], now)
        else Column(Modifier.fillMaxSize(), verticalArrangement = Arrangement.SpaceEvenly) {
            items.take(3).forEach { CompactClockItem(it, now, large = items.size == 2) }
        }
    }
}

@Composable
private fun ColumnScope.BigClockItem(item: ClockItem, now: Long) {
    val (time, fraction) = item.reading(now)
    val paused = item.state == "paused"
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Label(item.title(), item.accent())
            Text(time, fontSize = if (time.length > 7) 42.sp else 52.sp, fontWeight = FontWeight.Light,
                letterSpacing = (-2).sp, maxLines = 1, softWrap = false)
            Text(if (paused) "Paused" else if (item.kind == "stopwatch") "Running"
                 else "of " + clockText(item.durationMs), color = Soft, fontSize = 15.sp)
        }
        ProgressRing(fraction, item.accent(), paused, Modifier.size(84.dp))
    }
    Spacer(Modifier.weight(1f))
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        ActionPill(if (paused) "Resume" else "Pause", paused, onClick = {
            VoiceService.clockAction(item.id, if (paused) "resume" else "pause") })
        ActionPill(if (item.kind == "stopwatch") "Stop" else "Cancel", null, onClick = {
            VoiceService.clockAction(item.id, "stop") })
    }
}

@Composable
private fun CompactClockItem(item: ClockItem, now: Long, large: Boolean) {
    val (time, fraction) = item.reading(now)
    val paused = item.state == "paused"
    Row(verticalAlignment = Alignment.CenterVertically) {
        ProgressRing(fraction, item.accent(), paused, Modifier.size(if (large) 54.dp else 38.dp))
        Spacer(Modifier.width(14.dp))
        Column(Modifier.weight(1f)) {
            Text(time, fontSize = if (large) 32.sp else 24.sp, fontWeight = FontWeight.Light,
                lineHeight = if (large) 34.sp else 26.sp, maxLines = 1)
            Text(item.title() + if (paused) " · paused" else "", color = Soft, fontSize = if (large) 15.sp else 13.sp, maxLines = 1,
                overflow = TextOverflow.Ellipsis)
        }
        RoundButton(onClick = { VoiceService.clockAction(item.id, if (paused) "resume" else "pause") }) {
            if (paused) Icon(Icons.Filled.PlayArrow, "Resume", tint = Ink, modifier = Modifier.size(20.dp)) else PauseGlyph()
        }
        Spacer(Modifier.width(8.dp))
        RoundButton(onClick = { VoiceService.clockAction(item.id, "stop") }) {
            Icon(Icons.Filled.Close, "Cancel", tint = Ink, modifier = Modifier.size(18.dp))
        }
    }
}

@Composable
private fun RingingTile(ringing: List<ClockItem>, modifier: Modifier) {
    val pulse by rememberInfiniteTransition(label = "ring").animateFloat(0.85f, 1f,
        infiniteRepeatable(tween(600), RepeatMode.Reverse), label = "pulse")
    Column(modifier.tile(RingTile).alpha(pulse)
        .clickable { ringing.forEach { VoiceService.clockAction(it.id, "dismiss") } }
        .padding(horizontal = 22.dp, vertical = 18.dp)) {
        Label("Time's up", RingInk.copy(alpha = 0.7f))
        Spacer(Modifier.height(6.dp))
        Text(ringing.joinToString(", ") { it.title() }, color = RingInk, fontSize = 32.sp, fontWeight = FontWeight.SemiBold,
            lineHeight = 36.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.weight(1f))
        Text("Tap to dismiss, or say \u201cstop\u201d", color = RingInk, fontSize = 16.sp, fontWeight = FontWeight.Medium,
            modifier = Modifier.clip(RoundedCornerShape(50)).background(Color(0x33FFFFFF))
                .padding(horizontal = 14.dp, vertical = 8.dp))
    }
}

@Composable
private fun ProgressRing(fraction: Float, color: Color, paused: Boolean, modifier: Modifier) {
    Canvas(modifier) {
        val stroke = size.minDimension * 0.1f
        val inset = stroke / 2
        val arcSize = androidx.compose.ui.geometry.Size(size.width - stroke, size.height - stroke)
        drawArc(Color(0x26FFFFFF), 0f, 360f, false, Offset(inset, inset), arcSize, style = Stroke(stroke))
        drawArc(if (paused) color.copy(alpha = 0.45f) else color, -90f, 360f * fraction.coerceIn(0f, 1f), false,
            Offset(inset, inset), arcSize, style = Stroke(stroke, cap = StrokeCap.Round))
    }
}

@Composable
private fun ActionPill(text: String, resume: Boolean?, onClick: () -> Unit) {
    Row(Modifier.clip(RoundedCornerShape(50)).background(Color(0x1FFFFFFF)).clickable(onClick = onClick)
        .padding(horizontal = 16.dp, vertical = 9.dp), verticalAlignment = Alignment.CenterVertically) {
        when (resume) {
            true -> Icon(Icons.Filled.PlayArrow, null, tint = Ink, modifier = Modifier.size(18.dp))
            false -> PauseGlyph(14.dp)
            null -> Icon(Icons.Filled.Close, null, tint = Ink, modifier = Modifier.size(16.dp))
        }
        Spacer(Modifier.width(8.dp))
        Text(text, fontSize = 16.sp, fontWeight = FontWeight.Medium)
    }
}

@Composable
private fun RoundButton(onClick: () -> Unit, content: @Composable () -> Unit) {
    Box(Modifier.size(38.dp).clip(CircleShape).background(Color(0x1FFFFFFF)).clickable(onClick = onClick),
        contentAlignment = Alignment.Center) { content() }
}

/** Two bars; the core icon set has no pause icon. */
@Composable
private fun PauseGlyph(size: androidx.compose.ui.unit.Dp = 16.dp) {
    Row(Modifier.size(size), horizontalArrangement = Arrangement.SpaceEvenly) {
        repeat(2) { Box(Modifier.fillMaxHeight().width(size / 4).clip(RoundedCornerShape(2.dp)).background(Ink)) }
    }
}
