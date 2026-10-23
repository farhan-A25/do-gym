"""Razorpay payments for the Do Gym Owner App (automatic unlock).

The owner's payment rule (2026-10-23): NO confirmation buttons — the
owner pays, and the website must unlock BY ITSELF the moment the
payment is confirmed. A static QR image cannot do that (it is just a
picture; nothing tells this server a transfer happened), so the real
confirmation channel is a payment gateway:

    owner picks a plan → this server creates a Razorpay order →
    owner pays in Razorpay Checkout → Razorpay calls our webhook →
    webhook signature verified → payment recorded → gate lifts.

Standard library only (urllib) — no new dependencies. Configuration
comes from the environment and is read on every call:

    RAZORPAY_KEY_ID          Key ID from the Razorpay dashboard
    RAZORPAY_KEY_SECRET      Key secret (server-side only, never sent
                             to the browser)
    RAZORPAY_WEBHOOK_SECRET  Secret set on the webhook in the Razorpay
                             dashboard; used to verify that a webhook
                             call really came from Razorpay.

When the keys are not set, the gateway is simply disabled: order
creation answers 404, the frontend falls back to the static UPI QR
panel (waiting state), and the webhook endpoint answers 404.
"""
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
_HTTP_TIMEOUT = 15  # seconds


class RazorpayError(Exception):
    """Any failure talking to Razorpay; callers turn it into a friendly error."""


def razorpay_key_id() -> str:
    return os.environ.get("RAZORPAY_KEY_ID", "").strip()


def razorpay_key_secret() -> str:
    return os.environ.get("RAZORPAY_KEY_SECRET", "").strip()


def razorpay_webhook_secret() -> str:
    return os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()


def razorpay_enabled() -> bool:
    """Checkout is on only when both API keys are configured."""
    return bool(razorpay_key_id() and razorpay_key_secret())


def razorpay_webhook_enabled() -> bool:
    """Webhooks can only be trusted when a webhook secret is configured."""
    return bool(razorpay_webhook_secret())


def create_order(amount_paise: int, receipt: str, notes: dict) -> dict:
    """Create a Razorpay order. Amount is in paise (₹1 = 100 paise) and
    is ALWAYS computed server-side from billing.py by the caller — the
    browser never names a price. Returns the decoded Razorpay order."""
    payload = {
        "amount": int(amount_paise),
        "currency": "INR",
        "receipt": receipt,
        "notes": notes or {},
    }
    body = json.dumps(payload).encode("utf-8")
    credentials = f"{razorpay_key_id()}:{razorpay_key_secret()}".encode("utf-8")
    import base64

    req = urllib.request.Request(
        RAZORPAY_API_BASE + "/orders",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise RazorpayError(f"Razorpay order creation failed: {exc}") from exc


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """True only if `signature` is Razorpay's HMAC-SHA256 of the exact
    raw request body, keyed with the webhook secret. Constant-time
    comparison; anything malformed is simply False."""
    secret = razorpay_webhook_secret()
    if not secret or not signature or not isinstance(raw_body, (bytes, bytearray)):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), bytes(raw_body), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())
