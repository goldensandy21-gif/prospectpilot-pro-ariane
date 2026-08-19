"""Mission 6, section 15 — Alertes.

Uniquement pour un changement RÉEL, jamais à chaque recalcul :
- franchissement d'un seuil INTENT (uniquement à la MONTÉE, uniquement vers
  un niveau actionnable "probable"/"forte") ;
- nouveau signal fort (intent/engagement, positif, impact élevé) ;
- nouvel engagement PredictNeed ;
- réactivation après inactivité (>= REACTIVATION_INACTIVITY_DAYS jours sans
  signal ni engagement, puis un nouveau).

Dédoublonnage garanti par la contrainte unique sur Alert(prospect,
alert_type, dedup_key) — `get_or_create` ne crée jamais deux fois la même
alerte. Chaque type choisit une clé qui identifie l'ÉVÉNEMENT réel :
l'ID du signal/événement déclencheur quand il existe (naturellement unique,
ne se répète jamais pour le même événement), ou un panier "jour" pour
intent_threshold_crossed (pas d'événement discret disponible sans historiser
les scores — ce que la mission interdit de faire en double).
"""
from django.db.models.functions import Coalesce
from django.utils import timezone

from ..models import Alert
from .in_market_status import IN_MARKET_LEVELS

STRONG_SIGNAL_IMPACT_THRESHOLD = 7
REACTIVATION_INACTIVITY_DAYS = 30

_LEVEL_RANK = {"no_signal": 0, "weak": 1, "emerging": 2, "probable": 3, "strong": 4}
_LEVEL_LABEL = {code: label for _, _, code, label in IN_MARKET_LEVELS}
_ACTIONABLE_LEVELS = {"probable", "strong"}


def _level_for_score(score):
    for low, high, code, _label in IN_MARKET_LEVELS:
        if low <= score <= high:
            return code
    return "no_signal"


def _last_activity_before(prospect, before_at):
    # `observed_at` (quand l'événement a réellement eu lieu) prime toujours
    # sur `detected_at` (quand ProspectPilot a créé la ligne) — cohérent avec
    # services/signal_freshness.py et commercial_timeline.py.
    last_signal_at = (
        prospect.signals.annotate(activity_at=Coalesce("observed_at", "detected_at"))
        .filter(activity_at__lt=before_at).order_by("-activity_at")
        .values_list("activity_at", flat=True).first()
    )
    last_engagement_at = prospect.engagement_events.filter(occurred_at__lt=before_at).order_by("-occurred_at").values_list("occurred_at", flat=True).first()
    candidates = [d for d in (last_signal_at, last_engagement_at) if d]
    return max(candidates) if candidates else None


def _maybe_create_reactivation_alert(prospect, trigger_at, dedup_key, trigger_label):
    if trigger_at is None:
        return None
    last_activity = _last_activity_before(prospect, trigger_at)
    if last_activity is None:
        return None  # jamais actif avant : ce n'est pas une RÉactivation.
    inactivity_days = (trigger_at - last_activity).total_seconds() / 86400
    if inactivity_days < REACTIVATION_INACTIVITY_DAYS:
        return None
    alert, created = Alert.objects.get_or_create(
        prospect=prospect, alert_type="reactivated", dedup_key=dedup_key,
        defaults={"message": f"Prospect réactivé après {inactivity_days:.0f} jours d'inactivité ({trigger_label})."},
    )
    return alert if created else None


def check_signal_alerts(prospect, saved_signals):
    """Appelé depuis persist_signals() (services/signals.py), seul point
    d'entrée pour toute écriture de ProspectSignal — donc valable quel que
    soit le pipeline d'origine (acquisition_pipeline, SignalCollector...).
    Ne considère que les signaux réellement NOUVEAUX (`_was_created`), pas
    les rafraîchissements d'un signal déjà connu."""
    created_alerts = []
    for signal in saved_signals:
        if not getattr(signal, "_was_created", False):
            continue
        trigger_at = signal.observed_at or signal.detected_at

        if signal.positive and signal.signal_group in ("intent", "engagement") and signal.score_impact >= STRONG_SIGNAL_IMPACT_THRESHOLD:
            alert, created = Alert.objects.get_or_create(
                prospect=prospect, alert_type="strong_signal", dedup_key=f"signal:{signal.pk}",
                defaults={"message": f"Signal fort détecté : {signal.label}."},
            )
            if created:
                created_alerts.append(alert)

        reactivation = _maybe_create_reactivation_alert(
            prospect, trigger_at, f"reactivated_by_signal:{signal.pk}", signal.label,
        )
        if reactivation:
            created_alerts.append(reactivation)
    return created_alerts


def check_intent_threshold_alert(prospect, previous_intent_score, now=None):
    """Appelé depuis recompute_acquisition_scores() (services/
    acquisition_scores.py) juste après avoir recalculé intent_score. N'alerte
    que sur une MONTÉE vers un niveau actionnable — jamais sur une baisse, ni
    sur un recalcul qui laisse le niveau inchangé."""
    now = now or timezone.now()
    previous_level = _level_for_score(previous_intent_score)
    current_level = _level_for_score(prospect.intent_score)

    if current_level == previous_level or current_level not in _ACTIONABLE_LEVELS:
        return None
    if _LEVEL_RANK[current_level] <= _LEVEL_RANK.get(previous_level, -1):
        return None

    dedup_key = f"{current_level}:{now.date().isoformat()}"
    alert, created = Alert.objects.get_or_create(
        prospect=prospect, alert_type="intent_threshold_crossed", dedup_key=dedup_key,
        defaults={"message": f"Intent passé à « {_LEVEL_LABEL[current_level]} » ({prospect.intent_score}/100)."},
    )
    return alert if created else None


def check_engagement_alert(prospect, engagement_event):
    """Appelé depuis predictneed_webhook.py juste après la création d'un
    EngagementEvent avec source="predictneed"."""
    alerts = []
    alert, created = Alert.objects.get_or_create(
        prospect=prospect, alert_type="new_engagement", dedup_key=f"event:{engagement_event.pk}",
        defaults={"message": f"Nouvel engagement PredictNeed : {engagement_event.get_event_type_display()}."},
    )
    if created:
        alerts.append(alert)

    reactivation = _maybe_create_reactivation_alert(
        prospect, engagement_event.occurred_at, f"reactivated_by_event:{engagement_event.pk}",
        engagement_event.get_event_type_display(),
    )
    if reactivation:
        alerts.append(reactivation)
    return alerts
