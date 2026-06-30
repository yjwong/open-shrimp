package place.wong.shrimp.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import android.os.Build
import androidx.annotation.RequiresApi

/**
 * Renders the per-ChatScope agent-status Live Update.
 *
 * The bot pushes three discrete transitions per turn — started,
 * permission_required, done — as FCM data messages.  On Android 16 (API 36)
 * the running/permission notification is an ongoing [Notification.ProgressStyle]
 * that requests promotion to the status-bar chip; on older devices it degrades
 * to a plain ongoing notification.  Each ChatScope keeps a single, stable
 * notification id so repeated events update the existing notification rather
 * than stacking, and the done event dismisses exactly the right one.
 *
 * Promotion is system-discretionary and realistically only one chip shows, so
 * only the permission-required segment requests promotion (it is the most
 * time-sensitive and the only actionable one).  All notifications share a
 * group + summary so the shade does not fill with chips.
 */
object AgentStatusNotifier {
    const val CHANNEL_ID = "agent_status"
    private const val GROUP_KEY = "place.wong.shrimp.companion.AGENT_STATUS"
    private const val SUMMARY_ID = 0x5A0001

    private const val STATE_STARTED = "started"
    private const val STATE_PERMISSION = "permission_required"
    private const val STATE_DONE = "done"

    // Value of the (API 36.1) Notification.EXTRA_REQUEST_PROMOTED_ONGOING constant,
    // hardcoded because the app compiles against API 36.0 where it is absent.
    private const val EXTRA_REQUEST_PROMOTED_ONGOING = "android.requestPromotedOngoing"

    /** Dispatch an ``agent_status`` FCM data message to the notification shade. */
    fun handle(context: Context, data: Map<String, String>) {
        val state = data["state"] ?: return
        val notificationId = notificationId(data) ?: return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return

        if (state == STATE_DONE) {
            manager.cancel(notificationId)
            val remaining = agentNotificationCount(manager, exceptId = notificationId)
            if (remaining == 0) manager.cancel(SUMMARY_ID)
            return
        }

        ensureChannel(manager)
        val permission = state == STATE_PERMISSION
        val notification = build(
            context = context,
            notificationId = notificationId,
            title = data["title"].orEmpty().ifEmpty { "OpenShrimp" },
            text = data["text"].orEmpty(),
            permission = permission,
            toolUseId = data["tool_use_id"],
        )
        manager.notify(notificationId, notification)
        manager.notify(SUMMARY_ID, buildSummary(context))
    }

    /**
     * Optimistically reflect a tapped approve/deny action before the bot's
     * follow-up ``started`` event arrives, so the buttons disappear at once.
     */
    fun markResolved(context: Context, notificationId: Int, decision: String) {
        if (notificationId == 0) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        ensureChannel(manager)
        val text = if (decision == "approve") "Approved — resuming…" else "Denied — resuming…"
        manager.notify(notificationId, baseBuilder(context, "OpenShrimp", text).build())
    }

    private fun build(
        context: Context,
        notificationId: Int,
        title: String,
        text: String,
        permission: Boolean,
        toolUseId: String?,
    ): Notification {
        val builder = baseBuilder(context, title, text)
        if (permission && !toolUseId.isNullOrEmpty()) {
            builder.addAction(action(context, notificationId, toolUseId, "approve", "Approve"))
            builder.addAction(action(context, notificationId, toolUseId, "deny", "Deny"))
        }
        if (Build.VERSION.SDK_INT >= 36) {
            applyProgressStyle(builder, permission)
            // Glanceable label rendered inside the status-bar chip when promoted.
            builder.setShortCriticalText(if (permission) "Approve?" else "Running")
            if (permission) {
                requestPromotion(builder)
            }
        }
        return builder.build()
    }

    /**
     * Ask the system to promote this notification to a status-bar chip (a "Live
     * Update").  The full builder API ([Notification.Builder.setRequestPromotedOngoing])
     * ships in Android 16 QPR1 (API 36.1), but this app compiles against API 36.0,
     * which lacks it; the runtime instead reads the [EXTRA_REQUEST_PROMOTED_ONGOING]
     * extra, whose value is the stable string below (matches
     * NotificationCompat.EXTRA_REQUEST_PROMOTED_ONGOING).  Setting it is what makes
     * the OS treat the notification as promotable — without it the notification is
     * posted normally and never becomes a chip.  Promotion also requires the
     * POST_PROMOTED_NOTIFICATIONS manifest permission and remains system-discretionary.
     */
    @RequiresApi(36)
    private fun requestPromotion(builder: Notification.Builder) {
        builder.addExtras(
            android.os.Bundle().apply { putBoolean(EXTRA_REQUEST_PROMOTED_ONGOING, true) },
        )
    }

    private fun baseBuilder(context: Context, title: String, text: String): Notification.Builder {
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(context, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(context)
        }
        return builder
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(title)
            .setContentText(text)
            .setCategory(Notification.CATEGORY_PROGRESS)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(contentIntent(context))
            .setGroup(GROUP_KEY)
    }

    @RequiresApi(36)
    private fun applyProgressStyle(builder: Notification.Builder, permission: Boolean) {
        // Three phased segments: started -> awaiting-approval -> finishing.
        val style = Notification.ProgressStyle()
        style.setProgressSegments(
            listOf(
                Notification.ProgressStyle.Segment(100),
                Notification.ProgressStyle.Segment(100),
                Notification.ProgressStyle.Segment(100),
            ),
        )
        style.setProgress(if (permission) 150 else 50)
        builder.setStyle(style)
    }

    private fun buildSummary(context: Context): Notification {
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(context, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(context)
        }
        return builder
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("OpenShrimp agents")
            .setGroup(GROUP_KEY)
            .setGroupSummary(true)
            .setOngoing(true)
            .build()
    }

    private fun action(
        context: Context,
        notificationId: Int,
        toolUseId: String,
        decision: String,
        label: String,
    ): Notification.Action {
        val intent = Intent(context, AgentApprovalReceiver::class.java).apply {
            action = "place.wong.shrimp.companion.AGENT_${decision.uppercase()}"
            putExtra(AgentApprovalReceiver.EXTRA_TOOL_USE_ID, toolUseId)
            putExtra(AgentApprovalReceiver.EXTRA_DECISION, decision)
            putExtra(AgentApprovalReceiver.EXTRA_NOTIFICATION_ID, notificationId)
        }
        val pendingIntent = PendingIntent.getBroadcast(
            context,
            (toolUseId + decision).hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val icon = Icon.createWithResource(
            context,
            if (decision == "approve") {
                android.R.drawable.ic_menu_save
            } else {
                android.R.drawable.ic_menu_close_clear_cancel
            },
        )
        return Notification.Action.Builder(icon, label, pendingIntent).build()
    }

    private fun contentIntent(context: Context): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        return PendingIntent.getActivity(
            context,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private fun ensureChannel(manager: NotificationManager) {
        if (Build.VERSION.SDK_INT < 26) return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Agent status",
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = "Live updates for running OpenShrimp agents"
        }
        manager.createNotificationChannel(channel)
    }

    private fun agentNotificationCount(manager: NotificationManager, exceptId: Int): Int =
        try {
            manager.activeNotifications.count {
                it.notification.group == GROUP_KEY && it.id != SUMMARY_ID && it.id != exceptId
            }
        } catch (_: Exception) {
            0
        }

    private fun notificationId(data: Map<String, String>): Int? {
        data["notification_id"]?.toIntOrNull()?.let { return it }
        return data["scope_key"]?.takeIf { it.isNotEmpty() }?.hashCode()
    }
}
