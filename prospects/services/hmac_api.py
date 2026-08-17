"""ETAPE 20 — API serveur-à-serveur ProspectPilot <-> PredictNeed IA.

Signature symétrique au schéma Stripe (déjà utilisé côté PredictNeed dans
billing.verify_stripe_signature) : `t=<timestamp>,v1=<hmac_sha256(secret, f"{t}.{body}")>`.
Cela facilite une implémentation cohérente des deux côtés.
"""
import hashlib
import hmac
import json
import logging
import time

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE_SECONDS = 300


def sign_payload(secret, timestamp, raw_body):
    message = f"{timestamp}.{raw_body}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def build_signature_header(secret, raw_body, timestamp=None):
    timestamp = timestamp or int(time.time())
    signature = sign_payload(secret, timestamp, raw_body)
    return f"t={timestamp},v1={signature}"


def verify_signature(secret, header_value, raw_body, tolerance=DEFAULT_TOLERANCE_SECONDS):
    """Retourne (ok: bool, error: str)."""
    if not secret:
        return False, "Secret partagé non configuré côté ProspectPilot."
    if not header_value:
        return False, "En-tête de signature manquant."

    parts = dict(
        item.split("=", 1) for item in header_value.split(",") if "=" in item
    )
    timestamp_raw = parts.get("t")
    signature = parts.get("v1")
    if not timestamp_raw or not signature:
        return False, "Format de signature invalide."

    try:
        timestamp = int(timestamp_raw)
    except ValueError:
        return False, "Timestamp invalide."

    if abs(time.time() - timestamp) > tolerance:
        return False, "Timestamp hors tolérance (rejeu possible)."

    expected = sign_payload(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected, signature):
        return False, "Signature invalide."

    return True, ""


def parse_json_body(raw_body):
    try:
        return json.loads(raw_body or "{}"), ""
    except (json.JSONDecodeError, TypeError):
        return None, "Corps JSON invalide."
