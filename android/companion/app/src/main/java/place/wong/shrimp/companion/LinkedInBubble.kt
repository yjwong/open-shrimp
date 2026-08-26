package place.wong.shrimp.companion

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import android.widget.TextView
import place.wong.shrimp.companion.data.Prefs
import kotlin.math.abs

/**
 * The tap target that hands the thread on screen to OpenShrimp.
 *
 * It is up only while a LinkedIn thread is in the foreground, so its presence
 * is the promise that a tap has one conversation to point at. The inbox list
 * does not count: its rows carry truncated previews, and two threads with the
 * same person collide on that, so a tap there would hand over the wrong
 * conversation without saying so.
 *
 * `TYPE_APPLICATION_OVERLAY` under `SYSTEM_ALERT_WINDOW`, which LinkedIn
 * permits — its APK calls neither `setHideOverlayWindows`, which would make
 * this vanish over its windows, nor `setFilterTouchesWhenObscured`, which
 * would make LinkedIn ignore the user's own taps while it is up.
 *
 * Not focusable, deliberately. Taking input focus would make this the active
 * window, and the capture reads `rootInActiveWindow` — the thread underneath
 * has to stay the active one for there to be anything to read.
 *
 * Draggable with a remembered position, because the Telegram bubble already
 * competes for the same corner.
 */
class LinkedInBubble(
    private val context: Context,
    private val prefs: Prefs,
    private val onTap: () -> Unit,
) {
    private val windows = context.getSystemService(WindowManager::class.java)
    private val slop = ViewConfiguration.get(context).scaledTouchSlop

    private var view: TextView? = null
    private val params = WindowManager.LayoutParams(
        dp(SIZE_DP),
        dp(SIZE_DP),
        WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
        PixelFormat.TRANSLUCENT,
    ).apply {
        gravity = Gravity.TOP or Gravity.START
        x = prefs.linkedInBubbleX.takeIf { it != Prefs.NO_POSITION } ?: dp(DEFAULT_INSET_DP)
        y = prefs.linkedInBubbleY.takeIf { it != Prefs.NO_POSITION } ?: dp(DEFAULT_TOP_DP)
    }

    fun show() {
        if (view != null) return
        val bubble = TextView(context).apply {
            text = LABEL
            contentDescription = "Hand this LinkedIn conversation to OpenShrimp"
            setTextColor(Color.WHITE)
            textSize = TEXT_SP
            gravity = Gravity.CENTER
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(BUBBLE_COLOR)
            }
            elevation = dp(ELEVATION_DP).toFloat()
            setOnTouchListener(Drag())
        }
        windows.addView(bubble, params)
        view = bubble
    }

    fun hide() {
        val bubble = view ?: return
        view = null
        windows.removeView(bubble)
    }

    /**
     * Whether a tap is answered.
     *
     * A handover is one HTTP request the host answers by posting a card, so a
     * second tap while the first is in flight would make a second card. The
     * bubble dims instead of vanishing: a target that disappeared under the
     * finger would read as a missed tap.
     */
    fun setBusy(busy: Boolean) {
        view?.alpha = if (busy) BUSY_ALPHA else 1f
    }

    /**
     * Moving the bubble and tapping it are the same gesture until the finger
     * travels past the touch slop, after which it is a drag and never becomes
     * a tap. The position is written down on the way up rather than on every
     * move: a drag is dozens of frames and one place to end up.
     */
    private inner class Drag : View.OnTouchListener {
        private var startX = 0
        private var startY = 0
        private var touchX = 0f
        private var touchY = 0f
        private var dragging = false

        @SuppressLint("ClickableViewAccessibility")
        override fun onTouch(v: View, event: MotionEvent): Boolean {
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    startX = params.x
                    startY = params.y
                    touchX = event.rawX
                    touchY = event.rawY
                    dragging = false
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.rawX - touchX
                    val dy = event.rawY - touchY
                    if (!dragging && abs(dx) < slop && abs(dy) < slop) return true
                    dragging = true
                    params.x = startX + dx.toInt()
                    params.y = startY + dy.toInt()
                    view?.let { windows.updateViewLayout(it, params) }
                }
                MotionEvent.ACTION_UP -> {
                    if (dragging) {
                        prefs.saveLinkedInBubblePosition(params.x, params.y)
                    } else {
                        v.performClick()
                        onTap()
                    }
                }
            }
            return true
        }
    }

    private fun dp(value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()

    private companion object {
        /** An arrow away from the phone, which is what the tap does. */
        const val LABEL = "↗"

        const val SIZE_DP = 48
        const val ELEVATION_DP = 6
        const val DEFAULT_INSET_DP = 12
        const val DEFAULT_TOP_DP = 220
        const val TEXT_SP = 22f
        const val BUSY_ALPHA = 0.4f

        /** The app theme's primary, which nothing in LinkedIn's palette is. */
        const val BUBBLE_COLOR = 0xFF2457D6.toInt()
    }
}
