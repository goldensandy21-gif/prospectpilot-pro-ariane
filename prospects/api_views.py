"""ETAPE 20 — endpoint serveur-à-serveur : réception des événements PredictNeed IA.

POST /api/predictneed/events/
En-tête : X-PredictNeed-Signature: t=<unix>,v1=<hmac_sha256_hex(secret, f"{t}.{body}")>
Corps JSON : voir services.predictneed_webhook.process_predictneed_event.
"""
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services.hmac_api import parse_json_body, verify_signature
from .services.predictneed_webhook import process_predictneed_event

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def predictneed_events_webhook(request):
    raw_body = request.body.decode("utf-8", "replace")
    signature_header = request.headers.get("X-PredictNeed-Signature", "")

    ok, error = verify_signature(settings.PREDICTNEED_SHARED_SECRET, signature_header, raw_body)
    if not ok:
        logger.warning("Webhook PredictNeed rejeté : %s", error)
        return JsonResponse({"error": error}, status=401)

    payload, parse_error = parse_json_body(raw_body)
    if parse_error:
        return JsonResponse({"error": parse_error}, status=400)

    status_code, response = process_predictneed_event(payload)
    if status_code >= 400:
        logger.warning("Webhook PredictNeed invalide : %s", response)
    return JsonResponse(response, status=status_code)
