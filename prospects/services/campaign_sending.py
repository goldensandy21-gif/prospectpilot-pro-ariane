"""ETAPE 15/16/17/26 — sélection de séquence par défaut + envoi de campagne encadré.

Recherche -> qualification -> campagne brouillon -> aperçu -> validation explicite
-> envoi. Aucune de ces étapes ne déclenche d'envoi automatique.
"""
from urllib.parse import urlparse

from django.utils import timezone

from ..models import Campaign, CampaignProspect, EmailSequence, EmailStep, EmailVariant
from .predictneed_email import send_predictneed_campaign_email
from .suppression import is_suppressed


def get_or_create_default_sequence(product, icp=None):
    sequence, created = EmailSequence.objects.get_or_create(
        product=product, icp=icp, name=f"Séquence par défaut — {icp.name if icp else product.name}",
        defaults={"active": True},
    )
    if created or not sequence.steps.exists():
        step, _ = EmailStep.objects.get_or_create(sequence=sequence, order=1, defaults={"delay_days": 0, "name": "Premier contact"})
        EmailVariant.objects.get_or_create(
            step=step, name="Observation + valeur",
            defaults={"subject_template": "{{ company_name }} — une observation sur votre parcours visiteurs", "cta_type": "simulator"},
        )
        EmailStep.objects.get_or_create(sequence=sequence, order=2, defaults={"delay_days": 4, "name": "Rappel court"})
        EmailStep.objects.get_or_create(sequence=sequence, order=3, defaults={"delay_days": 8, "name": "Dernier message"})
        for step in sequence.steps.all():
            EmailVariant.objects.get_or_create(
                step=step, name="Standard",
                defaults={"subject_template": "{{ company_name }} — comportement des visiteurs", "cta_type": "simulator"},
            )
    return sequence


def _domain(email):
    return (email or "").split("@", 1)[-1].lower()


def send_campaign_batch(campaign, limit=None):
    """Envoie le prochain lot de la campagne dans les limites configurées.
    Retourne un résumé {sent, blocked, skipped, errors}.

    Section B (Round D, verrous production) : refuse IMMÉDIATEMENT une
    campagne planning_managed=True, même appelée directement en Python hors
    de toute vue — le seul moteur d'envoi autorisé pour ces campagnes est
    campaign_sequencing.advance_campaign_prospect (via le scheduler), qui
    exige un PlannedEmailContent validé (testé, non périmé). Ce garde-fou
    reste nécessaire même si campaign.is_sendable devient True après une
    validation Planning (section A) : is_sendable seul ne suffit plus à
    distinguer les deux moteurs d'envoi."""
    if campaign.planning_managed:
        return {"sent": 0, "blocked": 0, "skipped": 0, "errors": [
            "Campagne pilotée par le Planning e-mail — envoi par lot legacy refusé (utiliser Préparer/Tester/Valider).",
        ]}
    if not campaign.is_sendable:
        return {"sent": 0, "blocked": 0, "skipped": 0, "errors": ["Campagne non validée."]}

    sequence = campaign.sequence
    if not sequence or not sequence.steps.exists():
        return {"sent": 0, "blocked": 0, "skipped": 0, "errors": ["Aucune séquence e-mail configurée."]}

    first_step = sequence.steps.order_by("order").first()
    variant = first_step.variants.filter(active=True).first()
    if not variant:
        return {"sent": 0, "blocked": 0, "skipped": 0, "errors": ["Aucun variant e-mail actif."]}

    today = timezone.now().date()
    already_sent_today = campaign.events.filter(event_type="email_sent", occurred_at__date=today).count()
    remaining_today = max(0, campaign.daily_send_limit - already_sent_today)
    total_sent = campaign.events.filter(event_type="email_sent").count()
    remaining_total = max(0, campaign.total_limit - total_sent)
    batch_limit = min(limit or remaining_today, remaining_today, remaining_total)

    contacted_domains_today = set(
        _domain(cp.prospect.public_email)
        for cp in campaign.campaign_prospects.filter(contacted_at__date=today)
    )

    summary = {"sent": 0, "blocked": 0, "skipped": 0, "errors": []}
    if batch_limit <= 0:
        return summary

    candidates = campaign.campaign_prospects.filter(
        status__in=["selected", "ready_to_contact"]
    ).select_related("prospect").order_by("-acquisition_score_snapshot")

    for campaign_prospect in candidates:
        if summary["sent"] >= batch_limit:
            break
        prospect = campaign_prospect.prospect
        email = prospect.public_email or ""
        domain = _domain(email)

        if is_suppressed(email, prospect=prospect):
            campaign_prospect.status = "do_not_contact"
            campaign_prospect.excluded_reason = "Opposition détectée avant envoi."
            campaign_prospect.save(update_fields=["status", "excluded_reason"])
            summary["blocked"] += 1
            continue

        if domain and domain in contacted_domains_today:
            summary["skipped"] += 1
            continue  # ETAPE 17 : éviter plusieurs e-mails au même domaine le même jour

        record = send_predictneed_campaign_email(campaign_prospect, email_step=first_step, email_variant=variant)
        if record.status == "sent":
            summary["sent"] += 1
            contacted_domains_today.add(domain)
        elif record.status in ("blocked", "suppressed"):
            summary["blocked"] += 1
        else:
            summary["errors"].append(f"{prospect.name} : {record.error}")

    return summary
