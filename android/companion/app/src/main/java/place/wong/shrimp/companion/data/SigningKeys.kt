package place.wong.shrimp.companion.data

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import java.nio.charset.StandardCharsets
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64
import java.util.UUID

/**
 * Owns the AndroidKeyStore EC signing key and the request-signing scheme shared by every
 * authenticated call to OpenShrimp. Used from both UI ViewModels and the FCM service.
 */
object SigningKeys {
    private const val ANDROID_KEYSTORE = "AndroidKeyStore"
    private const val KEY_ALIAS = "openshrimp_companion_signing"

    fun ensureKeyPair(): KeyPair {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        val private = keyStore.getKey(KEY_ALIAS, null) as? PrivateKey
        val public = keyStore.getCertificate(KEY_ALIAS)?.publicKey
        if (private != null && public != null) {
            return KeyPair(public, private)
        }
        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, ANDROID_KEYSTORE)
        generator.initialize(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .build(),
            SecureRandom(),
        )
        return generator.generateKeyPair()
    }

    fun publicKeyBase64Url(): String = ensureKeyPair().public.encoded.base64Url()

    fun sign(builder: Request.Builder, method: String, url: String, body: String, deviceId: String): Request.Builder {
        val timestamp = (System.currentTimeMillis() / 1000).toString()
        val nonce = UUID.randomUUID().toString()
        val httpUrl = url.toHttpUrl()
        val path = httpUrl.encodedPath + if (httpUrl.encodedQuery != null) "?${httpUrl.encodedQuery}" else ""
        val bodyHash = MessageDigest.getInstance("SHA-256")
            .digest(body.toByteArray(StandardCharsets.UTF_8))
            .base64Url()
        val payload = listOf(method.uppercase(), path, timestamp, nonce, bodyHash).joinToString("\n")
        val signature = Signature.getInstance("SHA256withECDSA").run {
            initSign(privateKey())
            update(payload.toByteArray(StandardCharsets.UTF_8))
            sign().base64Url()
        }
        return builder
            .header("X-OpenShrimp-Device-Id", deviceId)
            .header("X-OpenShrimp-Timestamp", timestamp)
            .header("X-OpenShrimp-Nonce", nonce)
            .header("X-OpenShrimp-Signature", signature)
    }

    private fun privateKey(): PrivateKey {
        ensureKeyPair()
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        return keyStore.getKey(KEY_ALIAS, null) as PrivateKey
    }
}

internal fun ByteArray.base64Url(): String =
    Base64.getUrlEncoder().withoutPadding().encodeToString(this)
