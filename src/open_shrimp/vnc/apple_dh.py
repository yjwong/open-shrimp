"""Apple Diffie-Hellman authentication for RFB security type 30.

Implements the client side of Apple Screen Sharing's ``mslogonII`` /
``DH+ARD`` security scheme.  Used by the WebSocket proxy in
:mod:`open_shrimp.vnc.api` to authenticate against the macOS guest's
Screen Sharing server on behalf of noVNC clients, so that credentials
never leave the bot process.

Protocol (after the client has selected security type 30 by sending
``0x1e`` in response to the server's security-types list, on RFB 3.8):

  server -> client:  g (uint16), keylen (uint16),
                     prime p (keylen bytes), serverPub (keylen bytes)
  client -> server:  AES-128-ECB(MD5(shared), user||pw) (128 bytes),
                     clientPub (keylen bytes)
  server -> client:  RFB 3.8 SecurityResult (uint32; 0 = OK)

Username and password are each UTF-8, NUL-padded to 64 bytes.

AES-128-ECB is implemented in pure Python so the module can run in
PyApp-bundled environments that don't ship ``cryptography`` /
``_cffi_backend``.  Performance is irrelevant — we encrypt a single
128-byte payload per VNC session.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import struct

logger = logging.getLogger(__name__)


class AppleDhAuthError(Exception):
    """Raised when Apple-DH RFB authentication fails."""


# ---------------------------------------------------------------------------
# Pure-Python AES-128-ECB
# ---------------------------------------------------------------------------

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36)


def _xtime(b: int) -> int:
    return ((b << 1) ^ 0x1b) & 0xff if b & 0x80 else (b << 1) & 0xff


def _expand_key(key: bytes) -> list[bytes]:
    """AES-128 key schedule: 16-byte key → 11 round keys (16 bytes each)."""
    rk = bytearray(key)
    for i in range(4, 44):
        t = bytearray(rk[(i - 1) * 4: i * 4])
        if i % 4 == 0:
            t = bytearray((
                _SBOX[t[1]] ^ _RCON[i // 4 - 1],
                _SBOX[t[2]],
                _SBOX[t[3]],
                _SBOX[t[0]],
            ))
        prev = rk[(i - 4) * 4: (i - 3) * 4]
        rk.extend(a ^ b for a, b in zip(t, prev))
    return [bytes(rk[r * 16: (r + 1) * 16]) for r in range(11)]


def _aes128_encrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    state = bytearray(a ^ b for a, b in zip(block, round_keys[0]))
    for r in range(1, 10):
        # SubBytes + ShiftRows (column-major state).
        state = bytearray((
            _SBOX[state[0]],  _SBOX[state[5]],  _SBOX[state[10]], _SBOX[state[15]],
            _SBOX[state[4]],  _SBOX[state[9]],  _SBOX[state[14]], _SBOX[state[3]],
            _SBOX[state[8]],  _SBOX[state[13]], _SBOX[state[2]],  _SBOX[state[7]],
            _SBOX[state[12]], _SBOX[state[1]],  _SBOX[state[6]],  _SBOX[state[11]],
        ))
        # MixColumns.
        new = bytearray(16)
        for c in range(4):
            s0, s1, s2, s3 = state[c * 4: (c + 1) * 4]
            new[c * 4 + 0] = _xtime(s0) ^ _xtime(s1) ^ s1 ^ s2 ^ s3
            new[c * 4 + 1] = s0 ^ _xtime(s1) ^ _xtime(s2) ^ s2 ^ s3
            new[c * 4 + 2] = s0 ^ s1 ^ _xtime(s2) ^ _xtime(s3) ^ s3
            new[c * 4 + 3] = _xtime(s0) ^ s0 ^ s1 ^ s2 ^ _xtime(s3)
        state = bytearray(a ^ b for a, b in zip(new, round_keys[r]))
    # Final round (no MixColumns).
    state = bytearray((
        _SBOX[state[0]],  _SBOX[state[5]],  _SBOX[state[10]], _SBOX[state[15]],
        _SBOX[state[4]],  _SBOX[state[9]],  _SBOX[state[14]], _SBOX[state[3]],
        _SBOX[state[8]],  _SBOX[state[13]], _SBOX[state[2]],  _SBOX[state[7]],
        _SBOX[state[12]], _SBOX[state[1]],  _SBOX[state[6]],  _SBOX[state[11]],
    ))
    return bytes(a ^ b for a, b in zip(state, round_keys[10]))


def _aes128_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    if len(data) % 16 != 0:
        raise ValueError("AES-ECB plaintext must be a multiple of 16 bytes")
    rk = _expand_key(key)
    return b"".join(
        _aes128_encrypt_block(data[i: i + 16], rk)
        for i in range(0, len(data), 16)
    )


# ---------------------------------------------------------------------------
# Apple DH RFB security type 30
# ---------------------------------------------------------------------------


async def authenticate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    username: str,
    password: str,
) -> None:
    """Complete Apple DH on a stream that has just selected security type 30.

    Caller must already have sent ``b"\\x1e"`` (= 30) to the server in
    response to its security-types list.  This function reads DH
    parameters, sends encrypted credentials, and verifies the
    SecurityResult.  On success it returns; on failure it raises
    :class:`AppleDhAuthError`.
    """
    # DH params: 2 bytes generator, 2 bytes keylen, then prime + serverPub.
    header = await reader.readexactly(4)
    g, keylen = struct.unpack("!HH", header)
    if keylen == 0 or keylen > 1024:
        raise AppleDhAuthError(f"unexpected DH key length {keylen}")

    prime_bytes = await reader.readexactly(keylen)
    server_pub_bytes = await reader.readexactly(keylen)

    p = int.from_bytes(prime_bytes, "big")
    server_pub = int.from_bytes(server_pub_bytes, "big")

    a = secrets.randbits(keylen * 8)
    client_pub = pow(g, a, p)
    shared = pow(server_pub, a, p)

    aes_key = hashlib.md5(shared.to_bytes(keylen, "big")).digest()

    user_block = username.encode("utf-8")[:63].ljust(64, b"\x00")
    pw_block = password.encode("utf-8")[:63].ljust(64, b"\x00")
    plaintext = user_block + pw_block  # 128 bytes = 8 AES blocks

    ciphertext = _aes128_ecb_encrypt(aes_key, plaintext)

    writer.write(ciphertext)
    writer.write(client_pub.to_bytes(keylen, "big"))
    await writer.drain()

    (result,) = struct.unpack("!I", await reader.readexactly(4))
    if result == 0:
        return
    try:
        (reason_len,) = struct.unpack("!I", await reader.readexactly(4))
        reason = (await reader.readexactly(reason_len)).decode(
            "utf-8", errors="replace",
        )
    except (asyncio.IncompleteReadError, UnicodeDecodeError):
        reason = ""
    raise AppleDhAuthError(
        f"VNC authentication rejected: {reason or 'no reason given'}"
    )
