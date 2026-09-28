package com.instinct.voice

import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.RoundRect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathOperation
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import kotlin.math.cos
import kotlin.math.sin

private val SunColor = Color(0xFFFFD166)
private val MoonColor = Color(0xFFE9E4FF)
private val CloudColor = Color(0xFFEEF2F7)
private val StormCloud = Color(0xFFB4BDCA)
private val RainColor = Color(0xFF7CC6FF)

private enum class Sky { CLEAR, PARTLY, CLOUDY, FOG, RAIN, SNOW, STORM }

private fun sky(code: Int) = when (code) {
    0, 1 -> Sky.CLEAR
    2 -> Sky.PARTLY
    45, 48 -> Sky.FOG
    in 51..67, in 80..82 -> Sky.RAIN
    in 71..77, 85, 86 -> Sky.SNOW
    in 95..99 -> Sky.STORM
    else -> Sky.CLOUDY
}

/** Flat weather glyph for a WMO code, drawn to fit a square canvas. */
@Composable
fun WeatherIcon(code: Int, day: Boolean, modifier: Modifier = Modifier) = Canvas(modifier) {
    val s = size.minDimension
    val c = Offset(size.width / 2, size.height / 2)
    when (sky(code)) {
        Sky.CLEAR -> if (day) sun(c, s * 0.5f) else moon(c, s * 0.5f)
        Sky.PARTLY -> {
            val corner = Offset(c.x + s * 0.14f, c.y - s * 0.16f)
            if (day) sun(corner, s * 0.34f) else moon(corner, s * 0.34f)
            cloud(Offset(c.x - s * 0.06f, c.y + s * 0.14f), s * 0.74f, CloudColor)
        }
        Sky.CLOUDY -> cloud(Offset(c.x, c.y + s * 0.06f), s * 0.92f, CloudColor)
        Sky.FOG -> {
            cloud(Offset(c.x, s * 0.38f), s * 0.84f, CloudColor.copy(alpha = 0.75f))
            for ((y, inset) in listOf(0.72f to 0.14f, 0.86f to 0.26f)) drawLine(CloudColor.copy(alpha = 0.7f),
                Offset(s * inset, s * y), Offset(s * (1 - inset), s * y), s * 0.06f, StrokeCap.Round)
        }
        Sky.RAIN -> {
            cloud(Offset(c.x, s * 0.36f), s * 0.9f, CloudColor)
            for (i in -1..1) {
                val x = c.x + i * s * 0.2f
                drawLine(RainColor, Offset(x + s * 0.04f, s * 0.68f), Offset(x - s * 0.04f, s * 0.9f), s * 0.06f, StrokeCap.Round)
            }
        }
        Sky.SNOW -> {
            cloud(Offset(c.x, s * 0.36f), s * 0.9f, CloudColor)
            for ((dx, y) in listOf(-0.22f to 0.72f, 0f to 0.84f, 0.22f to 0.72f, -0.11f to 0.94f, 0.11f to 0.94f))
                drawCircle(CloudColor, s * 0.04f, Offset(c.x + dx * s, s * y))
        }
        Sky.STORM -> {
            cloud(Offset(c.x, s * 0.36f), s * 0.9f, StormCloud)
            drawPath(Path().apply {
                moveTo(c.x + s * 0.05f, s * 0.58f); lineTo(c.x - s * 0.11f, s * 0.8f); lineTo(c.x, s * 0.8f)
                lineTo(c.x - s * 0.06f, s * 0.98f); lineTo(c.x + s * 0.14f, s * 0.72f); lineTo(c.x + s * 0.03f, s * 0.72f)
                close()
            }, SunColor)
        }
    }
}

private fun DrawScope.sun(c: Offset, r: Float) {
    drawCircle(SunColor, r * 0.42f, c)
    for (k in 0 until 8) {
        val a = Math.toRadians(k * 45.0)
        val (dx, dy) = cos(a).toFloat() to sin(a).toFloat()
        drawLine(SunColor, Offset(c.x + dx * r * 0.62f, c.y + dy * r * 0.62f),
            Offset(c.x + dx * r * 0.86f, c.y + dy * r * 0.86f), r * 0.11f, StrokeCap.Round)
    }
}

private fun DrawScope.moon(c: Offset, r: Float) {
    val disc = Path().apply { addOval(Rect(c, r * 0.64f)) }
    val cut = Path().apply { addOval(Rect(Offset(c.x + r * 0.34f, c.y - r * 0.26f), r * 0.56f)) }
    drawPath(Path().apply { op(disc, cut, PathOperation.Difference) }, MoonColor)
}

/** Three overlapping discs on a rounded base, centred near [c], [w] wide. */
private fun DrawScope.cloud(c: Offset, w: Float, color: Color) {
    val h = w * 0.55f
    fun disc(x: Float, y: Float, r: Float) = Path().apply { addOval(Rect(Offset(x, y), r)) }
    var shape = Path().apply {
        addRoundRect(RoundRect(c.x - w * 0.42f, c.y, c.x + w * 0.42f, c.y + h * 0.44f, CornerRadius(h * 0.22f)))
    }
    for (part in listOf(disc(c.x - w * 0.22f, c.y + h * 0.08f, h * 0.36f),
            disc(c.x - w * 0.02f, c.y - h * 0.12f, h * 0.5f), disc(c.x + w * 0.24f, c.y + h * 0.05f, h * 0.38f))) {
        shape = Path().apply { op(shape, part, PathOperation.Union) }
    }
    drawPath(shape, color)
}
