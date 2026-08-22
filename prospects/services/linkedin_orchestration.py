"""Mission 6, section 10 — Orchestration LinkedIn.

Persiste tout dans ContactLog(channel="linkedin") — aucun nouveau modèle de
contact ni de séquence. Le profil LinkedIn utilisé vient de
ContactPerson.profile_url (décideur) ou, à défaut, de
PublicSocialLink(platform="linkedin") (entreprise) — mission 6, section 9.
"""
from ..models import ContactLog
from .linkedin_provider import get_default_provider

STATUS_TO_OUTCOME = {
    "prepared": {"invitation": "invitation_prepared", "message": "message_prepared"},
    "sent": {"invitation": "invitation_sent", "message": "sent"},
    "failed": {"invitation": "invitation_failed", "message": "message_failed"},
}
# Correctif d'audit (round 2) : un statut provider inconnu/inattendu doit
# être traité en échec (fail-safe), jamais silencieusement en "préparé" —
# qui laisserait croire à tort qu'une action a réussi.
_UNKNOWN_STATUS_OUTCOME = {"invitation": "invitation_failed", "message": "message_failed"}


def linkedin_profile_url(prospect):
    person = prospect.contact_people.filter(is_active=True).exclude(profile_url="").order_by("-confidence_score").first()
    if person:
        return person.profile_url
    link = prospect.social_links.filter(platform="linkedin", is_active=True).first()
    return link.url if link else ""


def send_invitation(prospect, provider=None, note=""):
    """Prépare puis (selon le provider) envoie une invitation LinkedIn.
    Renvoie le ContactLog créé. Ne lève jamais pour l'absence de profil —
    renvoie None dans ce cas, à l'appelant de vérifier."""
    profile_url = linkedin_profile_url(prospect)
    if not profile_url:
        return None

    provider = provider or get_default_provider()
    result = provider.send_invitation(profile_url, note=note)
    outcome = STATUS_TO_OUTCOME.get(result["status"], _UNKNOWN_STATUS_OUTCOME)["invitation"]

    return ContactLog.objects.create(
        prospect=prospect, channel="linkedin", subject="Invitation LinkedIn",
        message=note, outcome=outcome,
        metadata={"provider": provider.name, "profile_url": profile_url, "detail": result["detail"]},
    )


def send_message(prospect, message, provider=None):
    profile_url = linkedin_profile_url(prospect)
    if not profile_url:
        return None

    provider = provider or get_default_provider()
    result = provider.send_message(profile_url, message)
    outcome = STATUS_TO_OUTCOME.get(result["status"], _UNKNOWN_STATUS_OUTCOME)["message"]

    return ContactLog.objects.create(
        prospect=prospect, channel="linkedin", subject="Message LinkedIn",
        message=message, outcome=outcome,
        metadata={"provider": provider.name, "profile_url": profile_url, "detail": result["detail"]},
    )


def record_invitation_accepted(contact_log):
    """À appeler quand une personne confirme manuellement (ou qu'une future
    intégration API autorisée le signale) qu'une invitation a été acceptée —
    jamais déduit automatiquement d'une simple absence de réponse."""
    contact_log.outcome = "invitation_accepted"
    contact_log.save(update_fields=["outcome", "updated_at"])
    return contact_log


def record_invitation_declined(contact_log):
    contact_log.outcome = "invitation_declined"
    contact_log.save(update_fields=["outcome", "updated_at"])
    return contact_log


def record_reply(contact_log, response_text=""):
    contact_log.outcome = "replied"
    contact_log.response_text = response_text
    contact_log.save(update_fields=["outcome", "response_text", "updated_at"])
    return contact_log
