package com.instinct.voice

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import kotlin.math.PI
import kotlin.math.exp
import kotlin.math.sin

/** Short synthesized cues in the style of a smart-speaker earcon: two soft, rounded notes a
 *  fifth apart, each sliding up into pitch ("bloop"). Rising when listening starts, falling
 *  once the request has been sent. Played on the assistant stream at the same volume as
 *  speech. */
object Chimes {
    private const val RATE = 24000

    // (frequency Hz, start s, length s) per note.
    private val LISTENING = listOf(Triple(329.6, 0.0, 0.26), Triple(493.9, 0.11, 0.34))  // E4 -> B4
    private val SENT = listOf(Triple(493.9, 0.0, 0.26), Triple(329.6, 0.11, 0.34))       // B4 -> E4

    private val listening by lazy { render(LISTENING) }
    private val sent by lazy { render(SENT) }

    fun listening(volume: Float) = play(listening, volume)
    fun sent(volume: Float) = play(sent, volume)

    /** Sine notes that start 3% flat and glide up within ~25 ms, with a soft 6 ms attack and
     *  a quick exponential decay; a trace of the octave for roundness. Mixed and normalized
     *  to about a third of full scale. */
    private fun render(notes: List<Triple<Double, Double, Double>>): ShortArray {
        val total = notes.maxOf { it.second + it.third }
        val mix = DoubleArray((total * RATE).toInt())
        for ((freq, start, length) in notes) {
            val offset = (start * RATE).toInt()
            var phase = 0.0
            for (i in 0 until (length * RATE).toInt()) {
                val t = i.toDouble() / RATE
                phase += 2 * PI * freq * (1 - 0.03 * exp(-t / 0.008)) / RATE
                val envelope = minOf(1.0, t / 0.006) * exp(-t / (length * 0.3))
                val tone = sin(phase) + 0.12 * sin(2 * phase)
                if (offset + i < mix.size) mix[offset + i] += envelope * tone
            }
        }
        val peak = mix.maxOf { kotlin.math.abs(it) }.coerceAtLeast(1e-9)
        return ShortArray(mix.size) { (mix[it] / peak * 0.35 * Short.MAX_VALUE).toInt().toShort() }
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
