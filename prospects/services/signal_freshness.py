"""Mission 6, section 4 — Fraîcheur des signaux.

Un signal perd de la valeur avec le temps : un recrutement Growth détecté il
y a 2 jours ne doit pas peser autant qu'un signal détecté il y a 60 jours,
même si son `score_impact` brut est identique. Cette fraîcheur est calculée
UNE SEULE FOIS ici (jamais recopiée/redéfinie ailleurs — intent_scoring.py et
engagement_scoring.py importent ces fonctions au lieu de coder leurs propres
seuils) et s'applique par-dessus le score_impact brut, qui lui reste
inchangé : `ProspectSignal.score_impact` n'est jamais réécrit par la
fraîcheur, on sépare la note brute du signal de son impact actuel.
"""
from django.utils import timezone

# (borne_max_en_jours, étiquette, multiplicateur), dans l'ordre croissant.
# Le premier seuil dont la borne est >= âge du signal s'applique. Au-delà de
# la dernière borne, le signal est "obsolète" (multiplicateur 0.0) : sa ligne
# reste en base (historique jamais supprimé) mais ne pèse plus dans les
# scores actuels.
FRESHNESS_THRESHOLDS_DAYS = [
    (3, "très frais", 1.0),
    (7, "frais", 0.75),
    (30, "récent", 0.5),
    (90, "ancien", 0.2),
]
STALE_LABEL = "obsolète"
STALE_MULTIPLIER = 0.0
UNKNOWN_LABEL = "date inconnue"


def signal_age_days(observed_at, now=None):
    """Âge en jours (float, jamais négatif) d'une date d'observation. None si
    `observed_at` est absent — un appelant doit alors traiter ce signal comme
    non daté plutôt que de deviner une date."""
    if observed_at is None:
        return None
    now = now or timezone.now()
    return max(0.0, (now - observed_at).total_seconds() / 86400)


def signal_freshness(observed_at, now=None):
    """Fonction canonique unique : renvoie {age_days, label, multiplier} pour
    une date d'observation donnée. `now` est injectable pour les tests
    (pas de dépendance à freezegun)."""
    age_days = signal_age_days(observed_at, now=now)
    if age_days is None:
        return {"age_days": None, "label": UNKNOWN_LABEL, "multiplier": STALE_MULTIPLIER}
    for max_days, label, multiplier in FRESHNESS_THRESHOLDS_DAYS:
        if age_days <= max_days:
            return {"age_days": age_days, "label": label, "multiplier": multiplier}
    return {"age_days": age_days, "label": STALE_LABEL, "multiplier": STALE_MULTIPLIER}


def signal_effective_impact(signal, now=None):
    """Impact ACTUEL d'un ProspectSignal, pondéré par sa fraîcheur — distinct
    de `signal.score_impact` (note brute, jamais modifiée ici). Utilise
    `observed_at` si connu, sinon retombe sur `detected_at` (date de création
    de la ligne, meilleure donnée disponible à défaut d'observation datée)."""
    freshness = signal_freshness(signal.observed_at or signal.detected_at, now=now)
    raw = signal.score_impact if signal.positive else -abs(signal.score_impact)
    return raw * freshness["multiplier"], freshness
