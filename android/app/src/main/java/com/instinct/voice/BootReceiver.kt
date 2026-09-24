package com.instinct.voice

import android.Manifest
import android.app.admin.DeviceAdminReceiver
import android.app.admin.DevicePolicyManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat

class OwnerReceiver : DeviceAdminReceiver()

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val settings = Settings(context)
        if (!settings.enabled || settings.token.isBlank()) return
        val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        val owner = context.getSystemService(DevicePolicyManager::class.java)
            .isDeviceOwnerApp(context.packageName)
        if (granted && (Build.VERSION.SDK_INT < 30 || owner)) {
            try {
                ContextCompat.startForegroundService(context, Intent(context, VoiceService::class.java))
            } catch (_: RuntimeException) {
                VoiceService.showBootNotice(context)
            }
        } else {
            VoiceService.showBootNotice(context)
        }
    }
}
