"""Mission 6, section 6 — Point d'entrée unique pour recalculer FIT/INTENT/
ENGAGEMENT sur un prospect.

FIT reste `icp_fit_score` (calculé par predictneed_scoring.py, formule
inchangée). INTENT et ENGAGEMENT sont calculés ici en une seule sauvegarde
(au lieu d'appeler apply_intent_score puis apply_engagement_score séparément,
ce qui écrirait `scores_computed_at` deux fois pour rien).
"""
from django.utils import timezone

from .engagement_scoring import compute_engagement_score
from .intent_scoring import compute_intent_score


def recompute_acquisition_scores(prospect, now=None, persist=True):
    now = now or timezone.now()
    previous_intent_score = prospect.intent_score
    intent_score, intent_reasons = compute_intent_score(prospect, now=now)
    engagement_score, engagement_reasons = compute_engagement_score(prospect, now=now)

    result = {
        "fit_score": prospect.icp_fit_score,
        "intent_score": intent_score,
        "intent_score_reasons": intent_reasons,
        "engagement_score": engagement_score,
        "engagement_score_reasons": engagement_reasons,
    }

    if persist:
        prospect.intent_score = intent_score
        prospect.intent_score_reasons = intent_reasons
        prospect.engagement_score = engagement_score
        prospect.engagement_score_reasons = engagement_reasons
        prospect.scores_computed_at = now
        prospect.save(update_fields=[
            "intent_score", "intent_score_reasons",
            "engagement_score", "engagement_score_reasons", "scores_computed_at",
        ])

        # Mission 6, section 15 : alerte uniquement si ce recalcul fait
        # réellement franchir un seuil actionnable — jamais à chaque appel.
        from .alerts import check_intent_threshold_alert
        check_intent_threshold_alert(prospect, previous_intent_score, now=now)

    return result
