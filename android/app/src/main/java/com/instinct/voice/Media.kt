package com.instinct.voice

import android.content.Context
import android.media.AudioManager
import android.view.KeyEvent

/** Gesture and presence control of whatever is playing on the Echo (Spotify, or a phone over
 *  Bluetooth). Media keys go to the most recently active media session, as a headset's would. */
object Media {
    // Set when leaving paused the music, so coming back resumes only what this paused.
    @Volatile private var pausedForAway = false

    private fun audio(context: Context) = context.getSystemService(AudioManager::class.java)

    private fun key(context: Context, code: Int) {
        val audio = audio(context)
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
    }

    /** An open palm with nothing else to stop: pause what is playing, or resume. */
    fun toggle(context: Context) {
        pausedForAway = false
        if (audio(context).isMusicActive) {
            key(context, KeyEvent.KEYCODE_MEDIA_PAUSE)
            State.toast("Paused", "pause")
        } else {
            key(context, KeyEvent.KEYCODE_MEDIA_PLAY)
            State.toast("Playing", "play")
        }
    }

    fun next(context: Context) {
        key(context, KeyEvent.KEYCODE_MEDIA_NEXT)
        State.toast("Next", "next")
    }

    fun previous(context: Context) {
        key(context, KeyEvent.KEYCODE_MEDIA_PREVIOUS)
        State.toast("Previous", "previous")
    }

    /** Presence changed. Leaving pauses music; coming back resumes it only if leaving paused it
     *  and nothing has started it since. "unknown" (camera service down) changes nothing. */
    fun zone(context: Context, previous: String, zone: String) {
        if (zone == "away" && previous != "away") {
            if (audio(context).isMusicActive) {
                key(context, KeyEvent.KEYCODE_MEDIA_PAUSE)
                pausedForAway = true
            }
        } else if (previous == "away" && (zone == "near" || zone == "far")) {
            if (pausedForAway && !audio(context).isMusicActive) {
                key(context, KeyEvent.KEYCODE_MEDIA_PLAY)
                State.toast("Welcome back", "play")
            }
            pausedForAway = false
        }
    }
}
