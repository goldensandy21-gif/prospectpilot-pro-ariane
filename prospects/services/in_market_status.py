"""Mission 6, section 7 — Statut "IN MARKET NOW".

Un statut CALCULÉ à partir de `intent_score` (lui-même déjà pondéré par la
fraîcheur — voir services/intent_scoring.py) — jamais une affirmation
absolue du type "Ce prospect veut acheter". La phrase reste toujours au
conditionnel/probabiliste : "Signaux compatibles avec une intention d'achat
probable.", jamais "veut acheter" ou "va acheter".
"""
IN_MARKET_LEVELS = [
    (0, 19, "no_signal", "Aucun signal récent"),
    (20, 39, "weak", "Signaux faibles"),
    (40, 59, "emerging", "Signaux émergents"),
    (60, 79, "probable", "Intention probable"),
    (80, 100, "strong", "Intention forte"),
]

PHRASES = {
    "no_signal": "Aucun signal d'intention récent détecté.",
    "weak": "Quelques signaux faibles, à surveiller plutôt qu'à contacter en priorité.",
    "emerging": "Signaux émergents compatibles avec un début d'intention d'achat, à confirmer.",
    "probable": "Signaux compatibles avec une intention d'achat probable.",
    "strong": "Plusieurs signaux récents et convergents, compatibles avec une intention d'achat forte.",
}


def in_market_status(prospect):
    """Retourne {code, label, phrase} — jamais persisté (recalculé à la
    volée depuis intent_score, pour ne jamais désynchroniser un statut figé
    d'un score qui, lui, est recalculé régulièrement)."""
    score = prospect.intent_score
    for low, high, code, label in IN_MARKET_LEVELS:
        if low <= score <= high:
            return {"code": code, "label": label, "phrase": PHRASES[code], "intent_score": score}
    return {"code": "no_signal", "label": "Aucun signal récent", "phrase": PHRASES["no_signal"], "intent_score": score}
