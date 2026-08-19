"""Mission 6, section 10 — Fournisseurs LinkedIn.

Abstraction volontairement minimale : ProspectPilot ne doit jamais être
verrouillé sur un seul fournisseur, et ne doit JAMAIS simuler des clics
humains sur linkedin.com (pas de Selenium/Playwright, pas de contournement
des restrictions de la plateforme). Deux implémentations concrètes pour
l'instant :

- ManualLinkedInProvider (par défaut) : ne fait AUCUN appel réseau. Prépare
  le contenu (invitation/message) et le consigne comme "préparé", à envoyer
  manuellement par une personne depuis son propre compte LinkedIn. Ne
  prétend jamais avoir envoyé quoi que ce soit.
- MockLinkedInProvider : pour les tests uniquement, simule un cycle complet
  (préparé -> envoyé -> accepté) de façon déterministe.

Un futur provider API autorisé (ex. LinkedIn Marketing/Sales Nav officiel,
si un jour souscrit et configuré) s'ajouterait comme une troisième classe
implémentant la même interface, sans toucher à l'orchestrateur.
"""
from abc import ABC, abstractmethod


class LinkedInProvider(ABC):
    name = "abstract"

    @abstractmethod
    def send_invitation(self, profile_url, note=""):
        """Renvoie {status, detail} où status in
        {"prepared", "sent", "failed"}. Ne doit jamais lever pour un simple
        refus/limite : renvoyer status="failed" avec un detail explicite."""
        raise NotImplementedError

    @abstractmethod
    def send_message(self, profile_url, message):
        raise NotImplementedError


class ManualLinkedInProvider(LinkedInProvider):
    """Mode par défaut, toujours disponible, sans aucune dépendance externe.
    Ne simule aucune action réelle sur LinkedIn : prépare uniquement le
    contenu pour un envoi manuel par une personne."""
    name = "manual"

    def send_invitation(self, profile_url, note=""):
        return {
            "status": "prepared",
            "detail": "Invitation préparée : à envoyer manuellement depuis un compte LinkedIn autorisé.",
        }

    def send_message(self, profile_url, message):
        return {
            "status": "prepared",
            "detail": "Message préparé : à envoyer manuellement depuis un compte LinkedIn autorisé.",
        }


class MockLinkedInProvider(LinkedInProvider):
    """Réservé aux tests automatisés — simule un envoi réussi immédiat."""
    name = "mock"

    def send_invitation(self, profile_url, note=""):
        return {"status": "sent", "detail": "Invitation envoyée (mock)."}

    def send_message(self, profile_url, message):
        return {"status": "sent", "detail": "Message envoyé (mock)."}


def get_default_provider():
    """Provider par défaut de l'application : toujours `manual` tant
    qu'aucune intégration API autorisée n'est configurée. Ne jamais faire de
    ce défaut un provider qui simule un envoi réel."""
    return ManualLinkedInProvider()
