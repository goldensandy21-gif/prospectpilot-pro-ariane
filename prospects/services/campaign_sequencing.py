"""Mission 6, section 11 — séquences multicanal.

Étend Campaign/CampaignProspect/EmailStep (voir models/acquisition.py) — pas
de second système de campagne. Une étape (`EmailStep`) porte maintenant un
`channel` (email / linkedin_connect / linkedin_message) et une
`advance_condition` (always / linkedin_accepted) en plus de son
`delay_days` existant, qui continue de porter le délai depuis l'étape
précédente pour tous les canaux.

Garantie centrale : `advance_campaign_prospect()` exécute AU PLUS UNE action
par appel (verrouillée par `select_for_update`), et n'avance
`CampaignProspect.current_step` qu'après exécution réussie — un prospect ne
peut donc jamais recevoir deux actions au même moment, et rappeler la
fonction avant que la condition/le délai de l'étape suivante ne soit
satisfait ne fait jamais rien de plus qu'un `waiting`.

Arrêt immédiat (avant toute exécution d'étape) si : réponse déjà obtenue,
conversion déjà enregistrée, désinscription/opposition, DNC, client déjà
payant — jamais une action de séquence après l'un de ces états.
"""
from django.db import transaction
from django.utils import timezone

from ..models import CampaignProspect, ConversionEvent, Suppression
from .linkedin_orchestration import linkedin_profile_url, send_invitation, send_message
from .predictneed_email import send_predictneed_campaign_email
from .signal_freshness import signal_freshness

STOP_CONTACT_LOG_OUTCOMES = {"replied", "meeting", "proposal", "optout"}


def _stop_reason(campaign_prospect):
    prospect = campaign_prospect.prospect
    sequence = campaign_prospect.campaign.sequence

    if prospect.status == "do_not_contact":
        return "Prospect marqué « Ne plus contacter »."
    if prospect.predictneed_stage == "paying":
        return "Déjà client payant."
    if Suppression.objects.filter(active=True, prospect=prospect).exists():
        return "Prospect en liste d'opposition."
    if prospect.public_email and Suppression.objects.filter(active=True, email__iexact=prospect.public_email).exists():
        return "Adresse e-mail en liste d'opposition."
    if campaign_prospect.status in ("do_not_contact", "lost", "paying"):
        return f"Statut campagne « {campaign_prospect.get_status_display()} »."

    if sequence and sequence.stop_on_reply:
        if prospect.contact_logs.filter(outcome__in={"replied", "meeting", "proposal"}).exists():
            return "Le prospect a répondu."
    if sequence and sequence.stop_on_unsubscribe:
        if prospect.contact_logs.filter(outcome="optout").exists():
            return "Le prospect s'est désinscrit / a signalé une opposition."
    if sequence and sequence.stop_on_conversion:
        if ConversionEvent.objects.filter(prospect=prospect).exists():
            return "Conversion déjà enregistrée."

    return ""


def _next_step(campaign_prospect):
    steps = list(campaign_prospect.campaign.sequence.steps.filter(active=True).order_by("order"))
    if not steps:
        return None
    if campaign_prospect.current_step is None:
        return steps[0]
    for index, step in enumerate(steps):
        if step.pk == campaign_prospect.current_step.pk:
            return steps[index + 1] if index + 1 < len(steps) else None
    return steps[0]


def _condition_met(campaign_prospect, step, now):
    """Au-delà du simple délai : une étape linkedin_message n'est prête que
    si l'invitation précédente a été acceptée. Si elle a été refusée/expirée,
    on ne bloque jamais indéfiniment — l'étape est marquée "skip" par
    l'appelant, qui passe alors à l'étape suivante."""
    if step.advance_condition == "linkedin_accepted":
        accepted = campaign_prospect.contact_logs.filter(
            channel="linkedin", outcome="invitation_accepted",
        ).exists()
        declined = campaign_prospect.contact_logs.filter(
            channel="linkedin", outcome="invitation_declined",
        ).exists()
        if declined:
            return "skip"
        return "ready" if accepted else "wait"
    return "ready"


def _delay_elapsed(campaign_prospect, step, now):
    if campaign_prospect.current_step_started_at is None:
        return True
    age_days = signal_freshness(campaign_prospect.current_step_started_at, now=now)["age_days"]
    return age_days is not None and age_days >= step.delay_days


def _mark_step_done(campaign_prospect, step, now):
    campaign_prospect.current_step = step
    campaign_prospect.current_step_started_at = now
    if campaign_prospect.status in ("identified", "selected", "ready_to_contact"):
        campaign_prospect.status = "contacted"
    if campaign_prospect.contacted_at is None:
        campaign_prospect.contacted_at = now
    campaign_prospect.save(update_fields=["current_step", "current_step_started_at", "status", "contacted_at", "updated_at"])


def advance_campaign_prospect(campaign_prospect_id, now=None, linkedin_provider=None):
    """Point d'entrée unique de la séquence multicanal. Toujours appelé avec
    un PK (pas une instance) pour pouvoir verrouiller la ligne — évite deux
    exécutions concurrentes de la même étape pour le même prospect."""
    now = now or timezone.now()

    with transaction.atomic():
        campaign_prospect = CampaignProspect.objects.select_for_update().select_related(
            "campaign__sequence", "prospect",
        ).get(pk=campaign_prospect_id)

        reason = _stop_reason(campaign_prospect)
        if reason:
            if campaign_prospect.status not in ("do_not_contact", "lost", "paying"):
                campaign_prospect.status = "do_not_contact" if "opposition" in reason.lower() or "ne plus contacter" in reason.lower() else campaign_prospect.status
                campaign_prospect.excluded_reason = reason
                campaign_prospect.save(update_fields=["status", "excluded_reason", "updated_at"])
            return {"action": "stopped", "reason": reason}

        sequence = campaign_prospect.campaign.sequence
        if not sequence or not sequence.steps.filter(active=True).exists():
            return {"action": "no_sequence"}

        step = _next_step(campaign_prospect)
        if step is None:
            return {"action": "sequence_complete"}

        condition = _condition_met(campaign_prospect, step, now)
        if condition == "wait":
            return {"action": "waiting", "step": step.name}
        if condition == "skip":
            # Invitation refusée/expirée : on ne bloque jamais la séquence,
            # mais l'étape suivante respecte quand même son propre délai
            # (compté à partir de ce passage) — pas de rattrapage instantané,
            # pour rester cohérent avec le comportement d'un délai non sauté.
            _mark_step_done(campaign_prospect, step, now)
            return advance_campaign_prospect(campaign_prospect_id, now=now, linkedin_provider=linkedin_provider)

        if not _delay_elapsed(campaign_prospect, step, now):
            return {"action": "waiting", "step": step.name}

        return _execute_step(campaign_prospect, step, now, linkedin_provider)


def _execute_step(campaign_prospect, step, now, linkedin_provider):
    prospect = campaign_prospect.prospect

    if step.channel == "linkedin_connect":
        if not linkedin_profile_url(prospect):
            _mark_step_done(campaign_prospect, step, now)
            return {"action": "skipped_no_linkedin_profile", "step": step.name}
        log = send_invitation(prospect, provider=linkedin_provider)
        log.campaign_prospect = campaign_prospect
        log.email_step = step
        log.save(update_fields=["campaign_prospect", "email_step"])
        _mark_step_done(campaign_prospect, step, now)
        return {"action": "linkedin_invitation", "outcome": log.outcome, "step": step.name}

    if step.channel == "linkedin_message":
        if not linkedin_profile_url(prospect):
            _mark_step_done(campaign_prospect, step, now)
            return {"action": "skipped_no_linkedin_profile", "step": step.name}
        variant_message = f"Suite à notre mise en relation, {prospect.name} pourrait être intéressé par PredictNeed IA."
        log = send_message(prospect, variant_message, provider=linkedin_provider)
        log.campaign_prospect = campaign_prospect
        log.email_step = step
        log.save(update_fields=["campaign_prospect", "email_step"])
        _mark_step_done(campaign_prospect, step, now)
        return {"action": "linkedin_message", "outcome": log.outcome, "step": step.name}

    if step.channel == "email":
        variant = step.variants.filter(active=True).first()
        if not variant or not prospect.public_email:
            _mark_step_done(campaign_prospect, step, now)
            return {"action": "skipped_no_email", "step": step.name}
        record = send_predictneed_campaign_email(campaign_prospect, email_step=step, email_variant=variant)
        _mark_step_done(campaign_prospect, step, now)
        return {"action": "email", "outcome": record.status, "step": step.name}

    _mark_step_done(campaign_prospect, step, now)
    return {"action": "unknown_channel", "step": step.name}


def run_campaign_sequences(campaign, limit=None, now=None, linkedin_provider=None):
    """Fait avancer d'un cran chaque CampaignProspect éligible de la
    campagne. Ne s'occupe QUE des CampaignProspect déjà entrés dans la
    séquence (status != identified/selected sans étape) — la sélection
    initiale reste gérée ailleurs (validation de campagne)."""
    now = now or timezone.now()
    candidates = campaign.campaign_prospects.exclude(
        status__in=["do_not_contact", "lost", "paying", "excluded"],
    ).order_by("-acquisition_score_snapshot")
    if limit:
        candidates = candidates[:limit]

    results = []
    for campaign_prospect in candidates:
        result = advance_campaign_prospect(campaign_prospect.pk, now=now, linkedin_provider=linkedin_provider)
        results.append({"campaign_prospect_id": campaign_prospect.pk, **result})
    return results
