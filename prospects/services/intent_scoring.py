"""Mission 6, section 5 — Score INTENT.

INTENT est un score TEMPOREL neuf, distinct de `icp_fit_score` (structure) et
de `predictneed_acquisition_score` (priorité, formule PredictNeed inchangée
par cette mission). Il ne lit QUE les ProspectSignal dont `signal_group ==
"intent"` (recrutement Growth/CRO, refonte digitale, nouvel outil marketing,
activité publique pertinente, contenu conversion/acquisition récent...) —
jamais les signaux "fit" (ex. CRM/analytics détecté = maturité structurelle,
pas une intention d'achat). Voir CATEGORY_TO_GROUP dans services/signals.py
pour la classification catégorie -> groupe, qui est la seule source de vérité
sur ce qui compte comme "intent".

Chaque signal pèse selon son impact actuel (services/signal_freshness.py),
jamais son impact brut : un signal "intent" vieux de 6 mois ne doit plus
faire monter le score. Le score et ses raisons sont entièrement
déterministes et explicables (mission 6, section 6).
"""
from .signal_freshness import signal_effective_impact

BASE_SCORE = 20
REPETITION_BONUS_PER_EXTRA_SIGNAL = 4
REPETITION_BONUS_CAP = 20
RECENT_SIGNAL_THRESHOLD_DAYS = 7


def _clip(value, lo=0, hi=100):
    return max(lo, min(hi, round(value)))


def compute_intent_score(prospect, now=None):
    """Retourne (score, reasons) — jamais persisté ici, voir
    apply_intent_score pour l'écriture sur le Prospect."""
    intent_signals = list(prospect.signals.filter(signal_group="intent"))
    if not intent_signals:
        return 0, ["Aucun signal d'intention détecté."]

    score = BASE_SCORE
    reasons = []
    recent_count = 0
    contributions = []

    for signal in intent_signals:
        impact, freshness = signal_effective_impact(signal, now=now)
        contributions.append((signal, impact, freshness))
        if impact == 0:
            continue
        score += impact
        if freshness["age_days"] is not None and freshness["age_days"] <= RECENT_SIGNAL_THRESHOLD_DAYS:
            recent_count += 1

    contributions.sort(key=lambda item: abs(item[1]), reverse=True)
    for signal, impact, freshness in contributions[:5]:
        if impact == 0:
            continue
        age_txt = f"{freshness['age_days']:.0f}j" if freshness["age_days"] is not None else "date inconnue"
        reasons.append(f"{signal.label} ({freshness['label']}, {age_txt}, impact {impact:+.1f})")

    # Répétition de plusieurs signaux récents (mission 6, section 5) : un
    # prospect avec 3 signaux d'intention dont 2 récents est plus "chaud"
    # qu'un prospect avec un seul signal isolé, même à impact brut égal.
    if recent_count >= 2:
        bonus = min(REPETITION_BONUS_CAP, (recent_count - 1) * REPETITION_BONUS_PER_EXTRA_SIGNAL)
        score += bonus
        reasons.insert(0, f"{recent_count} signaux d'intention récents (< {RECENT_SIGNAL_THRESHOLD_DAYS}j), dont au moins 2 : bonus de répétition (+{bonus}).")

    if not reasons:
        reasons.append("Signaux d'intention présents mais tous obsolètes : impact actuel nul.")

    return _clip(score), reasons


def apply_intent_score(prospect, now=None, persist=True):
    from django.utils import timezone

    score, reasons = compute_intent_score(prospect, now=now)
    if persist:
        prospect.intent_score = score
        prospect.intent_score_reasons = reasons
        prospect.scores_computed_at = now or timezone.now()
        prospect.save(update_fields=["intent_score", "intent_score_reasons", "scores_computed_at"])
    return score, reasons
