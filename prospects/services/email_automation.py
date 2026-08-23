"""Automatisation email commerciale planifiée — J0 / J4 / J8 / J14.

Étend campaign_sequencing.py (moteur d'exécution existant, verrouillé,
idempotent) — n'invente pas un second moteur de séquence. Ce module ajoute
uniquement ce qui manquait :

- has_prior_commercial_first_contact() : verrou anti-doublon global, basé
  sur l'historique réel des EmailSend, jamais sur le statut d'une campagne.
- Le calcul déterministe des dates J0/J4/J8/J14 avec report week-end.
- Le contenu figé (PlannedEmailContent) : une fois validé par un humain,
  c'est exactement ce contenu qui part en SMTP, jamais un nouveau rendu.
- Le scheduler planifié (fenêtre/jours/limites globales Europe/Paris),
  qui délègue l'exécution réelle à campaign_sequencing.advance_campaign_prospect
  — ce module ne réenvoie jamais un email lui-même.

Les campagnes historiques/manuelles (Campaign.planning_managed=False, valeur
par défaut) ne sont JAMAIS concernées par ce module : aucun comportement
existant n'est modifié pour elles.
"""
import hashlib
import secrets
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

from ..models import (
    Campaign,
    CampaignProspect,
    EmailAutomationSettings,
    EmailSend,
    PlannedEmailContent,
)
from .campaign_sequencing import advance_campaign_prospect
from .predictneed_email import render_predictneed_email


# ---------------------------------------------------------------------------
# D — anti-double-premier-contact global
# ---------------------------------------------------------------------------

def has_prior_commercial_first_contact(prospect):
    """True si ce prospect (ou son adresse publique actuelle) a déjà reçu un
    premier email commercial (step.order == 1) réellement envoyé, dans
    N'IMPORTE QUELLE campagne. Basé sur l'historique réel EmailSend — jamais
    seulement le statut de la campagne courante. Un envoi is_test=True ne
    compte jamais comme premier contact."""
    qs = EmailSend.objects.filter(is_test=False, status="sent", email_step__order=1)
    email = (prospect.public_email or "").strip()
    if email:
        return qs.filter(Q(prospect=prospect) | Q(to_email__iexact=email)).exists()
    return qs.filter(prospect=prospect).exists()


def filter_out_already_contacted(prospects):
    """Utilisé à la sélection/préparation (point D.1) : ne retourne que les
    prospects sans premier contact commercial antérieur."""
    return [p for p in prospects if not has_prior_commercial_first_contact(p)]


def assert_not_already_contacted(prospect):
    """Utilisé à la création/inscription dans une nouvelle séquence
    (point D.2). Lève ValueError plutôt que de laisser créer le
    CampaignProspect — jamais un échec silencieux."""
    if has_prior_commercial_first_contact(prospect):
        raise ValueError(
            f"{prospect.name} a déjà reçu un premier contact commercial — "
            "ne peut pas être inscrit comme nouveau prospect d'une autre séquence."
        )


# ---------------------------------------------------------------------------
# B — dates déterministes J0/J4/J8/J14 avec report week-end
# ---------------------------------------------------------------------------

def cumulative_delay_days(sequence, up_to_step):
    """Somme des delay_days (délai depuis l'étape précédente) de toutes les
    étapes actives jusqu'à `up_to_step` inclus — donne 0/4/8/14 pour des
    delay_days stockés 0/4/4/6."""
    total = 0
    for step in sequence.steps.filter(active=True).order_by("order"):
        total += step.delay_days
        if step.pk == up_to_step.pk:
            return total
    return total


def next_business_day(d):
    """Ne déplace QUE si le jour tombe samedi/dimanche — jamais d'envoi le
    week-end (section B)."""
    while d.weekday() >= 5:  # 5=samedi, 6=dimanche
        d += timedelta(days=1)
    return d


def compute_scheduled_date(first_contact_date, sequence, step):
    raw_date = first_contact_date + timedelta(days=cumulative_delay_days(sequence, step))
    return next_business_day(raw_date)


# ---------------------------------------------------------------------------
# E — contenu figé (validation humaine obligatoire)
# ---------------------------------------------------------------------------

def content_hash_for(subject, html_body, text_body):
    payload = f"{subject}\x00{html_body}\x00{text_body}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def render_live_content(campaign_prospect, email_step, open_tracking_token=None):
    variant = email_step.variants.filter(active=True).first()
    subject, html, text = render_predictneed_email(
        campaign_prospect, email_step, variant, request=None,
        open_tracking_token=open_tracking_token,
    )
    return subject, html, text


def freeze_planned_content(campaign_prospect, email_step, user, scheduled_date):
    """Rend le contenu réel (comme un envoi le ferait), le fige, et
    enregistre qui/quand a approuvé (section E). Idempotent : réappeler ne
    crée pas de doublon (une seule ligne par (campaign_prospect, email_step))
    mais ré-approuve avec un contenu frais si rappelé volontairement."""
    open_token = secrets.token_urlsafe(32)
    subject, html, text = render_live_content(campaign_prospect, email_step, open_tracking_token=open_token)
    content_hash = content_hash_for(subject, html, text)

    planned, _created = PlannedEmailContent.objects.update_or_create(
        campaign_prospect=campaign_prospect, email_step=email_step,
        defaults={
            "subject": subject, "html_body": html, "text_body": text,
            "content_hash": content_hash, "open_tracking_token": open_token,
            "scheduled_date": scheduled_date,
            "approved_by": user, "approved_at": timezone.now(),
            "status": "validated",
        },
    )
    return planned


def is_content_stale(planned):
    """Compare le hash figé à un nouveau rendu live : si le prospect/produit
    a changé depuis la validation, le contenu est jugé obsolète — jamais
    renvoyé silencieusement (section E, critique)."""
    subject, html, text = render_live_content(
        planned.campaign_prospect, planned.email_step,
        open_tracking_token=planned.open_tracking_token or None,
    )
    return content_hash_for(subject, html, text) != planned.content_hash


def mark_stale_if_changed(planned):
    if planned.status == "validated" and is_content_stale(planned):
        planned.status = "stale"
        planned.save(update_fields=["status", "updated_at"])
    return planned


# ---------------------------------------------------------------------------
# F — scheduler planifié (fenêtre / jours / limites globales)
# ---------------------------------------------------------------------------

def is_within_send_window(now, settings_row):
    tz = ZoneInfo(settings_row.timezone_name)
    local_now = now.astimezone(tz)
    if local_now.weekday() >= 5:  # jamais le week-end
        return False
    return settings_row.send_window_start <= local_now.time() <= settings_row.send_window_end


def _today_counts(settings_row, now):
    tz = ZoneInfo(settings_row.timezone_name)
    today = now.astimezone(tz).date()
    sent_today = EmailSend.objects.filter(
        campaign_prospect__campaign__planning_managed=True,
        is_test=False, status="sent", sent_at__date=today,
    )
    total = sent_today.count()
    new_contacts = sent_today.filter(email_step__order=1).count()
    return total, new_contacts


def run_planning_scheduler(now=None):
    """Point d'entrée du scheduler (appelé par la tâche Celery). Idempotent :
    chaque appel ne fait avancer que ce qui est réellement dû, jamais deux
    fois la même étape (garanti par campaign_sequencing.advance_campaign_prospect,
    verrouillé par select_for_update). Reprend sans doublon après un
    redémarrage — aucun état en mémoire, tout est lu depuis la base à chaque
    appel."""
    now = now or timezone.now()
    settings_row = EmailAutomationSettings.current()

    if not settings_row.active:
        return {"action": "inactive", "processed": []}
    if not is_within_send_window(now, settings_row):
        return {"action": "outside_window", "processed": []}

    total_sent_today, new_contacts_sent_today = _today_counts(settings_row, now)

    results = []
    campaigns = Campaign.objects.filter(planning_managed=True, status__in=["ready", "active"])
    candidates = CampaignProspect.objects.filter(
        campaign__in=campaigns,
    ).exclude(
        status__in=["do_not_contact", "lost", "paying", "churned", "excluded"],
    ).select_related("campaign", "prospect", "current_step").order_by("-acquisition_score_snapshot")

    for campaign_prospect in candidates:
        if total_sent_today >= settings_row.daily_total_limit:
            results.append({"campaign_prospect_id": campaign_prospect.pk, "action": "deferred_daily_total_limit"})
            continue

        is_first_contact = campaign_prospect.current_step_id is None
        if is_first_contact and new_contacts_sent_today >= settings_row.new_contacts_per_day:
            results.append({"campaign_prospect_id": campaign_prospect.pk, "action": "deferred_new_contacts_limit"})
            continue

        result = advance_campaign_prospect(campaign_prospect.pk, now=now)
        result["campaign_prospect_id"] = campaign_prospect.pk
        results.append(result)

        if result.get("action") == "email":
            total_sent_today += 1
            if is_first_contact:
                new_contacts_sent_today += 1

    return {"action": "ran", "processed": results}
