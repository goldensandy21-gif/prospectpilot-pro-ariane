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

Correctif d'audit (post-Mission 6) — garde-fous d'avant Mission 6 restaurés,
qui n'avaient pas été repris par ce module au premier passage :
- `campaign.is_sendable` (validation explicite obligatoire) vérifié EN
  PREMIER, avant toute autre logique : une campagne brouillon/non validée
  ne produit aucune action, sur aucun canal.
- `daily_send_limit`/`total_limit` : comptés tous canaux confondus (e-mail +
  LinkedIn), jamais dépassés.
- Politique domaine/jour existante (`services/campaign_sending.py::_domain`)
  réappliquée à l'étape e-mail : jamais deux e-mails au même domaine le
  même jour pour une campagne.

Correctif d'audit (round 2) — concurrence : `daily_send_limit`/`total_limit`
sont des quotas GLOBAUX à la campagne, pas au CampaignProspect. Verrouiller
seulement la ligne CampaignProspect ne protège pas deux workers qui
avancent CONCURREMMENT deux prospects différents de la MÊME campagne — les
deux pourraient lire le même compteur avant que l'un des deux ne commit.
`Campaign` est donc explicitement verrouillée (`select_for_update()`,
séparément de CampaignProspect) en tout début de transaction : toute
vérification de quota pour une campagne donnée est ainsi sérialisée entre
tous les appelants concurrents, quel que soit le CampaignProspect visé.
"""
from django.db import transaction
from django.utils import timezone

from ..models import Campaign, CampaignProspect, ContactLog, ConversionEvent, EmailSend, Suppression
from .campaign_sending import _domain
from .linkedin_orchestration import linkedin_profile_url, send_invitation, send_message
from .message_guardrails import build_personalization_snippet
from .predictneed_email import send_predictneed_campaign_email
from .signal_freshness import signal_freshness

STOP_CONTACT_LOG_OUTCOMES = {"replied", "meeting", "proposal", "optout"}


def _campaign_action_counts(campaign, today):
    """Nombre d'actions sortantes (e-mail + LinkedIn confondus) déjà
    exécutées pour cette campagne aujourd'hui et au total — sert à faire
    respecter daily_send_limit/total_limit quel que soit le canal."""
    email_today = EmailSend.objects.filter(
        campaign_prospect__campaign=campaign, created_at__date=today,
    ).exclude(status="draft").count()
    linkedin_today = ContactLog.objects.filter(
        campaign_prospect__campaign=campaign, channel="linkedin", contacted_at__date=today,
    ).count()
    email_total = EmailSend.objects.filter(campaign_prospect__campaign=campaign).exclude(status="draft").count()
    linkedin_total = ContactLog.objects.filter(campaign_prospect__campaign=campaign, channel="linkedin").count()
    return email_today + linkedin_today, email_total + linkedin_total


def _domain_already_contacted_today(campaign, domain, today):
    """Même règle que send_campaign_batch (ETAPE 17) : jamais deux e-mails
    au même domaine le même jour pour une campagne."""
    contacted_domains = {
        _domain(cp.prospect.public_email)
        for cp in campaign.campaign_prospects.filter(contacted_at__date=today)
    }
    return domain in contacted_domains


def _stop_reason(campaign_prospect):
    prospect = campaign_prospect.prospect
    sequence = campaign_prospect.campaign.sequence

    if prospect.status == "do_not_contact":
        return "Prospect marqué « Ne plus contacter »."
    if prospect.predictneed_stage == "paying":
        return "Déjà client payant."
    if prospect.predictneed_stage == "churned":
        # Correctif d'audit (round 3) : une séquence active devient obsolète
        # dès la résiliation — repartir sur une reconquête (NURTURE) est une
        # décision humaine, jamais la continuation automatique de la
        # séquence en cours (qui référencerait un abonnement qui n'existe
        # plus). Ne bloque jamais de futures campagnes de reconquête.
        return "Client parti (résiliation) — séquence arrêtée, reconquête à retravailler manuellement."
    if Suppression.objects.filter(active=True, prospect=prospect).exists():
        return "Prospect en liste d'opposition."
    if prospect.public_email and Suppression.objects.filter(active=True, email__iexact=prospect.public_email).exists():
        return "Adresse e-mail en liste d'opposition."
    if campaign_prospect.status in ("do_not_contact", "lost", "paying", "churned"):
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
    un PK (pas une instance) pour pouvoir verrouiller la ligne.

    Correctif d'audit (round 2) : verrouille explicitement la ligne
    `Campaign` (en plus de `CampaignProspect`) — daily_send_limit/
    total_limit sont des quotas GLOBAUX à la campagne, donc leur vérification
    doit être sérialisée au niveau de la campagne, pas seulement du
    CampaignProspect visé, sans quoi deux workers avançant CONCURREMMENT
    deux prospects différents de la même campagne pourraient tous les deux
    lire un quota non encore consommé par l'autre."""
    now = now or timezone.now()

    with transaction.atomic():
        # of=("self",) : `Campaign.sequence` est une FK nullable
        # (SET_NULL) — select_related() génère donc un LEFT OUTER JOIN, et
        # PostgreSQL refuse FOR UPDATE sur le côté nullable d'un outer join.
        # On ne verrouille que la ligne Campaign elle-même, jamais la ligne
        # EmailSequence jointe (qui n'a pas besoin de l'être ici).
        campaign_id = CampaignProspect.objects.only("campaign_id").get(pk=campaign_prospect_id).campaign_id
        campaign = Campaign.objects.select_for_update(of=("self",)).select_related("sequence").get(pk=campaign_id)
        campaign_prospect = CampaignProspect.objects.select_for_update(of=("self",)).select_related("prospect").get(pk=campaign_prospect_id)
        campaign_prospect.campaign = campaign  # réutilise l'instance déjà verrouillée, évite une requête en double

        # Garde-fou restauré (audit) : une campagne brouillon/non validée ne
        # doit produire AUCUN envoi réel, sur aucun canal — vérifié en tout
        # premier, avant même les conditions d'arrêt par prospect.
        if not campaign.is_sendable:
            return {"action": "not_sendable", "reason": "Campagne non validée ou non active (draft)."}

        reason = _stop_reason(campaign_prospect)
        if reason:
            if campaign_prospect.status not in ("do_not_contact", "lost", "paying", "churned"):
                campaign_prospect.status = "do_not_contact" if "opposition" in reason.lower() or "ne plus contacter" in reason.lower() else campaign_prospect.status
                campaign_prospect.excluded_reason = reason
                campaign_prospect.save(update_fields=["status", "excluded_reason", "updated_at"])
            return {"action": "stopped", "reason": reason}

        sequence = campaign.sequence
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

        # Garde-fous restaurés (audit) : limites d'envoi quotidien/total,
        # tous canaux confondus.
        today_count, total_count = _campaign_action_counts(campaign, now.date())
        if total_count >= campaign.total_limit:
            return {"action": "blocked_total_limit", "step": step.name}
        if today_count >= campaign.daily_send_limit:
            return {"action": "blocked_daily_limit", "step": step.name}

        # Politique domaine/jour existante (ETAPE 17), réappliquée à l'étape e-mail.
        if step.channel == "email":
            domain = _domain(campaign_prospect.prospect.public_email)
            if domain and _domain_already_contacted_today(campaign, domain, now.date()):
                return {"action": "skipped_domain_already_contacted_today", "step": step.name}

        return _execute_step(campaign_prospect, step, now, linkedin_provider)


def _build_linkedin_message(prospect):
    """Mission 6, section 16 : personnalisation limitée aux phrases
    pré-approuvées de message_guardrails.py — jamais une affirmation
    d'intention inventée à partir d'un simple signal de maturité."""
    phrases = build_personalization_snippet(prospect)
    if phrases:
        observation = " et ".join(phrases)
        return f"Bonjour, j'ai remarqué que {observation}. Je vous partage volontiers un exemple concret d'utilisation de PredictNeed IA."
    return "Bonjour, je vous partage volontiers un exemple concret d'utilisation de PredictNeed IA pour votre activité."


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
        # Correctif d'audit (round 2) : un échec provider ne doit JAMAIS être
        # traité comme une réussite — l'étape n'avance pas (current_step
        # inchangé), pour permettre un nouvel essai (retry/backoff) au
        # prochain appel, ou une intervention humaine (l'échec reste visible
        # dans ContactLog).
        if log.outcome == "invitation_failed":
            return {"action": "linkedin_failed", "outcome": log.outcome, "step": step.name, "error": log.metadata.get("detail", "")}
        _mark_step_done(campaign_prospect, step, now)
        return {"action": "linkedin_invitation", "outcome": log.outcome, "step": step.name}

    if step.channel == "linkedin_message":
        if not linkedin_profile_url(prospect):
            _mark_step_done(campaign_prospect, step, now)
            return {"action": "skipped_no_linkedin_profile", "step": step.name}
        variant_message = _build_linkedin_message(prospect)
        log = send_message(prospect, variant_message, provider=linkedin_provider)
        log.campaign_prospect = campaign_prospect
        log.email_step = step
        log.save(update_fields=["campaign_prospect", "email_step"])
        if log.outcome == "message_failed":
            return {"action": "linkedin_failed", "outcome": log.outcome, "step": step.name, "error": log.metadata.get("detail", "")}
        _mark_step_done(campaign_prospect, step, now)
        return {"action": "linkedin_message", "outcome": log.outcome, "step": step.name}

    if step.channel == "email":
        variant = step.variants.filter(active=True).first()
        if not variant or not prospect.public_email:
            _mark_step_done(campaign_prospect, step, now)
            return {"action": "skipped_no_email", "step": step.name}

        frozen_content = None
        if campaign_prospect.campaign.planning_managed:
            # Section D — dernier verrou anti-doublon avant SMTP, uniquement
            # pour le premier contact (une relance J4/J8/J14 de la séquence
            # en cours reste évidemment autorisée). Un bug de sélection en
            # amont ne peut donc jamais produire un doublon réel.
            from .email_automation import has_prior_commercial_first_contact, is_content_stale
            from ..models import PlannedEmailContent

            if step.order == 1 and has_prior_commercial_first_contact(prospect):
                campaign_prospect.status = "excluded"
                campaign_prospect.excluded_reason = "Premier contact commercial déjà envoyé pour ce prospect (verrou global)."
                campaign_prospect.save(update_fields=["status", "excluded_reason", "updated_at"])
                return {"action": "blocked_duplicate_first_contact", "step": step.name}

            # Section E — sans contenu validé et figé, aucun envoi
            # automatique n'est possible : 0 SMTP commercial sans
            # approbation humaine explicite.
            planned = PlannedEmailContent.objects.filter(
                campaign_prospect=campaign_prospect, email_step=step, status="validated",
            ).first()
            if not planned or is_content_stale(planned):
                return {"action": "blocked_awaiting_validation", "step": step.name}
            frozen_content = {
                "subject": planned.subject, "html_body": planned.html_body,
                "text_body": planned.text_body, "open_tracking_token": planned.open_tracking_token,
            }

        record = send_predictneed_campaign_email(campaign_prospect, email_step=step, email_variant=variant, frozen_content=frozen_content)

        # Correctif d'audit (round 3) : ne jamais avancer l'étape comme si
        # l'e-mail avait été envoyé quand ce n'est pas le cas.
        if record.status == "sent":
            _mark_step_done(campaign_prospect, step, now)
            return {"action": "email", "outcome": record.status, "step": step.name}

        if record.status == "suppressed":
            # Opposition détectée juste avant l'envoi (re-vérification dans
            # send_predictneed_campaign_email) : arrêt de la séquence / DNC,
            # même logique que _stop_reason() pour une opposition.
            campaign_prospect.status = "do_not_contact"
            campaign_prospect.excluded_reason = "Opposition détectée juste avant l'envoi (e-mail)."
            campaign_prospect.save(update_fields=["status", "excluded_reason", "updated_at"])
            return {"action": "email_suppressed", "outcome": record.status, "step": step.name}

        if record.status == "blocked":
            # Aucune adresse exploitable au moment de l'envoi (raison déjà
            # posée par send_predictneed_campaign_email) — n'avance pas,
            # raison exploitable renvoyée, mais pas un échec technique
            # "retryable" au sens strict (rien ne changera sans nouvelle
            # donnée de contact).
            return {"action": "email_blocked", "outcome": record.status, "step": step.name, "error": record.error}

        # "failed" (échec SMTP) ou tout autre statut inattendu : jamais
        # traité comme un succès. N'avance pas current_step — reste
        # rejouable au prochain appel (retry), erreur conservée sur l'EmailSend.
        return {"action": "email_failed", "outcome": record.status, "step": step.name, "error": record.error}

    _mark_step_done(campaign_prospect, step, now)
    return {"action": "unknown_channel", "step": step.name}


def run_campaign_sequences(campaign, limit=None, now=None, linkedin_provider=None):
    """Fait avancer d'un cran chaque CampaignProspect éligible de la
    campagne. Ne s'occupe QUE des CampaignProspect déjà entrés dans la
    séquence (status != identified/selected sans étape) — la sélection
    initiale reste gérée ailleurs (validation de campagne)."""
    now = now or timezone.now()
    candidates = campaign.campaign_prospects.exclude(
        status__in=["do_not_contact", "lost", "paying", "excluded", "churned"],
    ).order_by("-acquisition_score_snapshot")
    if limit:
        candidates = candidates[:limit]

    results = []
    for campaign_prospect in candidates:
        result = advance_campaign_prospect(campaign_prospect.pk, now=now, linkedin_provider=linkedin_provider)
        results.append({"campaign_prospect_id": campaign_prospect.pk, **result})
    return results
