"""Mission 6, section 16 — garde-fous de personnalisation des messages.

Email et LinkedIn ne doivent JAMAIS transformer un signal de maturité (FIT)
en affirmation d'intention d'achat. Exemple interdit, explicitement testé
(voir tests/test_mission6_message_guardrails.py) : "Google Analytics
détecté" (FIT, `analytics_detected`) ne doit jamais devenir "Vous cherchez
actuellement une solution d'analyse comportementale" — c'est un signal
totalement différent (`behaviour_analytics_detected`, lui-même un indice de
maturité, pas d'intention) et ne doit jamais être déduit d'un autre signal.

Principe : aucune génération libre. Chaque `signal_type` autorisé pour la
personnalisation a EXACTEMENT une phrase pré-approuvée, factuelle et hedgée,
listée dans `SAFE_PHRASE_TEMPLATES` — seule source de vérité. Un
`signal_type` non répertorié ne produit AUCUNE phrase : silence plutôt
qu'invention. Réutilisé par la génération de message LinkedIn
(services/campaign_sequencing.py) — un futur générateur e-mail devra passer
par la même fonction plutôt que reconstruire sa propre logique.
"""
from .signal_freshness import signal_age_days

# Phrase pré-approuvée par signal_type. Toutes restent au niveau du FAIT
# observé, jamais une interprétation de l'intention du prospect — y compris
# pour les signaux signal_group="intent" (ex. formulaire de contact présent
# reste un FAIT sur le site, pas une affirmation sur ce que veut l'entreprise).
SAFE_PHRASE_TEMPLATES = {
    # FIT / maturité.
    "analytics_detected": "vous utilisez déjà des outils de suivi d'audience ({value})",
    "gtm_detected": "Google Tag Manager est déjà installé sur votre site",
    "advertising_pixel_detected": "vous investissez déjà dans l'acquisition payante ({value})",
    "crm_detected": "vous utilisez déjà un outil de CRM ou de marketing automation ({value})",
    "behaviour_analytics_detected": "vous utilisez déjà un outil d'analyse comportementale ({value})",
    "social_presence_linkedin": "votre entreprise est présente sur LinkedIn",
    "decision_maker_identified": "{value} occupe une fonction pertinente chez vous",
    # INTENT — toujours formulé comme un fait constaté sur le site, jamais
    # comme une lecture de l'intention du prospect.
    "contact_form_detected": "votre site propose un formulaire de contact",
    "booking_detected": "votre site permet une prise de rendez-vous en ligne",
    "lead_magnet_detected": "vous proposez déjà un contenu à télécharger pour capter des contacts",
    "signup_form_detected": "votre site propose une inscription en ligne",
    "landing_pages_detected": "votre site dispose de pages de conversion dédiées",
}

# Formulations interdites dans tout texte généré par ProspectPilot — signe
# qu'un texte affirme une intention non prouvée plutôt qu'un fait observé.
BLOCKED_CLAIM_PATTERNS = [
    "vous cherchez", "vous voulez acheter", "vous avez besoin de", "vous souhaitez",
    "vous êtes à la recherche", "votre intention", "vous envisagez d'acheter",
]


def safe_personalization_for_signal(signal):
    """Renvoie la phrase pré-approuvée pour ce signal, ou "" si son
    signal_type n'est pas répertorié — ne génère jamais de texte ad hoc."""
    template = SAFE_PHRASE_TEMPLATES.get(signal.signal_type)
    if not template:
        return ""
    return template.format(value=signal.value or signal.label)


def build_personalization_snippet(prospect, max_signals=2):
    """Jusqu'à `max_signals` phrases pré-approuvées, signal le plus récent
    d'abord. Renvoie une liste vide si aucun signal du prospect n'a de
    phrase répertoriée — jamais une phrase générique inventée à la place."""
    signals = sorted(
        prospect.signals.all(),
        key=lambda s: signal_age_days(s.observed_at or s.detected_at) if (s.observed_at or s.detected_at) else 9999,
    )
    phrases = []
    for signal in signals:
        phrase = safe_personalization_for_signal(signal)
        if phrase and phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) >= max_signals:
            break
    return phrases


def assert_no_overclaiming(text):
    """Renvoie la liste des formulations interdites trouvées dans `text`
    (vide = texte conforme). Utilisé dans les tests, et appelable avant tout
    envoi réel d'un message généré ailleurs dans l'application."""
    lowered = text.lower()
    return [pattern for pattern in BLOCKED_CLAIM_PATTERNS if pattern in lowered]
