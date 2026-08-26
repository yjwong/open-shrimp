package place.wong.shrimp.companion

import android.accessibilityservice.AccessibilityService
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.Toast
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import place.wong.shrimp.companion.data.LinkedInCapture
import place.wong.shrimp.companion.data.LinkedInHandover
import place.wong.shrimp.companion.data.LinkedInIdMissing
import place.wong.shrimp.companion.data.HandoverText
import place.wong.shrimp.companion.data.LinkedInNode
import place.wong.shrimp.companion.data.LinkedInReader
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi

/**
 * Puts a bubble over LinkedIn's thread screen, and hands the thread under it
 * to OpenShrimp when the bubble is tapped.
 *
 * Nothing polls and nothing runs on its own: this reads the screen at the
 * moment of a tap and at no other time. The only thing it does between taps is
 * decide whether the bubble is up, which is a search for one resource id.
 *
 * The bubble tap is the first of the two human gates. The card the host posts
 * is inert until someone taps Pick up in Telegram, which is the second, so a
 * conversation captured here reaches no agent turn by itself.
 *
 * Nothing it reads may reach a log or a notification. The lines it writes
 * carry counts and resource ids; a message body, a name and a headline are all
 * written by whoever is messaging the user, and the only place any of them
 * belongs is inside the untrusted envelope the host wraps the event in.
 *
 * The service is deliberately not filtered to `com.linkedin.android` in its
 * configuration. A filter there would mean no event ever arrives from the app
 * the user switched *to*, so the bubble would be stranded over every other
 * screen on the phone. The filter is [isLinkedIn] instead, applied before
 * anything but a package name is read.
 */
class LinkedInAccessibilityService : AccessibilityService() {
    private val handler = Handler(Looper.getMainLooper())
    private val io = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val api = ServerApi()

    private lateinit var prefs: Prefs
    private var bubble: LinkedInBubble? = null
    private var sending = false
    private var lookQueued = false
    private var watchingContent = false

    private val look = Runnable {
        lookQueued = false
        updateBubble()
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        // Off the event path: the first read parses the preferences file off
        // disk, and this service can be the only thing alive in the process,
        // so doing it inside a dispatch would stall one.
        prefs = Prefs(this)
        bubble = LinkedInBubble(this, prefs, ::onBubbleTapped)
        // The service can be switched on while a thread is already open, and
        // nothing will change on screen to say so.
        updateBubble()
        LogStore.add("LinkedIn capture is on")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        val from = event.packageName?.toString()
        // The bubble is a window, and showing it announces itself as one. A
        // service that read its own announcement as "another app came up"
        // would take the bubble down the frame after putting it up, and show
        // it again on whatever LinkedIn sent next — a bubble that appears only
        // while a thread is being scrolled. An unattributed event says nothing
        // about what is in front either.
        if (from == null || from == packageName) return
        if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            watchContent(from == PACKAGE)
            handler.removeCallbacks(look)
            if (from == PACKAGE) handler.post(look) else hideBubble()
            return
        }
        // Text and content descriptions change on every frame of a scroll and
        // on every keystroke, and neither can add or remove a fragment. The
        // change that matters — a subtree appearing — is reported as its own
        // type, so it still gets a look.
        val kinds = event.contentChangeTypes
        if (kinds != 0 && kinds and TEXT_CHANGES.inv() == 0) return
        if (from == PACKAGE && !lookQueued) {
            lookQueued = true
            handler.postDelayed(look, LOOK_MS)
        }
    }

    override fun onInterrupt() = hideBubble()

    override fun onUnbind(intent: Intent?): Boolean {
        handler.removeCallbacks(look)
        hideBubble()
        io.cancel()
        LogStore.add("LinkedIn capture is off")
        return super.onUnbind(intent)
    }

    /**
     * Subscribe to LinkedIn's content events only while LinkedIn is in front.
     *
     * The framework works out, per app, whether any enabled service wants its
     * content events, and an app nobody is listening to skips producing them
     * altogether. A service that stays subscribed therefore makes every app on
     * the phone build and post events all day for a bubble that only ever
     * appears over one of them.
     *
     * Window-state events stay subscribed throughout and unfiltered, because
     * they are what says the user switched away — and an app the bubble is not
     * over is exactly the app that would never tell us.
     */
    private fun watchContent(on: Boolean) {
        if (on == watchingContent) return
        watchingContent = on
        serviceInfo = serviceInfo.apply {
            eventTypes = if (on) {
                AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED or
                    AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
            } else {
                AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
            }
        }
    }

    /**
     * Show the bubble over a thread and take it away everywhere else.
     *
     * A null root is a window in the middle of changing rather than an answer,
     * and taking the bubble away on one would make it flicker through every
     * scroll that outran the tree.
     */
    private fun updateBubble() {
        val root = rootInActiveWindow ?: return
        val linkedIn = root.packageName?.toString() == PACKAGE
        // Also asked here, not only on a window-state change, because there is
        // no such change to wait for when the service is switched on with a
        // thread already open — and without content events the bubble would
        // then stay up all the way back out to the inbox list.
        watchContent(linkedIn)
        if (linkedIn && root.byId(LinkedInCapture.FRAGMENT) != null) {
            showBubble()
        } else {
            hideBubble()
        }
    }

    private fun showBubble() {
        try {
            bubble?.show()
        } catch (e: Exception) {
            // Almost always the overlay permission, which is granted per app
            // in a settings screen the accessibility toggle does not imply.
            LogStore.add("LinkedIn capture: the bubble could not be drawn — ${e.message}")
        }
    }

    private fun hideBubble() {
        bubble?.hide()
    }

    /**
     * Capture the thread on screen and send it, once.
     *
     * The read happens here on the main thread because that is where the
     * accessibility tree is addressable at all; only the HTTP request goes to
     * the background. A capture is a few hundred nodes and costs a frame or
     * two of the LinkedIn app the user is looking at.
     */
    private fun onBubbleTapped() {
        if (sending) return
        val screen = try {
            capture()
        } catch (e: LinkedInIdMissing) {
            reportBreakage(e.resourceId)
            return
        } catch (e: Exception) {
            report("The conversation could not be captured — ${e.message}", null)
            return
        }
        sending = true
        bubble?.setBusy(true)
        io.launch {
            try {
                // Asked before the store read, which is a root grant and a
                // copy of a database: an unpaired phone has nowhere to send
                // the answer, so paying for one would be work done to throw
                // away.
                val baseUrl = prefs.baseUrl
                val deviceId = prefs.deviceId
                check(baseUrl.isNotEmpty() && deviceId != null) { HandoverText.NOT_PAIRED }
                val handover = enrich(screen)
                val result = api.sendLinkedInHandover(baseUrl, deviceId, handover)
                prefs.linkedInBrokenId = null
                // A count and a fidelity, never content.
                LogStore.add(
                    "Handed over ${handover.messages.size} LinkedIn messages" +
                        if (handover.storeRead) " from the store" else " from the screen",
                )
                report(HandoverText.SENT, result.deepLink)
            } catch (e: Exception) {
                report(e.message ?: "The conversation could not be sent", null)
            } finally {
                withContext(Dispatchers.Main) {
                    sending = false
                    bubble?.setBusy(false)
                }
            }
        }
    }

    /**
     * *screen* with what only LinkedIn's own store holds, or *screen* itself.
     *
     * The two halves fail independently on purpose. A bind that is refused,
     * a store that has been signed out and cleared, or a thread whose text
     * nothing in the store matches all leave the capture that was already
     * made: the same conversation without profile links, message ids, the
     * InMail flag, or anything that was above the viewport, which the card
     * says for itself through `store_read`.
     *
     * The reason is logged as the reader wrote it, which is a count or a file
     * name. Nothing the store holds may reach a log.
     */
    private fun enrich(screen: LinkedInHandover): LinkedInHandover =
        try {
            LinkedInReader.handover(this, screen)
        } catch (e: Exception) {
            LogStore.add("LinkedIn store not read — ${e.message}")
            screen
        }

    /**
     * The thread on screen, as the host's endpoint takes it.
     *
     * Everything the walk needs is addressed by resource id, so none of this
     * matches on position or on a localised string. A vanished id stops the
     * capture rather than narrowing it — see [LinkedInIdMissing].
     */
    private fun capture(): LinkedInHandover {
        val root = rootInActiveWindow
            ?: error("the LinkedIn window went away before it could be read")
        val screen = walk(root)
        if (!screen.insideFragment) throw LinkedInIdMissing(LinkedInCapture.FRAGMENT)
        val title = screen.singles[LinkedInCapture.TOOLBAR_TITLE]?.textOf()
            ?: throw LinkedInIdMissing(LinkedInCapture.TOOLBAR_TITLE)
        // The thread screen is confirmed by this point, so a list that is not
        // in it is a layout that moved rather than a thread without one.
        val list = screen.singles[LinkedInCapture.MESSAGE_LIST]
            ?: throw LinkedInIdMissing(LinkedInCapture.MESSAGE_LIST)

        return LinkedInCapture.read(
            nodes = screen.nodes,
            title = title,
            headline = screen.singles[LinkedInCapture.OCCUPATION]?.textOf(),
            // What the list will still scroll to, rather than what it holds:
            // a RecyclerView offers the backward action only while there is
            // something above the viewport to reach.
            canScrollBack = list.actionList.any {
                it.id == AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_BACKWARD.id
            },
        )
    }

    /** What one walk of the screen found. */
    private class Screen {
        /** The first node carrying each id the capture needs exactly one of. */
        val singles = HashMap<String, AccessibilityNodeInfo>()

        /** The message stream, in the order it is laid out. */
        val nodes = ArrayList<LinkedInNode>()

        var insideFragment = false
    }

    /**
     * Read everything the capture needs in one traversal.
     *
     * A search per id would be a full walk of LinkedIn's tree per id, across
     * the process boundary, and five of them for one tap. This is the same
     * answer for one.
     *
     * Order is the meaning for the message stream, which is why it is a walk
     * and not a set of lookups: LinkedIn labels the first message of a run
     * with its sender and puts the date on a header row of its own, so a body
     * only knows who wrote it and when from what stood above it.
     *
     * The stream is collected only under `message_list_fragment`. `id/body` is
     * a generic name this app uses on other screens too, and a bottom sheet
     * over the thread would otherwise put its own text in the transcript.
     */
    private fun walk(root: AccessibilityNodeInfo): Screen {
        val screen = Screen()
        var budget = MAX_NODES

        fun visit(node: AccessibilityNodeInfo, inFragment: Boolean) {
            if (budget-- <= 0) return
            val id = node.viewIdResourceName
            val within = inFragment || id == LinkedInCapture.FRAGMENT
            if (id != null) {
                if (id in LinkedInCapture.SINGLE) screen.singles.putIfAbsent(id, node)
                if (within) {
                    screen.insideFragment = true
                    if (id in LinkedInCapture.COLLECTED) {
                        node.textOf()?.let { screen.nodes.add(LinkedInNode(id, it)) }
                    }
                }
            }
            for (i in 0 until node.childCount) visit(node.getChild(i) ?: continue, within)
        }

        visit(root, false)
        return screen
    }

    private fun AccessibilityNodeInfo.byId(id: String): AccessibilityNodeInfo? =
        findAccessibilityNodeInfosByViewId(id)?.firstOrNull()

    private fun AccessibilityNodeInfo.textOf(): String? =
        (text ?: contentDescription)?.toString()?.trim()?.ifEmpty { null }

    /**
     * Say what happened where the person who tapped is looking, which is the
     * LinkedIn app and not this one.
     *
     * The toast is the answer to the tap; the notification is what survives it
     * and carries the link into the topic the card landed in.
     */
    private fun report(message: String, deepLink: String?) {
        handler.post {
            Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
            notify(message, deepLink)
        }
    }

    /**
     * A resource id the thread screen no longer has, reported as a bug report
     * naming it.
     *
     * The screen is server-driven enough that this can land without an app
     * update to notice it, so the one thing worth saying is which id went.
     */
    private fun reportBreakage(resourceId: String) {
        LogStore.add("LinkedIn capture: $resourceId is gone from the thread screen")
        // Written down as well as said. A toast over another app lasts two
        // seconds and a notification is one swipe from gone, so someone who
        // hands a thread over once a week meets this long after it broke, with
        // nothing left on screen to explain why taps stopped working.
        prefs.linkedInBrokenId = resourceId
        report("LinkedIn's layout changed: $resourceId is gone. Nothing was sent.", null)
    }

    private fun notify(message: String, deepLink: String?) {
        val manager = getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) == null) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "LinkedIn handovers",
                    NotificationManager.IMPORTANCE_DEFAULT,
                ),
            )
        }
        val builder = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_upload_done)
            .setContentTitle("LinkedIn conversation")
            .setContentText(message)
            .setAutoCancel(true)
        deepLink?.let {
            builder.setContentIntent(
                PendingIntent.getActivity(
                    this,
                    0,
                    Intent(Intent.ACTION_VIEW, Uri.parse(it))
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                ),
            )
        }
        manager.notify(NOTIFICATION_ID, builder.build())
    }

    private companion object {
        const val PACKAGE = "com.linkedin.android"

        /** How often the screen is looked at while LinkedIn is busy drawing. */
        const val LOOK_MS = 400L

        /**
         * The content changes that cannot add or remove a fragment, so cannot
         * change whether the bubble belongs on screen.
         */
        const val TEXT_CHANGES = AccessibilityEvent.CONTENT_CHANGE_TYPE_TEXT or
            AccessibilityEvent.CONTENT_CHANGE_TYPE_CONTENT_DESCRIPTION

        /**
         * How many nodes one capture may walk.
         *
         * A thread screen is a few hundred, so this bounds a tree that is not
         * the one this was written for rather than a long conversation.
         * Running out stops the walk instead of failing it: what has been
         * collected is still a window onto the thread, and the truncation flag
         * already says so.
         */
        const val MAX_NODES = 4_000

        const val CHANNEL_ID = "linkedin_handover"

        /** One id, so a second handover replaces the first rather than stacking. */
        const val NOTIFICATION_ID = 0x5A0002
    }
}
