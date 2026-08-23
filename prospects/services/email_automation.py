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
    EmailSequence,
    EmailStep,
    EmailVariant,
    PlannedEmailContent,
)
from .campaign_sequencing import _next_step as _sequence_next_step
from .campaign_sequencing import advance_campaign_prospect
from .predictneed_email import (
    editable_body_text_for_step,
    render_custom_planned_content,
    render_predictneed_email,
    send_predictneed_campaign_email,
)
from .suppression import is_suppressed


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


def render_live_content(campaign_prospect, email_step):
    """Rendu TOUJOURS sans pixel d'ouverture (section 5, audit correctif
    final) : le contenu préparé/testé/haché reste strictement le contenu
    commercial visible. Le pixel n'est injecté qu'au moment du VRAI envoi
    commercial, jamais avant (voir predictneed_email.inject_open_pixel)."""
    variant = email_step.variants.filter(active=True).first()
    subject, html, text = render_predictneed_email(campaign_prospect, email_step, variant, request=None)
    return subject, html, text


def prepare_planned_content(campaign_prospect, email_step, scheduled_date):
    """« Préparer » — génère LA version candidate (un seul rendu, sans
    pixel), statut « à relire ». Toute nouvelle préparation efface une
    éventuelle prise en main manuelle antérieure (`manually_edited_at`) :
    ce rendu auto-généré redevient la version de référence — y compris
    `editable_body_text` (workflow live preview, section 1), recalculé ici
    à partir des mêmes ingrédients (ctx) que `html_body`/`text_body`, jamais
    extrait par un parsing fragile de ces derniers. `tested_content_hash`/
    `test_sent_at` sont réinitialisés (le test, désormais facultatif, ne
    représente plus le contenu courant)."""
    subject, html, text = render_live_content(campaign_prospect, email_step)
    editable_body_text = editable_body_text_for_step(campaign_prospect, email_step)
    content_hash = content_hash_for(subject, html, text)

    planned, _created = PlannedEmailContent.objects.update_or_create(
        campaign_prospect=campaign_prospect, email_step=email_step,
        defaults={
            "subject": subject, "html_body": html, "text_body": text,
            "editable_body_text": editable_body_text,
            "content_hash": content_hash,
            "scheduled_date": scheduled_date,
            "approved_by": None, "approved_at": None,
            "tested_content_hash": "", "test_sent_at": None,
            "manually_edited_at": None,
            "status": "to_validate",
        },
    )
    return planned


def is_content_stale(planned):
    """Compare le hash figé à un nouveau rendu live (sans pixel — seul le
    contenu commercial visible compte) : si le prospect/produit a changé
    depuis la préparation, le contenu est jugé obsolète — jamais renvoyé
    silencieusement.

    Workflow final (section 4/9) : une fois qu'une modification manuelle a
    été enregistrée (`manually_edited_at` renseigné), ce contrôle n'a plus
    lieu d'être — la version humaine fait foi, elle ne doit jamais être
    signalée « périmée » simplement parce qu'un nouveau rendu automatique
    donnerait un texte différent (c'est précisément le but de l'édition
    manuelle de s'en écarter).

    Recharge explicitement `campaign_prospect`/`prospect` depuis la base à
    chaque appel plutôt que d'utiliser `planned.campaign_prospect` tel quel :
    un objet Django met en cache la relation dès son premier accès, ce qui
    masquerait silencieusement une modification survenue entre-temps (tout
    l'intérêt de cette fonction est justement de la détecter)."""
    if planned.manually_edited_at:
        return False
    campaign_prospect = CampaignProspect.objects.select_related("prospect", "campaign").get(
        pk=planned.campaign_prospect_id,
    )
    subject, html, text = render_live_content(campaign_prospect, planned.email_step)
    return content_hash_for(subject, html, text) != planned.content_hash


def mark_stale_if_changed(planned):
    if planned.status == "validated" and is_content_stale(planned):
        planned.status = "stale"
        planned.save(update_fields=["status", "updated_at"])
    return planned


def send_test_email(campaign_prospect, planned, test_recipient, request=None, now=None):
    """« Envoyer les tests » — envoie EXACTEMENT planned.{subject,html_body,
    text_body} (jamais un nouveau rendu) à `test_recipient`, avec
    is_test=True (donc jamais de pixel — voir
    predictneed_email.send_predictneed_campaign_email). Si l'envoi réussit,
    marque `planned` comme testé POUR CE CONTENU PRÉCIS (section 4) :
    `validate_planned_content` exigera `tested_content_hash == content_hash`
    pour autoriser la validation. Un échec d'envoi ne marque rien — le test
    devra être relancé."""
    record = send_predictneed_campaign_email(
        campaign_prospect, planned.email_step, request=request, is_test=True,
        test_recipient=test_recipient,
        frozen_content={"subject": planned.subject, "html_body": planned.html_body, "text_body": planned.text_body},
        now=now,
    )
    if record.status == "sent":
        planned.tested_content_hash = planned.content_hash
        planned.test_sent_at = timezone.now()
        planned.save(update_fields=["tested_content_hash", "test_sent_at", "updated_at"])
    return record


def validate_planned_content(planned, user):
    """« Programmer » — l'action humaine explicite d'approbation (workflow
    final, section 6/9). NE régénère jamais le texte. Renvoie
    (autorisé: bool, motif: str) — motif vide si autorisé.

    Le test email est désormais FACULTATIF (section 5/9) : il ne conditionne
    plus cette fonction — `approved_by`/`approved_at`/`status="validated"`
    sont eux-mêmes la preuve de l'approbation humaine du `content_hash`
    COURANT, que ce contenu ait été testé ou non. `tested_content_hash`
    reste purement informatif (badge « Testé » dans l'interface) et n'est
    jamais falsifié ici.

    Garde-fous, dans cet ordre :
    1) le rendu live doit toujours correspondre au hash figé lors de la
       préparation (sauf prise en main manuelle, voir is_content_stale) ;
       si les données source ont changé entre-temps -> `stale` (il faut
       relancer « Préparer », ou modifier le contenu à la main) ;
    2) le prospect doit toujours avoir une adresse exploitable -> `no_email`
       sinon ;
    3) le CampaignProspect ne doit pas être dans un état d'arrêt
       (do_not_contact/excluded/lost/churned) -> `prospect_not_eligible` ;
    4) le prospect ne doit être ni exclu (predictneed_excluded) ni en
       opposition/suppression (is_suppressed, qui couvre déjà
       prospecting_allowed=False et Prospect.status="do_not_contact") ->
       `prospect_not_eligible`.

    Une fois validée, une modification ultérieure de CE contenu (édition
    manuelle ou nouvelle préparation) efface systématiquement
    approved_by/approved_at et repasse le statut à « modifié »/« à relire »
    (voir apply_manual_edit/prepare_planned_content) — jamais d'envoi sur
    la base d'une ancienne approbation portant sur un contenu différent."""
    if is_content_stale(planned):
        planned.status = "stale"
        planned.save(update_fields=["status", "updated_at"])
        return False, "stale"

    campaign_prospect = planned.campaign_prospect
    prospect = campaign_prospect.prospect
    email = prospect.public_email or ""
    if not email:
        return False, "no_email"
    if campaign_prospect.status in ("do_not_contact", "excluded", "lost", "churned"):
        return False, "prospect_not_eligible"
    if prospect.predictneed_excluded or is_suppressed(email, prospect=prospect):
        return False, "prospect_not_eligible"

    planned.status = "validated"
    planned.approved_by = user
    planned.approved_at = timezone.now()
    planned.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return True, ""


def apply_manual_edit(planned, subject, body_text, request=None):
    """« Modifier » (section 3/4, workflow final) — remplace le sujet et le
    texte rédactionnel visible d'UNE ligne PlannedEmailContent précise
    (jamais du HTML brut manipulé par l'utilisatrice). Reconstruit le
    HTML/texte en conservant automatiquement l'enveloppe PredictNeed
    (bannière, CTA, footer conformité, signature) et les liens techniques
    réels (tracking, unsubscribe) — voir
    predictneed_email.render_custom_planned_content(). Ne touche JAMAIS :
    le template global, l'EmailVariant, l'AgentBrief, ni aucun autre
    PlannedEmailContent — seulement cette ligne.

    Invalide systématiquement toute programmation existante : si le contenu
    était déjà `validated`, il repasse à `modified` (« Modifié — à
    reprogrammer ») ; sinon il reste/redevient `to_validate` (« À relire »).
    `approved_by`/`approved_at` sont toujours effacés. `tested_content_hash`
    n'est JAMAIS falsifié — un ancien test, s'il existait, reste dans
    l'historique mais ne représente plus la version courante (son hash ne
    correspondra simplement plus à `content_hash`)."""
    subject_clean = (subject or "").strip()
    body_clean = (body_text or "").strip()
    campaign_prospect = planned.campaign_prospect
    html, text = render_custom_planned_content(campaign_prospect, planned.email_step, body_clean, request=request)

    was_validated = planned.status == "validated"
    planned.subject = subject_clean
    planned.html_body = html
    planned.text_body = text
    planned.editable_body_text = body_clean
    planned.content_hash = content_hash_for(subject_clean, html, text)
    planned.manually_edited_at = timezone.now()
    planned.approved_by = None
    planned.approved_at = None
    planned.status = "modified" if was_validated else "to_validate"
    planned.save(update_fields=[
        "subject", "html_body", "text_body", "editable_body_text", "content_hash", "manually_edited_at",
        "approved_by", "approved_at", "status", "updated_at",
    ])
    return planned


def freeze_planned_content(campaign_prospect, email_step, user, scheduled_date, test_recipient=""):
    """RÉSERVÉ AUX TESTS AUTOMATISÉS (section J, Round D — audité : à ce jour
    aucun appelant en dehors de prospects/tests/*.py, `grep -rn
    freeze_planned_content prospects/` en fait foi). Combinateur de
    convenance qui prépare, envoie un test réel (désormais facultatif côté
    validation, section 5/9 du workflow final — mais l'envoyer ici reste
    inoffensif et pratique pour les tests automatisés), PUIS programme — en
    un seul appel synchrone, SANS action humaine séparée entre les étapes.

    AUCUN CHEMIN PRODUCTION (vue, tâche Celery, commande de management) ne
    doit jamais appeler cette fonction : le workflow réel, humain, en
    production reste « Préparer » (prepare_planned_content) -> optionnellement
    « Envoyer un test » (send_test_email) -> « Programmer »
    (validate_planned_content), chacune une action explicite séparée. Les
    vues de production (acquisition_views.py::email_planning_prepare_week/
    email_planning_send_tests/email_planning_validate_and_schedule/
    email_planning_content_detail) appellent chacune directement les
    fonctions ci-dessus, jamais ce combinateur.

    Le test part vers `test_recipient`, ou à défaut l'adresse d'expédition
    du produit (adresse interne, jamais un vrai prospect)."""
    planned = prepare_planned_content(campaign_prospect, email_step, scheduled_date)
    recipient = test_recipient or campaign_prospect.campaign.product.sender_email or "contact-predict@predictneed-ia.com"
    send_test_email(campaign_prospect, planned, recipient)
    planned.refresh_from_db()
    validate_planned_content(planned, user)
    planned.refresh_from_db()
    return planned


def promote_campaign_after_validation(campaign, user):
    """Section A (Round D, verrous production) — après une action « Valider
    et programmer » ayant réellement validé au moins un PlannedEmailContent
    pour `campaign` dans ce lot, rend la campagne réellement envoyable par
    le scheduler.

    Pose TOUJOURS un nouvel horodatage (`timezone.now()`) et un nouvel
    approbateur — jamais l'ancien `validated_at` d'avant adoption (une
    campagne adoptée est explicitement remise à draft/validated_at=None
    précisément pour empêcher que son ancienne approbation manuelle ne
    compte ici comme une validation Planning). Ne rétrograde jamais une
    campagne déjà "active" vers "ready". N'est appelée QUE pour une
    campagne ayant au moins un contenu réellement validé dans ce lot — si
    aucun contenu n'a été approuvé, cette fonction n'est simplement jamais
    appelée et la campagne reste non-sendable (is_sendable exige toujours
    status in (ready, active) ET validated_at renseigné).

    Round E, point 2 — garde-fou défensif : refuse explicitement de
    promouvoir une campagne paused/cancelled/completed, même si un
    appelant l'invoquait par erreur (les vues Planning excluent déjà ces
    statuts en amont — ceci est la seconde ligne de défense). Une
    campagne en pause ne redevient jamais "ready" par validation
    implicite ; cancelled/completed ne sont jamais réactivées par ce
    workflow. La reprise d'une campagne en pause reste une action humaine
    explicite et distincte — hors périmètre de ce correctif (aucune
    interface de reprise n'est ajoutée ici) — jamais un effet de bord de
    « Valider et programmer »."""
    if campaign.status in ("paused", "cancelled", "completed"):
        return campaign
    campaign.validated_at = timezone.now()
    campaign.validated_by = user
    if campaign.status != "active":
        campaign.status = "ready"
    campaign.save(update_fields=["status", "validated_at", "validated_by", "updated_at"])
    return campaign


# ---------------------------------------------------------------------------
# 1 — adoption sûre d'une campagne existante dans le Planning
# ---------------------------------------------------------------------------

FOUR_STEP_SPECS = [
    (1, 0, "Premier contact"),
    (2, 4, "Rappel court"),
    (3, 4, "Relance valeur"),
    (4, 6, "Dernier message"),
]


def clone_sequence_for_campaign(sequence, campaign):
    """Section 2 — copie profonde d'une EmailSequence (+ ses EmailStep + leurs
    EmailVariant) en une séquence toute neuve, dédiée à `campaign`. NE
    MODIFIE JAMAIS `sequence` : une autre campagne peut la référencer et doit
    la conserver strictement identique. C'est ce clone, jamais l'original,
    qui sera ensuite normalisé par normalize_planning_sequence()."""
    clone = EmailSequence.objects.create(
        product=sequence.product, icp=sequence.icp,
        name=f"{sequence.name} — copie Planning ({campaign.name})",
        active=sequence.active,
        stop_on_reply=sequence.stop_on_reply,
        stop_on_conversion=sequence.stop_on_conversion,
        stop_on_unsubscribe=sequence.stop_on_unsubscribe,
    )
    for step in sequence.steps.order_by("order"):
        cloned_step = EmailStep.objects.create(
            sequence=clone, order=step.order, delay_days=step.delay_days,
            name=step.name, channel=step.channel, active=step.active,
            advance_condition=step.advance_condition,
        )
        for variant in step.variants.all():
            EmailVariant.objects.create(
                step=cloned_step, name=variant.name, active=variant.active,
                subject_template=variant.subject_template,
                cta_type=variant.cta_type, cta_label_override=variant.cta_label_override,
                weight=variant.weight,
            )
    return clone


def normalize_planning_sequence(sequence):
    """Section 1 — corrige RÉELLEMENT `delay_days`/`channel` sur les 4
    étapes canoniques J0(0)/J4(4)/J8(4)/J14(6) (cumulatif 0/4/8/14), y
    compris sur des étapes DÉJÀ existantes dont le délai serait incorrect —
    par exemple une séquence historique stockée 0/4/8 (get_or_create_default_
    sequence) dont le cumulatif réel serait 0/4/12, pas 0/4/8 puisque
    delay_days est relatif à l'étape précédente. Crée les étapes manquantes
    avec un sujet générique de repli. NE TOUCHE JAMAIS
    EmailVariant.subject_template d'une étape déjà existante (le J0
    personnalisé, notamment, n'est ni modifié ni remplacé) — crée
    uniquement les variantes manquantes.

    NE DOIT JAMAIS être appelée directement sur une séquence potentiellement
    partagée par une autre campagne : `sequence` doit toujours être soit un
    clone dédié (clone_sequence_for_campaign), soit une séquence Planning
    fraîchement créée pour cet usage exclusif
    (get_or_create_planning_default_sequence)."""
    existing_steps = {s.order: s for s in sequence.steps.all()}
    for order, delay, name in FOUR_STEP_SPECS:
        step = existing_steps.get(order)
        if step is None:
            step = EmailStep.objects.create(sequence=sequence, order=order, delay_days=delay, name=name, channel="email")
            EmailVariant.objects.create(
                step=step, name=name, cta_type="simulator",
                subject_template=f"{{{{ company_name }}}} — {name.lower()}",
            )
            continue
        updates = []
        if step.delay_days != delay:
            step.delay_days = delay
            updates.append("delay_days")
        if step.channel != "email":
            step.channel = "email"
            updates.append("channel")
        if updates:
            step.save(update_fields=updates)
        if not step.variants.exists():
            EmailVariant.objects.create(
                step=step, name=name, cta_type="simulator",
                subject_template=f"{{{{ company_name }}}} — {name.lower()}",
            )

    # Section F (Round D) : une séquence legacy clonée peut contenir des
    # étapes au-delà de J14 (order 5, 6...) — jamais laissées actives dans
    # la copie Planning, sans quoi le scheduler continuerait au-delà de la
    # 4e étape. JAMAIS supprimées (elles appartiennent à un clone dédié,
    # aucune autre campagne n'y touche) ni modifiées sur l'originale
    # (celle-ci n'est jamais passée à cette fonction, voir
    # adopt_campaign_into_planning). Le reste du moteur filtre déjà partout
    # sur active=True (_next_step, cumulative_delay_days, render_live_content
    # via email_step.variants.filter(active=True)...), donc une étape
    # désactivée ici n'est plus jamais exécutée par le scheduler.
    for extra_step in sequence.steps.exclude(order__in=[1, 2, 3, 4]).filter(active=True):
        extra_step.active = False
        extra_step.save(update_fields=["active"])

    return sequence


def get_or_create_planning_default_sequence(product, icp):
    """Section 3 — séquence par défaut dédiée aux NOUVELLES campagnes
    Planning sans séquence explicitement choisie. Distincte de
    campaign_sending.get_or_create_default_sequence() (séquence legacy 3
    étapes 0/4/8, jamais garantie 4 étapes) : cette séquence-ci est
    exclusivement utilisée par le Planning, jamais partagée avec une
    campagne manuelle préexistante, donc toujours sûre à normaliser
    directement (pas de clone nécessaire — rien d'autre ne la référence à sa
    création)."""
    sequence, created = EmailSequence.objects.get_or_create(
        product=product, icp=icp,
        name=f"Séquence planning J0-J14 — {icp.name if icp else product.name}",
        defaults={"active": True},
    )
    if created or sequence.steps.count() < 4:
        normalize_planning_sequence(sequence)
    return sequence


def adopt_campaign_into_planning(campaign, user=None):
    """Section 1/2 — fait entrer PROPREMENT une campagne existante (créée
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
    - (section 2) CLONE TOUJOURS la séquence avant de la compléter à 4
      étapes — jamais de modification de la séquence d'origine, que celle-ci
      soit ou non partagée par une autre campagne : une autre campagne
      utilisant l'ancienne séquence reste ainsi strictement byte pour byte
      inchangée ;
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

    clone = clone_sequence_for_campaign(campaign.sequence, campaign)
    normalize_planning_sequence(clone)

    campaign.sequence = clone
    campaign.planning_managed = True
    campaign.status = "draft"
    campaign.validated_at = None
    campaign.validated_by = None
    if campaign.total_limit < 4:
        campaign.total_limit = 4
    campaign.save(update_fields=["sequence", "planning_managed", "status", "validated_at", "validated_by", "total_limit"])
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

    Round E, point 3 — pour chaque NOUVEAU J0 ainsi placé, calcule aussi ses
    étapes suivantes (J4/J8/J14, délais canoniques depuis la date
    prévisionnelle de CE J0) et les inclut également si leur date tombe
    dans la semaine en cours de préparation (jusqu'au vendredi de la
    semaine de `start` inclus) — pour que l'utilisatrice puisse déjà tester
    et valider tout ce qui peut partir cette semaine, sans devoir revenir
    y intervenir. Les étapes plus tardives (ex : J8/J14 d'un J0 de lundi)
    seront préparées lors de la préparation de leur propre semaine. Ces
    étapes anticipées n'occupent PAS `daily_total_limit`/`new_contacts_per_day`
    (ce ne sont que des contenus préparés à l'avance, pas des envois) et ne
    font JAMAIS avancer `current_step` — le moteur d'exécution réel
    (campaign_sequencing.py) reste seul juge du moment réel d'envoi, avec
    ses garde-fous inchangés (J4 n'part jamais si J0 n'a pas réellement
    réussi, délai réel depuis l'étape précédente, stop conditions).

    Renvoie une liste de (date, campaign_prospect, email_step) — n'écrit
    rien elle-même (voir prepare_planned_content, appelé par l'appelant)."""
    now = now or timezone.now()
    settings_row = EmailAutomationSettings.current()
    today = local_today(now, settings_row)
    start = today if today.weekday() < 5 else next_business_day(today)
    week_monday = start - timedelta(days=start.weekday())
    week_end = week_monday + timedelta(days=4)  # vendredi de la semaine de `start`

    # Section I (Round D) : une campagne en pause/annulée/terminée ne doit
    # jamais se voir préparer de nouveau contenu — brouillon/prête/active
    # restent autorisées (une campagne "draft" peut légitimement préparer/
    # tester avant sa toute première validation).
    campaigns = Campaign.objects.filter(planning_managed=True).exclude(
        status__in=["paused", "cancelled", "completed"],
    ).select_related("sequence")

    remaining_followups = _due_followups(campaigns, start)
    remaining_new = _pending_new_contacts(campaigns)

    plan = []
    future_steps = []
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

            sequence = cp.campaign.sequence
            for later_step in sequence.steps.filter(active=True, order__gt=step.order).order_by("order"):
                later_date = compute_scheduled_date(current_day, sequence, later_step)
                if later_date > week_end:
                    break  # delay_days >= 0 : les étapes suivantes ne feront que s'éloigner davantage
                future_steps.append((later_date, cp, later_step))

        day_index += 1

    plan.extend(future_steps)
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
