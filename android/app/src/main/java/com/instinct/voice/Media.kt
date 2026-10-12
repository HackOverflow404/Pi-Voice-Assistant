package com.instinct.voice

import android.content.Context
import android.media.AudioManager
import android.view.KeyEvent

/** Gesture control of whatever is playing on the Echo (Spotify, or a phone over
 *  Bluetooth). Media keys go to the most recently active media session, as a headset's would. */
object Media {
    private fun audio(context: Context) = context.getSystemService(AudioManager::class.java)

    private fun key(context: Context, code: Int) {
        val audio = audio(context)
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
    }

    /** An open palm with nothing else to stop: pause what is playing, or resume. */
    fun toggle(context: Context) {
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
}
