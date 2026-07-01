package place.wong.shrimp.companion

/**
 * Stream-multiplexing frame codec for the phone port-forward relay. Must match
 * `src/open_shrimp/port_relay/frames.py` byte-for-byte:
 *
 *     [type:1][stream_id:4 big-endian][payload:...]
 */
object PortForwardFrames {
    const val TYPE_OPEN: Byte = 0x10
    const val TYPE_DATA: Byte = 0x11
    const val TYPE_CLOSE: Byte = 0x12
    const val TYPE_KEEPALIVE: Byte = 0x13

    const val HEADER_SIZE = 5

    fun encode(type: Byte, streamId: Int, payload: ByteArray = EMPTY, length: Int = payload.size): ByteArray {
        val frame = ByteArray(HEADER_SIZE + length)
        frame[0] = type
        frame[1] = (streamId ushr 24).toByte()
        frame[2] = (streamId ushr 16).toByte()
        frame[3] = (streamId ushr 8).toByte()
        frame[4] = streamId.toByte()
        System.arraycopy(payload, 0, frame, HEADER_SIZE, length)
        return frame
    }

    data class Frame(val type: Byte, val streamId: Int, val payload: ByteArray)

    fun decode(data: ByteArray): Frame? {
        if (data.size < HEADER_SIZE) return null
        val streamId =
            ((data[1].toInt() and 0xFF) shl 24) or
                ((data[2].toInt() and 0xFF) shl 16) or
                ((data[3].toInt() and 0xFF) shl 8) or
                (data[4].toInt() and 0xFF)
        return Frame(data[0], streamId, data.copyOfRange(HEADER_SIZE, data.size))
    }

    private val EMPTY = ByteArray(0)
}
