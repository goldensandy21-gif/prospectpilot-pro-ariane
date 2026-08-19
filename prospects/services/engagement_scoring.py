"""Mission 6, section 5 — Score ENGAGEMENT.

Réutilise EngagementEvent tel quel (aucun nouvel événement, aucune nouvelle
table) : ENGAGEMENT mesure l'interaction RÉELLE et déjà enregistrée d'un
prospect avec ProspectPilot/PredictNeed IA (clic sur une campagne, visite
PredictNeed, simulateur démarré/terminé, inscription, paiement...) —
contrairement à INTENT (services/intent_scoring.py), qui infère une intention
à partir de signaux externes (recrutement, refonte...), ENGAGEMENT ne fait
aucune inférence : chaque point vient d'un événement effectivement survenu.

Comme pour INTENT, chaque événement pèse selon sa fraîcheur
(services/signal_freshness.py) — un simulateur terminé il y a 2 jours compte
plus qu'un abonnement annulé il y a un an.
"""
from .signal_freshness import signal_freshness

# Poids par type d'événement. `email_sent`/`email_failed` sont des actions
# SORTANTES de ProspectPilot, pas un engagement du prospect : exclus (poids 0).
EVENT_WEIGHTS = {
    "email_sent": 0,
    "email_failed": 0,
    "link_clicked": 10,
    "product_visited": 15,
    "simulator_started": 20,
    "simulator_completed": 30,
    "signup_started": 25,
    "signup_completed": 40,
    "checkout_started": 45,
    "subscription_activated": 60,
    "subscription_cancelled": -30,
}

EVENT_LABELS = dict(
    email_sent="E-mail envoyé", email_failed="E-mail en échec", link_clicked="Lien cliqué",
    product_visited="Visite du produit", simulator_started="Simulateur démarré",
    simulator_completed="Simulateur terminé", signup_started="Inscription démarrée",
    signup_completed="Inscription terminée", checkout_started="Paiement démarré",
    subscription_activated="Abonnement activé", subscription_cancelled="Abonnement annulé",
)


def _clip(value, lo=0, hi=100):
    return max(lo, min(hi, round(value)))


def compute_engagement_score(prospect, now=None):
    """Retourne (score, reasons). Pas de score de base : l'absence
    d'engagement réel doit se lire comme 0, jamais comme une valeur neutre
    inventée (mission 6, section 16 — ne jamais affirmer ce qui n'est pas
    prouvé)."""
    events = list(prospect.engagement_events.all())
    if not events:
        return 0, ["Aucun événement d'engagement enregistré."]

    score = 0
    contributions = []
    for event in events:
        weight = EVENT_WEIGHTS.get(event.event_type, 0)
        if weight == 0:
            continue
        freshness = signal_freshness(event.occurred_at, now=now)
        impact = weight * freshness["multiplier"]
        score += impact
        contributions.append((event, impact, freshness))

    contributions.sort(key=lambda item: abs(item[1]), reverse=True)
    reasons = []
    for event, impact, freshness in contributions[:5]:
        if impact == 0:
            continue
        age_txt = f"{freshness['age_days']:.0f}j" if freshness["age_days"] is not None else "date inconnue"
        label = EVENT_LABELS.get(event.event_type, event.event_type)
        reasons.append(f"{label} ({freshness['label']}, {age_txt}, impact {impact:+.1f})")

    if not reasons:
        reasons.append("Événements enregistrés mais tous obsolètes : impact actuel nul.")

    return _clip(score), reasons


def apply_engagement_score(prospect, now=None, persist=True):
    from django.utils import timezone

    score, reasons = compute_engagement_score(prospect, now=now)
    if persist:
        prospect.engagement_score = score
        prospect.engagement_score_reasons = reasons
        prospect.scores_computed_at = now or timezone.now()
        prospect.save(update_fields=["engagement_score", "engagement_score_reasons", "scores_computed_at"])
    return score, reasons
