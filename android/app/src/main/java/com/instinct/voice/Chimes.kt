package com.instinct.voice

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import kotlin.math.PI
import kotlin.math.exp
import kotlin.math.sin

/** Short synthesized cues: a rising two-note chime when listening starts, and a quick
 *  three-note arpeggio when the request has been sent. Played on the assistant stream at
 *  the same volume as speech. */
object Chimes {
    private const val RATE = 24000

    // (frequency Hz, start s, length s) per note.
    private val LISTENING = listOf(Triple(659.3, 0.0, 0.16), Triple(987.8, 0.09, 0.22))
    private val SENT = listOf(Triple(1046.5, 0.0, 0.12), Triple(1318.5, 0.07, 0.12), Triple(1568.0, 0.14, 0.24))

    private val listening by lazy { render(LISTENING) }
    private val sent by lazy { render(SENT) }

    fun listening(volume: Float) = play(listening, volume)
    fun sent(volume: Float) = play(sent, volume)

    /** Sine notes with a soft attack and exponential decay, a little of the octave for
     *  brightness, mixed and normalized to about half of full scale. */
    private fun render(notes: List<Triple<Double, Double, Double>>): ShortArray {
        val total = notes.maxOf { it.second + it.third }
        val mix = DoubleArray((total * RATE).toInt())
        for ((freq, start, length) in notes) {
            val offset = (start * RATE).toInt()
            for (i in 0 until (length * RATE).toInt()) {
                val t = i.toDouble() / RATE
                val envelope = minOf(1.0, t / 0.008) * exp(-t / (length * 0.35))
                val tone = sin(2 * PI * freq * t) + 0.25 * sin(4 * PI * freq * t)
                if (offset + i < mix.size) mix[offset + i] += envelope * tone
            }
        }
        val peak = mix.maxOf { kotlin.math.abs(it) }.coerceAtLeast(1e-9)
        return ShortArray(mix.size) { (mix[it] / peak * 0.5 * Short.MAX_VALUE).toInt().toShort() }
    }

    private fun play(pcm: ShortArray, volume: Float) {
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setBufferSizeInBytes(pcm.size * 2)
            .setTransferMode(AudioTrack.MODE_STATIC)
            .build()
        track.write(pcm, 0, pcm.size)
        track.setVolume(volume.coerceIn(0f, 1f))
        track.setNotificationMarkerPosition(pcm.size)
        track.setPlaybackPositionUpdateListener(object : AudioTrack.OnPlaybackPositionUpdateListener {
            override fun onMarkerReached(t: AudioTrack) = t.release()
            override fun onPeriodicNotification(t: AudioTrack) {}
        })
        track.play()
    }
}
