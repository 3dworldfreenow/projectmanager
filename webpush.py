"""
Minimal Web Push (RFC 8291 aes128gcm + RFC 8292 VAPID) on top of `cryptography`.
No pywebpush dependency. Used to notify phones even when the browser is closed.
"""
import os, json, time, base64, struct, threading, urllib.request, urllib.error
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def b64d(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------------ VAPID --
def generate_vapid():
    """Return (private_pem, public_key_b64url) for a new P-256 key."""
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return pem, b64u(pub)


def vapid_headers(private_pem, endpoint, subject):
    k = serialization.load_pem_private_key(private_pem.encode(), None)
    from urllib.parse import urlparse
    u = urlparse(endpoint); aud = f"{u.scheme}://{u.netloc}"
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": subject}).encode())
    signing = f"{header}.{claims}".encode()
    r, s = decode_dss_signature(k.sign(signing, ec.ECDSA(hashes.SHA256())))
    jwt = f"{header}.{claims}." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    pub = b64u(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    return {"Authorization": f"vapid t={jwt}, k={pub}"}


# ---------------------------------------------------------- encryption ----
def _hkdf(salt, ikm, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh_b64: str, auth_b64: str, salt=None, server_key=None) -> bytes:
    """RFC 8291 aes128gcm content encoding. Returns the body to POST."""
    ua_pub = b64d(p256dh_b64); auth = b64d(auth_b64)
    salt = salt or os.urandom(16)
    sk = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = sk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    shared = sk.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = payload + b"\x02"                      # delimiter: last record
    body = AESGCM(cek).encrypt(nonce, record, None)
    header = salt + struct.pack("!I", 4096) + bytes([len(as_pub)]) + as_pub
    return header + body


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """Inverse of encrypt() — used only by the self-test."""
    salt = body[:16]; rs = struct.unpack("!I", body[16:20])[0]; idlen = body[20]; as_pub = body[21:21 + idlen]
    ct = body[21 + idlen:]
    ua_pub = ua_private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    return AESGCM(cek).decrypt(nonce, ct, None).rstrip(b"\x00")[:-1]


# --------------------------------------------------------------- sending --
def send(subscription: dict, data: dict, private_pem: str, subject: str, ttl=60):
    """POST one push message. Returns (status, text). 404/410 mean the subscription is dead."""
    body = encrypt(json.dumps(data).encode(), subscription["keys"]["p256dh"], subscription["keys"]["auth"])
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl),
               "Urgency": "high" if data.get("urgent") else "normal"}
    headers.update(vapid_headers(private_pem, subscription["endpoint"], subject))
    req = urllib.request.Request(subscription["endpoint"], data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:200]
    except Exception as e:
        return 0, str(e)


def send_async(subscriptions, data, private_pem, subject, on_dead=None):
    """Fire-and-forget to many subscriptions; on_dead(endpoint) called for 404/410."""
    def run():
        for sub in subscriptions:
            st, _ = send(sub, data, private_pem, subject)
            if st in (404, 410) and on_dead:
                try: on_dead(sub["endpoint"])
                except Exception: pass
    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    # RFC 8291 §5 test vector
    ua_priv = ec.derive_private_key(int.from_bytes(b64d("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"), "big"), ec.SECP256R1())
    as_priv = ec.derive_private_key(int.from_bytes(b64d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    auth = "BTBZMqHH6r4Tts7J_aSIgg"; salt = b64d("DGv6ra1nlYgDCS1FRnbzlw")
    ua_pub = b64u(ua_priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    out = encrypt(b"When I grow up, I want to be a watermelon", ua_pub, auth, salt=salt, server_key=as_priv)
    expected = "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    print("RFC 8291 test vector:", "PASS" if b64u(out) == expected else "FAIL")
    print("roundtrip:", decrypt(out, ua_priv, b64d(auth)))
    pem, pub = generate_vapid(); print("vapid header ok:", vapid_headers(pem, "https://fcm.googleapis.com/fcm/send/x", "mailto:a@b.c")["Authorization"][:20])
