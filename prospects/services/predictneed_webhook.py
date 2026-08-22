"""ETAPE 20/21 — traitement des événements reçus depuis PredictNeed IA.

Ne reçoit jamais de données de carte bancaire (Stripe reste géré par PredictNeed) :
uniquement les informations commerciales nécessaires à l'attribution.
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from ..models import CampaignProspect, ConversionEvent, EngagementEvent, EmailSend, RevenueAttribution

VALID_EVENT_TYPES = {choice[0] for choice in EngagementEvent.EVENT_TYPES}
CONVERSION_MAP = {
    "signup_completed": "signup",
    "subscription_activated": "paying",
    "subscription_cancelled": "cancelled",
}
STAGE_MAP = {
    "signup_completed": "signed_up",
    "subscription_activated": "paying",
    # Correctif d'audit (round 3) : un client qui résilie doit sortir de
    # l'état "paying" — sinon _hard_exclusion() (predictneed_scoring.py)
    # continue à exclure un prospect qui n'est plus client, et rien ne
    # distingue jamais un client actif d'un client parti.
    "subscription_cancelled": "churned",
}


def _parse_datetime(value):
    if not value:
        return timezone.now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt_timezone.utc)
    except ValueError:
        return timezone.now()


def _decimal(value):
    try:
        return Decimal(str(value)) if value is not None else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def process_predictneed_event(payload):
    """Retourne (status_code, response_dict)."""
    event_type = payload.get("event_type")
    if event_type not in VALID_EVENT_TYPES:
        return 400, {"error": f"event_type invalide : {event_type!r}"}

    idempotency_key = payload.get("idempotency_key") or ""
    if idempotency_key and EngagementEvent.objects.filter(idempotency_key=idempotency_key).exists():
        return 200, {"status": "duplicate_ignored"}

    ppt_token = payload.get("ppt") or ""
    campaign_prospect = CampaignProspect.objects.filter(tracking_token=ppt_token).select_related(
        "prospect", "campaign"
    ).first() if ppt_token else None
    prospect = campaign_prospect.prospect if campaign_prospect else None
    campaign = campaign_prospect.campaign if campaign_prospect else None

    occurred_at = _parse_datetime(payload.get("occurred_at"))
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {"raw": str(metadata)[:500]}

    engagement = EngagementEvent.objects.create(
        campaign_prospect=campaign_prospect, prospect=prospect, campaign=campaign,
        event_type=event_type, source="predictneed", metadata=metadata,
        idempotency_key=idempotency_key or None, occurred_at=occurred_at,
    )

    response = {"status": "ok", "engagement_event_id": engagement.pk}

    # Correctif d'audit (round 3) : l'ordre transactionnel compte.
    # predictneed_stage doit être définitivement à jour AVANT tout appel à
    # score_prospect() — sinon _hard_exclusion() (predictneed_scoring.py),
    # qui exclut un prospect dont predictneed_stage == "paying", ne voit pas
    # encore le nouvel état lors d'un subscription_activated et calcule un
    # score normal au lieu de l'exclusion attendue. score_prospect() est
    # donc appelé en tout dernier, une fois campaign_prospect/predictneed_stage/
    # ConversionEvent/RevenueAttribution tous à jour.
    if campaign_prospect:
        campaign_prospect.last_engagement_at = timezone.now()
        new_status = STAGE_MAP.get(event_type)
        if new_status:
            campaign_prospect.status = new_status
        elif campaign_prospect.status == "contacted":
            campaign_prospect.status = "engaged"
        if new_status in ("signed_up", "paying"):
            campaign_prospect.converted_at = campaign_prospect.converted_at or timezone.now()
        campaign_prospect.save(update_fields=["last_engagement_at", "status", "converted_at"])

    if prospect and event_type in STAGE_MAP:
        prospect.predictneed_stage = STAGE_MAP[event_type]
        prospect.save(update_fields=["predictneed_stage", "updated_at"])

    conversion_type = CONVERSION_MAP.get(event_type)
    if conversion_type and prospect:
        conversion, created = ConversionEvent.objects.get_or_create(
            idempotency_key=idempotency_key or f"{event_type}:{engagement.pk}",
            defaults={
                "prospect": prospect, "campaign": campaign, "event_type": conversion_type,
                "external_reference": payload.get("external_reference", ""),
                "occurred_at": occurred_at, "raw_payload": {k: v for k, v in payload.items() if k not in ("metadata",)},
            },
        )
        response["conversion_event_id"] = conversion.pk

        if conversion_type == "paying" and created:
            last_send = EmailSend.objects.filter(
                campaign_prospect=campaign_prospect, status="sent"
            ).order_by("-sent_at").first() if campaign_prospect else None
            attribution = RevenueAttribution.objects.create(
                conversion_event=conversion, prospect=prospect, campaign=campaign,
                icp=campaign.icp if campaign else None,
                email_sequence=campaign.sequence if campaign else None,
                email_step=last_send.email_step if last_send else None,
                email_variant=last_send.email_variant if last_send else None,
                subscription_value=_decimal(payload.get("subscription_value")),
                mrr=_decimal(payload.get("mrr")),
                currency=payload.get("currency", "EUR"),
                attributed_at=occurred_at,
            )
            response["revenue_attribution_id"] = attribution.pk
        # Correctif d'audit (round 3) : une résiliation (ConversionEvent
        # "cancelled") ne touche JAMAIS à un RevenueAttribution existant —
        # aucune suppression, aucune réécriture ici. L'historique de revenu
        # réellement généré reste intact quel que soit ce qui se passe après.

    if prospect:
        # Mission 6, section 15 : alerte sur un nouvel engagement PredictNeed
        # réel, jamais un événement synthétique.
        from .alerts import check_engagement_alert
        check_engagement_alert(prospect, engagement)

        # Recalcul temps réel — engagement_score ET la "Priorité" canonique
        # reflètent immédiatement ce nouvel événement, avec predictneed_stage
        # déjà à jour. Lecture seule côté PredictNeed : aucune boucle.
        from .predictneed_scoring import score_prospect
        score_prospect(prospect)

    return 200, response
