package com.instinct.voice

import android.Manifest
import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.*
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import okhttp3.*
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

private sealed interface Incoming {
    data object Open : Incoming
    data class Text(val value: String) : Incoming
    data class Binary(val value: ByteString) : Incoming
}

class VoiceService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val client = OkHttpClient.Builder().pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(15, TimeUnit.SECONDS).readTimeout(0, TimeUnit.SECONDS).build()
    private var runner: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    @Volatile private var socket: WebSocket? = null
    @Volatile private var muted = true

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == "STOP") {
            Settings(this).enabled = false
            stopSelf()
            return START_NOT_STICKY
        }
        ensureChannel(this)
        val stop = PendingIntent.getService(this, 2,
            Intent(this, VoiceService::class.java).setAction("STOP"), PendingIntent.FLAG_IMMUTABLE)
        val notification = NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Pi Voice Assistant")
            .setContentText("Microphone active • tap to view dashboard")
            .setContentIntent(openDashboard(this)).setOngoing(true)
            .addAction(android.R.drawable.ic_media_pause, "Stop", stop).build()
        startForeground(1, notification)
        if (!Settings(this).enabled || ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (runner?.isActive != true) {
            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PiVoice:microphone").apply { acquire() }
            State.running(true)
            runner = scope.launch {
                var backoff = 1000L
                while (isActive) {
                    State.connection("Connecting")
                    val connectedAt = System.currentTimeMillis()
                    try {
                        connect()
                    } catch (cancel: CancellationException) {
                        throw cancel
                    } catch (error: Exception) {
                        State.connection("Reconnecting", error.message ?: "Connection interrupted")
                    }
                    if (System.currentTimeMillis() - connectedAt > 30000) backoff = 1000
                    delay(backoff)
                    backoff = (backoff * 2).coerceAtMost(30000)
                }
            }
        }
        return START_STICKY
    }

    private suspend fun connect() = coroutineScope {
        val settings = Settings(this@VoiceService)
        val incoming = Channel<Incoming>(64)
        val listener = object : WebSocketListener() {
            private fun offer(ws: WebSocket, event: Incoming) {
                if (incoming.trySend(event).isFailure) {
                    incoming.close(IllegalStateException("Audio download overflow"))
                    ws.cancel()
                }
            }
            override fun onOpen(ws: WebSocket, response: Response) { offer(ws, Incoming.Open) }
            override fun onMessage(ws: WebSocket, text: String) { offer(ws, Incoming.Text(text)) }
            override fun onMessage(ws: WebSocket, bytes: ByteString) { offer(ws, Incoming.Binary(bytes)) }
            override fun onFailure(ws: WebSocket, error: Throwable, response: Response?) {
                incoming.close(IllegalStateException("WebSocket failed: ${error.javaClass.simpleName}"))
            }
            override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                ws.close(code, reason)
                incoming.close(IllegalStateException("Server closed: $code $reason"))
            }
            override fun onClosed(ws: WebSocket, code: Int, reason: String) { incoming.close() }
        }
        muted = true
        val ws = client.newWebSocket(Request.Builder().url(settings.url)
            .header("Authorization", "Bearer ${settings.token}").build(), listener)
        socket = ws
        var mic: Job? = null
        val file = File(cacheDir, "reply.wav")
        var output: FileOutputStream? = null
        var audioId: String? = null
        var expected = 0L
        var received = 0L
        try {
            for (event in incoming) {
                when (event) {
                    Incoming.Open -> {
                        State.connection("Connected")
                        mic = launch { record(ws) }
                    }
                    is Incoming.Text -> {
                        val json = JSONObject(event.value)
                        when (json.getString("type")) {
                            "status" -> {
                                State.event(json)
                                muted = json.optString("status") !in listOf("idle", "listening")
                            }
                            "audio_start" -> {
                                check(output == null) { "Overlapping audio transfer" }
                                audioId = json.getString("id")
                                expected = json.getLong("bytes")
                                check(expected in 44..(32L * 1024 * 1024)) { "Invalid WAV size" }
                                received = 0
                                output = FileOutputStream(file)
                            }
                            "audio_end" -> {
                                check(output != null && json.getString("id") == audioId && received == expected) {
                                    "Incomplete WAV transfer"
                                }
                                output?.close(); output = null
                                var result = "playback_done"
                                try { play(file) }
                                catch (cancel: CancellationException) { throw cancel }
                                catch (_: Exception) {
                                    result = "playback_error"
                                    State.connection("Connected", "Could not play reply audio")
                                }
                                check(ws.send(JSONObject().put("type", result).put("id", audioId).toString()))
                                file.delete()
                                audioId = null
                            }
                        }
                    }
                    is Incoming.Binary -> {
                        check(output != null && received + event.value.size <= expected) { "Unexpected audio bytes" }
                        output?.write(event.value.toByteArray())
                        received += event.value.size
                    }
                }
            }
        } finally {
            muted = true
            withContext(NonCancellable) { mic?.cancelAndJoin() }
            output?.close()
            file.delete()
            ws.cancel()
            incoming.cancel()
            socket = null
        }
    }

    @Suppress("MissingPermission")
    private suspend fun record(ws: WebSocket) {
        val minimum = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        check(minimum > 0) { "16 kHz microphone input is not supported by this ROM" }
        val recorder = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000,
            AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minimum * 2, 10240))
        try {
            check(recorder.state == AudioRecord.STATE_INITIALIZED) { "Microphone initialization failed" }
            recorder.startRecording()
            check(recorder.recordingState == AudioRecord.RECORDSTATE_RECORDING) { "Microphone access denied" }
            val pcm = ShortArray(1280)
            while (currentCoroutineContext().isActive) {
                val count = recorder.read(pcm, 0, pcm.size, AudioRecord.READ_BLOCKING)
                check(count > 0) { "Microphone read failed: $count" }
                val bytes = ByteArray(count * 2)
                if (!muted) for (i in 0 until count) {
                    bytes[i * 2] = (pcm[i].toInt() and 255).toByte()
                    bytes[i * 2 + 1] = (pcm[i].toInt() shr 8).toByte()
                }
                check(ws.queueSize() < 64000) { "Network too slow for live microphone audio" }
                check(ws.send(bytes.toByteString())) { "Microphone connection closed" }
            }
        } finally {
            if (recorder.recordingState == AudioRecord.RECORDSTATE_RECORDING) recorder.stop()
            recorder.release()
        }
    }

    private suspend fun play(file: File) {
        val manager = getSystemService(AudioManager::class.java)
        val attributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
        val player = MediaPlayer()
        val focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
            .setAudioAttributes(attributes).setOnAudioFocusChangeListener { change ->
                if (change == AudioManager.AUDIOFOCUS_LOSS || change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) {
                    if (player.isPlaying) player.pause()
                }
            }.build()
        try {
            check(manager.requestAudioFocus(focus) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED) { "Audio focus unavailable" }
            player.setAudioAttributes(attributes)
            player.setDataSource(file.absolutePath)
            player.prepare()
            val completed = withTimeoutOrNull(player.duration.toLong() + 10000) {
                suspendCancellableCoroutine<Unit> { continuation ->
                    player.setOnCompletionListener { if (continuation.isActive) continuation.resume(Unit) }
                    player.setOnErrorListener { _, what, extra ->
                        if (continuation.isActive) continuation.resumeWithException(IllegalStateException("Playback $what/$extra"))
                        true
                    }
                    player.start()
                }
                true
            }
            check(completed == true) { "Playback timed out" }
        } finally {
            manager.abandonAudioFocusRequest(focus)
            player.release()
        }
    }

    override fun onDestroy() {
        socket?.cancel()
        scope.cancel()
        runner?.invokeOnCompletion {
            wakeLock?.let { if (it.isHeld) it.release() }
            client.dispatcher.executorService.shutdown()
            client.connectionPool.evictAll()
        }
        State.running(false)
        State.connection("Stopped")
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    companion object {
        private const val CHANNEL = "voice"
        private fun ensureChannel(context: Context) {
            context.getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(CHANNEL, "Voice assistant", NotificationManager.IMPORTANCE_LOW))
        }
        private fun openDashboard(context: Context) = PendingIntent.getActivity(context, 0,
            Intent(context, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        fun showBootNotice(context: Context) {
            ensureChannel(context)
            if (android.os.Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(context,
                    Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
            context.getSystemService(NotificationManager::class.java).notify(2,
                NotificationCompat.Builder(context, CHANNEL).setSmallIcon(android.R.drawable.ic_btn_speak_now)
                    .setContentTitle("Start Pi Voice Assistant")
                    .setContentText("Tap Start in the dashboard to enable the microphone")
                    .setContentIntent(openDashboard(context)).setAutoCancel(true).build())
        }
    }
}
