// androidx.media (MediaSessionCompat/MediaStyle) is deprecated in favour of
// Media3 (androidx.media3:media3-session). It still works; migrating means
// wrapping the PC remote-control commands in a Media3 Player facade.
@file:Suppress("DEPRECATION")

package com.lantooth

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.support.v4.media.session.MediaSessionCompat
import android.support.v4.media.session.PlaybackStateCompat
import androidx.core.app.NotificationCompat
import androidx.media.app.NotificationCompat.MediaStyle

/**
 * Manages the MediaSessionCompat that puts Bluetooth-style media buttons into
 * the system notification shade and lock screen.
 *
 * Button taps call [onCommand] with one of Protocol.CMD_* values.
 */
class MediaControlManager(
    private val context: Context,
    private val onCommand: (Byte, Int) -> Unit,
) {
    private val session = MediaSessionCompat(context, "LANTooth").apply {
        setFlags(
            MediaSessionCompat.FLAG_HANDLES_MEDIA_BUTTONS or
            MediaSessionCompat.FLAG_HANDLES_TRANSPORT_CONTROLS
        )
        setCallback(object : MediaSessionCompat.Callback() {
            override fun onPlay()         { onCommand(Protocol.CMD_PLAY_PAUSE, 0) }
            override fun onPause()        { onCommand(Protocol.CMD_PLAY_PAUSE, 0) }
            override fun onSkipToNext()   { onCommand(Protocol.CMD_NEXT_TRACK, 0) }
            override fun onSkipToPrevious() { onCommand(Protocol.CMD_PREV_TRACK, 0) }
            override fun onStop()         { onCommand(Protocol.CMD_STOP, 0) }
        })
        isActive = true
    }

    val token: MediaSessionCompat.Token get() = session.sessionToken

    @Volatile private var isPlaying = true
    @Volatile var isMicActive = false
        private set

    fun updatePlayState(playing: Boolean) {
        isPlaying = playing
        val state = PlaybackStateCompat.Builder()
            .setActions(
                PlaybackStateCompat.ACTION_PLAY_PAUSE or
                PlaybackStateCompat.ACTION_SKIP_TO_NEXT or
                PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS or
                PlaybackStateCompat.ACTION_STOP
            )
            .setState(
                if (playing) PlaybackStateCompat.STATE_PLAYING else PlaybackStateCompat.STATE_PAUSED,
                PlaybackStateCompat.PLAYBACK_POSITION_UNKNOWN,
                1f,
            )
            .build()
        session.setPlaybackState(state)
    }

    fun setMicActive(active: Boolean) {
        isMicActive = active
    }

    fun buildNotification(
        channelId: String,
        notifTitle: String,
        notifText: String,
    ): NotificationCompat.Builder {
        val micAction = if (isMicActive) {
            NotificationCompat.Action(
                android.R.drawable.ic_btn_speak_now,
                "Mute mic",
                buildAction(ACTION_MIC_OFF),
            )
        } else {
            NotificationCompat.Action(
                android.R.drawable.ic_lock_silent_mode,
                "Unmute mic",
                buildAction(ACTION_MIC_ON),
            )
        }

        return NotificationCompat.Builder(context, channelId)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(notifTitle)
            .setContentText(notifText)
            .setOngoing(true)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .addAction(android.R.drawable.ic_media_previous, "Previous",
                buildAction(ACTION_PREV))
            .addAction(
                if (isPlaying) android.R.drawable.ic_media_pause else android.R.drawable.ic_media_play,
                if (isPlaying) "Pause" else "Play",
                buildAction(ACTION_PLAY_PAUSE),
            )
            .addAction(android.R.drawable.ic_media_next, "Next",
                buildAction(ACTION_NEXT))
            .addAction(micAction)
            .setStyle(
                MediaStyle()
                    .setMediaSession(token)
                    .setShowActionsInCompactView(0, 1, 2)
            )
    }

    fun release() {
        session.isActive = false
        session.release()
    }

    // ---------------------------------------------------------------------------
    // Internal helpers
    // ---------------------------------------------------------------------------

    private fun buildAction(action: String): PendingIntent {
        val intent = Intent(context, StreamService::class.java).apply {
            this.action = action
        }
        return PendingIntent.getService(
            context, action.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    companion object {
        const val ACTION_PLAY_PAUSE = "com.lantooth.PLAY_PAUSE"
        const val ACTION_NEXT       = "com.lantooth.NEXT"
        const val ACTION_PREV       = "com.lantooth.PREV"
        const val ACTION_MIC_ON     = "com.lantooth.MIC_ON"
        const val ACTION_MIC_OFF    = "com.lantooth.MIC_OFF"
    }
}
