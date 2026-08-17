"""ETAPE 17/18 (mission 2) — point de contrôle unique pour toute opposition.

Tous les chemins d'envoi (manuel, campagne, API, tâche Celery planifiée) doivent
appeler `is_suppressed()` juste avant l'envoi SMTP — jamais seulement au moment
où la campagne a été préparée.
"""
from ..models import Suppression


def normalize_email(email):
    return (email or "").strip().lower()


def is_suppressed(email, prospect=None):
    """True si l'envoi doit être bloqué : opposition sur l'adresse, sur le domaine
    (uniquement si un administrateur a explicitement bloqué tout le domaine),
    ou sur le prospect lui-même (prospecting_allowed=False / do_not_contact)."""
    normalized = normalize_email(email)

    if prospect is not None:
        if not prospect.prospecting_allowed:
            return True
        if prospect.status == "do_not_contact":
            return True
        if prospect.predictneed_stage == "do_not_contact":
            return True
        if Suppression.objects.filter(active=True, prospect=prospect).exists():
            return True

    if not normalized:
        return True  # pas d'adresse exploitable = pas d'envoi possible

    if Suppression.objects.filter(active=True, email__iexact=normalized).exists():
        return True

    domain = normalized.rsplit("@", 1)[-1] if "@" in normalized else ""
    if domain and Suppression.objects.filter(active=True, email="", domain__iexact=domain).exists():
        return True

    return False


def suppress(email="", prospect=None, reason="", domain=""):
    """Ajoute une opposition. `domain` ne doit être renseigné que sur décision
    explicite d'un administrateur de bloquer un domaine entier."""
    normalized = normalize_email(email)
    Suppression.objects.get_or_create(
        email=normalized, domain=domain,
        defaults={"prospect": prospect, "reason": reason, "active": True},
    )
