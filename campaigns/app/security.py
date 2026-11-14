"""Signed tracking tokens and API keys."""
import base64
import hashlib
import hmac
import secrets
import uuid


def sign(secret, *parts, length=16):
    """Short URL-safe signature over the given parts."""
    payload = ':'.join(str(p) for p in parts).encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip('=')[:length]


def verify(secret, signature, *parts):
    return hmac.compare_digest(sign(secret, *parts), signature or '')


def new_id():
    return uuid.uuid4().hex


def hash_key(plain):
    return hashlib.sha256(plain.encode()).hexdigest()


def new_api_key():
    """Returns (plain key shown once, display prefix, stored hash)."""
    plain = 'em_' + secrets.token_urlsafe(32)
    return plain, plain[:10], hash_key(plain)


def new_secret(prefix='whsec_'):
    return prefix + secrets.token_urlsafe(24)


def webhook_signature(secret, timestamp, body):
    payload = f'{timestamp}.'.encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
