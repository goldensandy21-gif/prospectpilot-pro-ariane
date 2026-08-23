"""Automatisation email commerciale planifiée — J0 / J4 / J8 / J14.

Étend campaign_sequencing.py (moteur d'exécution existant, verrouillé,
idempotent) — n'invente pas un second moteur de séquence. Ce module ajoute
uniquement ce qui manquait :

- has_prior_commercial_first_contact() : verrou anti-doublon global, basé
  sur l'historique réel des EmailSend, jamais sur le statut d'une campagne.
- Le calcul déterministe des dates J0/J4/J8/J14 avec report week-end, et la
  répartition réelle lundi->vendredi (build_week_plan).
- Le contenu figé (PlannedEmailContent) : une seule version candidate,
  générée UNE fois par prepare_planned_content() — le test et l'envoi
  commercial réutilisent ensuite ce contenu verbatim, jamais un nouveau
  rendu (correctif audit, section 3).
- Le scheduler planifié (fenêtre/jours/limites globales Europe/Paris),
  qui délègue l'exécution réelle à campaign_sequencing.advance_campaign_prospect
  — ce module ne réenvoie jamais un email lui-même.
- Le retry/backoff SMTP (section 6) : jamais une rafale de tentatives.
- L'adoption sûre d'une campagne existante dans le Planning (section 1).

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
    EmailStep,
    EmailVariant,
    PlannedEmailContent,
)
from .campaign_sequencing import _next_step as _sequence_next_step
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


def contacted_prospect_ids():
    """Variante efficace (une seule requête) de has_prior_commercial_first_contact,
    pour filtrer une liste de candidats côté base de données (voir
    acquisition_views.py::campaign_create). Ne couvre que le rattachement
    direct par Prospect.pk — le contrôle définitif avant toute écriture
    reste has_prior_commercial_first_contact (prospect ET adresse)."""
    return set(
        EmailSend.objects.filter(is_test=False, status="sent", email_step__order=1)
        .exclude(prospect__isnull=True)
        .values_list("prospect_id", flat=True)
    )


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


def business_day_generator(start_date):
    """Générateur infini de jours ouvrés à partir de `start_date` (inclus si
    c'est déjà un jour ouvré)."""
    d = start_date
    while True:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def local_today(now, settings_row=None):
    settings_row = settings_row or EmailAutomationSettings.current()
    tz = ZoneInfo(settings_row.timezone_name)
    return now.astimezone(tz).date()


# ---------------------------------------------------------------------------
# E — contenu figé (validation humaine obligatoire, UNE seule version)
#
# Correctif audit (section 3) : le workflow précédent régénérait un rendu
# différent à la préparation, au test et à la validation. Désormais :
# prepare_planned_content() est le SEUL endroit qui rend le contenu — le
# test et la validation réutilisent ensuite ce contenu verbatim.
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


def prepare_planned_content(campaign_prospect, email_step, scheduled_date):
    """« Préparer » — génère LA version candidate (un seul rendu), statut
    « à valider ». Le token de pixel est généré ICI et jamais régénéré
    ensuite : le test et l'envoi commercial réel réutilisent ce même
    contenu tel quel. Idempotent : réappeler avant validation régénère
    volontairement (l'utilisateur a demandé un nouveau « Préparer »)."""
    open_token = secrets.token_urlsafe(32)
    subject, html, text = render_live_content(campaign_prospect, email_step, open_tracking_token=open_token)
    content_hash = content_hash_for(subject, html, text)

    planned, _created = PlannedEmailContent.objects.update_or_create(
        campaign_prospect=campaign_prospect, email_step=email_step,
        defaults={
            "subject": subject, "html_body": html, "text_body": text,
            "content_hash": content_hash, "open_tracking_token": open_token,
            "scheduled_date": scheduled_date,
            "approved_by": None, "approved_at": None,
            "status": "to_validate",
        },
    )
    return planned


def is_content_stale(planned):
    """Compare le hash figé à un nouveau rendu live (avec le MÊME token de
    pixel déjà stocké, pour ne comparer que le contenu visible) : si le
    prospect/produit a changé depuis la préparation, le contenu est jugé
    obsolète — jamais renvoyé silencieusement.

    Recharge explicitement `campaign_prospect`/`prospect` depuis la base à
    chaque appel plutôt que d'utiliser `planned.campaign_prospect` tel quel :
    un objet Django met en cache la relation dès son premier accès, ce qui
    masquerait silencieusement une modification survenue entre-temps (tout
    l'intérêt de cette fonction est justement de la détecter)."""
    campaign_prospect = CampaignProspect.objects.select_related("prospect", "campaign").get(
        pk=planned.campaign_prospect_id,
    )
    subject, html, text = render_live_content(
        campaign_prospect, planned.email_step,
        open_tracking_token=planned.open_tracking_token or None,
    )
    return content_hash_for(subject, html, text) != planned.content_hash


def mark_stale_if_changed(planned):
    if planned.status == "validated" and is_content_stale(planned):
        planned.status = "stale"
        planned.save(update_fields=["status", "updated_at"])
    return planned


def validate_planned_content(planned, user):
    """« Valider et programmer » — NE régénère jamais le texte. Vérifie
    seulement que le rendu live correspond toujours au hash figé lors de la
    préparation ; si les données source ont changé entre-temps, marque
    `stale` et refuse la validation (il faut relancer « Préparer »).
    Renvoie True si validé, False si resté/passé `stale`."""
    if is_content_stale(planned):
        planned.status = "stale"
        planned.save(update_fields=["status", "updated_at"])
        return False
    planned.status = "validated"
    planned.approved_by = user
    planned.approved_at = timezone.now()
    planned.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return True


def freeze_planned_content(campaign_prospect, email_step, user, scheduled_date):
    """Combinateur de convenance : prépare PUIS valide immédiatement, en un
    seul rendu (utilisé par les tests et par tout appelant qui n'a pas
    besoin de séparer les deux étapes humaines)."""
    planned = prepare_planned_content(campaign_prospect, email_step, scheduled_date)
    validate_planned_content(planned, user)
    planned.refresh_from_db()
    return planned


# ---------------------------------------------------------------------------
# 1 — adoption sûre d'une campagne existante dans le Planning
# ---------------------------------------------------------------------------

FOUR_STEP_SPECS = [
    (1, 0, "Premier contact"),
    (2, 4, "Rappel court"),
    (3, 4, "Relance valeur"),
    (4, 6, "Dernier message"),
]


def ensure_four_step_sequence(sequence):
    """Complète une séquence pour qu'elle possède exactement les 4 étapes
    email J0(0)/J4(4)/J8(4)/J14(6) — NE TOUCHE JAMAIS une étape déjà
    existante (le J0 personnalisé existant, notamment, n'est ni modifié ni
    remplacé) ; crée uniquement ce qui manque, avec un sujet générique de
    repli (à personnaliser ensuite normalement via le Planning)."""
    existing_orders = set(sequence.steps.values_list("order", flat=True))
    for order, delay, name in FOUR_STEP_SPECS:
        if order in existing_orders:
            continue
        step = EmailStep.objects.create(sequence=sequence, order=order, delay_days=delay, name=name, channel="email")
        EmailVariant.objects.create(
            step=step, name=name, cta_type="simulator",
            subject_template=f"{{{{ company_name }}}} — {name.lower()}",
        )
    return sequence


def adopt_campaign_into_planning(campaign, user=None):
    """Section 1 — fait entrer PROPREMENT une campagne existante (créée
    avant `planning_managed`) dans le Planning e-mail, sans envoi et sans
    considérer son ancienne validation comme une nouvelle approbation.

    Garde-fous :
    - exige qu'aucun premier email commercial n'ait déjà été réellement
      envoyé pour les prospects de cette campagne (sinon ValueError, rien
      n'est modifié) ;
    - invalide l'ancienne autorisation scheduler (status -> draft,
      validated_at/validated_by -> None) : le scheduler ne peut rien
      envoyer tant que la nouvelle validation humaine n'existe pas ;
    - passe planning_managed=True ;
    - conserve prospect, AgentBrief, destinataire, et le J0 personnalisé
      existant tels quels ;
    - complète la séquence à 4 étapes (sans écraser le J0 existant) ;
    - relève total_limit à au moins 4 (sinon J4/J8/J14 resteraient bloqués
      par l'ancienne limite total_limit=1)."""
    members = list(campaign.campaign_prospects.select_related("prospect").all())
    already_contacted = [m.prospect for m in members if has_prior_commercial_first_contact(m.prospect)]
    if already_contacted:
        names = ", ".join(p.name for p in already_contacted[:5])
        raise ValueError(
            f"Impossible d'adopter « {campaign.name} » dans le Planning : "
            f"{len(already_contacted)} prospect(s) ont déjà reçu un premier contact commercial réel ({names}). "
            "Aucune modification effectuée."
        )

    if not campaign.sequence:
        raise ValueError(f"Campagne « {campaign.name} » sans séquence — adoption impossible.")

    ensure_four_step_sequence(campaign.sequence)

    campaign.planning_managed = True
    campaign.status = "draft"
    campaign.validated_at = None
    campaign.validated_by = None
    if campaign.total_limit < 4:
        campaign.total_limit = 4
    campaign.save(update_fields=["planning_managed", "status", "validated_at", "validated_by", "total_limit"])
    return campaign


# ---------------------------------------------------------------------------
# 4 — planification réelle lundi -> vendredi
# ---------------------------------------------------------------------------

def _due_followups(campaigns, start_date):
    """Relances déjà dues ou à venir (CampaignProspect ayant déjà avancé
    d'au moins une étape) — jamais planifiées dans le passé."""
    items = []
    for cp in CampaignProspect.objects.filter(campaign__in=campaigns, current_step__isnull=False).exclude(
        status__in=["do_not_contact", "lost", "paying", "excluded", "churned"],
    ).select_related("campaign__sequence", "current_step", "prospect"):
        sequence = cp.campaign.sequence
        if not sequence:
            continue
        next_step = _sequence_next_step(cp)
        if next_step is None:
            continue
        first_contact_date = (cp.contacted_at or timezone.now()).date()
        natural_date = compute_scheduled_date(first_contact_date, sequence, next_step)
        natural_date = max(natural_date, start_date)
        items.append((natural_date, cp, next_step))
    items.sort(key=lambda t: t[0])
    return items


def _pending_new_contacts(campaigns):
    """Prospects planning_managed pas encore entrés dans la séquence, sans
    premier contact antérieur (défense en profondeur, en plus du verrou à
    l'inscription)."""
    items = []
    for cp in CampaignProspect.objects.filter(campaign__in=campaigns, current_step__isnull=True).exclude(
        status__in=["do_not_contact", "lost", "paying", "excluded", "churned"],
    ).select_related("campaign__sequence", "prospect").order_by("-acquisition_score_snapshot", "pk"):
        sequence = cp.campaign.sequence
        if not sequence:
            continue
        step1 = sequence.steps.filter(active=True, order=1).first()
        if not step1:
            continue
        if has_prior_commercial_first_contact(cp.prospect):
            continue
        items.append((cp, step1))
    return items


def build_week_plan(now=None):
    """Section 4 — répartition déterministe lundi->vendredi (puis semaines
    suivantes si nécessaire) :
    1) les relances déjà dues sont placées EN PRIORITÉ, à leur date
       naturelle si la capacité du jour le permet, sinon reportées au
       prochain jour ouvré (jamais perdues) ;
    2) les nouveaux premiers contacts remplissent ensuite la capacité
       restante de chaque jour, jusqu'à `new_contacts_per_day` ET jusqu'à
       la capacité totale restante du jour (`daily_total_limit`, qui inclut
       les relances).
    Renvoie une liste de (date, campaign_prospect, email_step) — n'écrit
    rien elle-même (voir prepare_planned_content, appelé par l'appelant)."""
    now = now or timezone.now()
    settings_row = EmailAutomationSettings.current()
    today = local_today(now, settings_row)
    start = today if today.weekday() < 5 else next_business_day(today)

    campaigns = Campaign.objects.filter(planning_managed=True).select_related("sequence")

    remaining_followups = _due_followups(campaigns, start)
    remaining_new = _pending_new_contacts(campaigns)

    plan = []
    day_gen = business_day_generator(start)
    day_total = {}
    day_new = {}
    day_index = 0
    current_day = None

    while remaining_followups or remaining_new:
        if day_index > 90:  # garde-fou anti boucle infinie (~4 mois ouvrés)
            break
        current_day = next(day_gen)
        day_total.setdefault(current_day, 0)
        day_new.setdefault(current_day, 0)

        i = 0
        while i < len(remaining_followups):
            natural_date, cp, step = remaining_followups[i]
            if natural_date <= current_day and day_total[current_day] < settings_row.daily_total_limit:
                plan.append((current_day, cp, step))
                day_total[current_day] += 1
                remaining_followups.pop(i)
            else:
                i += 1

        while (
            remaining_new
            and day_new[current_day] < settings_row.new_contacts_per_day
            and day_total[current_day] < settings_row.daily_total_limit
        ):
            cp, step = remaining_new.pop(0)
            plan.append((current_day, cp, step))
            day_total[current_day] += 1
            day_new[current_day] += 1

        day_index += 1

    return plan


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
    today = local_today(now, settings_row)
    sent_today = EmailSend.objects.filter(
        campaign_prospect__campaign__planning_managed=True,
        is_test=False, status="sent", sent_at__date=today,
    )
    total = sent_today.count()
    new_contacts = sent_today.filter(email_step__order=1).count()
    return total, new_contacts


def run_planning_scheduler(now=None):
    """Point d'entrée du scheduler (appelé par la tâche Celery). Idempotent :
    chaque appel ne fait avancer que ce qui est réellement dû ET dont la
    `scheduled_date` (PlannedEmailContent) n'est pas dans le futur (vérifié
    par campaign_sequencing.py), jamais deux fois la même étape (garanti
    par advance_campaign_prospect, verrouillé par select_for_update).
    Reprend sans doublon après un redémarrage — aucun état en mémoire, tout
    est lu depuis la base à chaque appel."""
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


# ---------------------------------------------------------------------------
# 6 — retry / backoff SMTP
# ---------------------------------------------------------------------------

SMTP_MAX_ATTEMPTS = 5
# Backoff raisonnable, plafonné : 5 min, 15 min, 45 min, 2h, 6h.
SMTP_BACKOFF_MINUTES = [5, 15, 45, 120, 360]


def _attempt_history(campaign_prospect, email_step, exclude_pk=None):
    qs = EmailSend.objects.filter(
        campaign_prospect=campaign_prospect, email_step=email_step, is_test=False,
    ).exclude(status__in=["draft", "blocked"])
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.order_by("-created_at")


def finalize_failed_send(record, campaign_prospect, email_step, now=None):
    """Appelé juste après un échec SMTP (section 6) : calcule `next_retry_at`
    selon le nombre RÉEL de tentatives déjà faites pour cette étape précise
    (jamais une rafale de tentatives toutes les 5 minutes), ou bascule en
    échec permanent au-delà de SMTP_MAX_ATTEMPTS. L'historique de chaque
    tentative reste conservé (une ligne EmailSend par tentative, jamais
    écrasée). Accepte `now` explicitement pour rester cohérent avec
    smtp_retry_allowed(..., now=now) — le scheduler/les tests peuvent
    simuler un moment précis plutôt que l'horloge murale réelle."""
    now = now or timezone.now()
    if not email_step:
        record.next_retry_at = None
        return record
    prior_attempts = _attempt_history(campaign_prospect, email_step, exclude_pk=record.pk).count()
    attempt_number = prior_attempts + 1
    if attempt_number >= SMTP_MAX_ATTEMPTS:
        record.status = "permanently_failed"
        record.next_retry_at = None
    else:
        backoff_minutes = SMTP_BACKOFF_MINUTES[min(attempt_number - 1, len(SMTP_BACKOFF_MINUTES) - 1)]
        record.next_retry_at = now + timedelta(minutes=backoff_minutes)
    return record


def smtp_retry_allowed(campaign_prospect, email_step, now=None):
    """Vérifié par campaign_sequencing.py AVANT de tenter un envoi planifié.
    Renvoie (autorisé: bool, motif: str). Accepte `now` explicitement (comme
    advance_campaign_prospect/run_planning_scheduler) plutôt que de lire
    l'horloge murale directement — sans cela, un scheduler délibérément
    exécuté avec un `now` simulé (reprise après incident, tests) comparerait
    `next_retry_at` à la vraie heure courante au lieu du moment simulé."""
    now = now or timezone.now()
    last = _attempt_history(campaign_prospect, email_step).first()
    if not last or last.status == "sent":
        return True, ""
    if last.status == "permanently_failed":
        return False, "permanent_failure"
    if last.status == "failed" and last.next_retry_at and now < last.next_retry_at:
        return False, "retry_backoff"
    return True, ""
