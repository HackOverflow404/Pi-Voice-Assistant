package com.instinct.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/** AirPlay from the Pi: shairport-sync's PCM (44.1 kHz, stereo, s16le), streamed by
 *  airplay/relay.py over USB (adb reverse tcp:8767) only while something is playing.
 *  It plays as media with audio focus: starting it pauses Spotify and the laptop's
 *  Bluetooth stream, they (and the assistant's speech) silence it in turn. The phone
 *  can't be paused from here, so a silenced stream is still read and discarded. */
class AirPlay(private val context: Context) {
    private val audio = context.getSystemService(AudioManager::class.java)
    private val attributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build()
    @Volatile private var gain = 1f
    private var track: AudioTrack? = null
    private var focus: AudioFocusRequest? = null
    private var written = 0L
    private var dropped = 0L
    private var lastError = ""

    suspend fun run() {
        while (currentCoroutineContext().isActive) {
            try {
                stream()
            } catch (cancel: CancellationException) {
                throw cancel
            } catch (error: Exception) {
                // No relay yet (Pi service down, no adb reverse): try again shortly. Each
                // distinct error is logged once, not every retry.
                val message = "${error.javaClass.simpleName}: ${error.message}"
                if (message != lastError) android.util.Log.w(TAG, "AirPlay link: $message")
                lastError = message
            } finally {
                stop()
            }
            delay(RETRY_MS)
        }
    }

    private suspend fun stream() {
        Socket().use { socket ->
            socket.connect(InetSocketAddress("127.0.0.1", PORT), 2000)
            socket.soTimeout = IDLE_MS
            val input = socket.getInputStream()
            val buffer = ByteArray(CHUNK_BYTES)
            var have = 0
            while (currentCoroutineContext().isActive) {
                val count = try {
                    input.read(buffer, have, buffer.size - have)
                } catch (_: SocketTimeoutException) {
                    stop()  // AirPlay paused or ended: give focus back to the Echo's other audio.
                    continue
                }
                if (count < 0) return
                have += count
                val usable = have - have % FRAME
                if (usable > 0) write(buffer, usable)
                buffer.copyInto(buffer, 0, usable, have)
                have -= usable
            }
        }
    }

    private fun write(pcm: ByteArray, size: Int) {
        val player = track ?: start()
        // Frames queued ahead of the speaker. The relay delivers in bursts; past MAX_BACKLOG the
        // Pi's clock has run ahead of the Echo's, so drop rather than let the delay grow. Only
        // once the speaker is reading: AudioFlinger starts a track only when its buffer is full.
        val head = player.playbackHeadPosition.toLong() and 0xffffffffL
        if (head > 0 && written - head > MAX_BACKLOG) {
            dropped += size / FRAME
            return
        }
        val result = player.write(pcm, 0, size, AudioTrack.WRITE_NON_BLOCKING)
        if (result > 0) written += result / FRAME
        if (result in 0 until size) dropped += (size - result) / FRAME
        if (player.playState != AudioTrack.PLAYSTATE_PLAYING && written >= PREFILL) player.play()
    }

    private fun start(): AudioTrack {
        val format = AudioFormat.Builder().setSampleRate(RATE).setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build()
        val minimum = AudioTrack.getMinBufferSize(RATE, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT)
        val player = AudioTrack.Builder().setAudioAttributes(attributes).setAudioFormat(format)
            .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(maxOf(minimum, BUFFER_BYTES)).build()
        check(player.state == AudioTrack.STATE_INITIALIZED) { "AirPlay output unavailable" }
        val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN).setAudioAttributes(attributes)
            .setOnAudioFocusChangeListener { change ->
                gain = when (change) {
                    AudioManager.AUDIOFOCUS_GAIN -> 1f
                    AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK -> 0.2f
                    else -> 0f
                }
                track?.setVolume(gain)
            }.build()
        gain = if (audio.requestAudioFocus(request) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED) 1f else 0f
        player.setVolume(gain)
        track = player
        focus = request
        written = 0
        dropped = 0
        android.util.Log.i(TAG, "AirPlay started")
        return player
    }

    private fun stop() {
        track?.let {
            runCatching { it.pause(); it.flush() }
            it.release()
            android.util.Log.i(TAG, "AirPlay stopped; dropped ${dropped * 1000 / RATE} ms of ${written * 1000 / RATE} ms")
        }
        focus?.let { audio.abandonAudioFocusRequest(it) }
        track = null
        focus = null
    }

    companion object {
        private const val TAG = "AirPlay"
        private const val PORT = 8767
        private const val RATE = 44100
        private const val FRAME = 4
        private const val CHUNK_BYTES = RATE * FRAME / 50        // 20 ms
        private const val BUFFER_BYTES = RATE * FRAME / 2        // 0.5 s
        private const val PREFILL = RATE * 15 / 100L              // 150 ms before starting
        private const val MAX_BACKLOG = RATE * 35 / 100L          // 350 ms
        private const val IDLE_MS = 1500
        private const val RETRY_MS = 3000L
    }
}
