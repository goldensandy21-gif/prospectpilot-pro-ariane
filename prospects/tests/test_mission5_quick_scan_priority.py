"""Mission 5, section 2 — le quick scan doit visiter en priorité les pages où
l'on trouve réellement des coordonnées (contact, mentions légales, à-propos,
équipe) avant les pages commerciales, et utiliser les vrais liens internes de
la page d'accueil quand ils existent plutôt que deviner des URLs au hasard."""
from unittest.mock import Mock, patch

import httpx
from django.test import TestCase, override_settings

from prospects.services import quick_scan


def _html_response(html, url):
    r = Mock(spec=httpx.Response)
    r.status_code = 200
    r.text = html
    r.headers = {"content-type": "text/html; charset=utf-8"}
    r.url = url
    return r


class FakeClient:
    """Simule httpx.Client : sert des pages HTML pré-enregistrées par URL."""

    def __init__(self, pages):
        self.pages = pages
        self.requested_urls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, *args, **kwargs):
        self.requested_urls.append(url)
        if url in self.pages:
            return _html_response(self.pages[url], url)
        raise httpx.HTTPError("not found")


HOMEPAGE_WITH_REAL_LINKS = """
<html><body>
<a href="/notre-equipe">Notre équipe</a>
<a href="/services">Nos services</a>
<a href="/offres">Nos offres</a>
Bienvenue sur notre site.
</body></html>
"""

TEAM_PAGE = """
<html><body>
Contactez Marie Dupont : <a href="mailto:marie@agence-test.example">marie@agence-test.example</a>
</body></html>
"""

HOMEPAGE_NO_LINKS = "<html><body>Bienvenue, aucune coordonnée ici.</body></html>"
SERVICES_PAGE = "<html><body>Nos services professionnels.</body></html>"
CONTACT_PAGE = """
<html><body>Ecrivez-nous : contact@agence-test.example</body></html>
"""


@override_settings(ACQUISITION_QUICK_SCAN_PAGES=5)
class QuickScanRealLinksPriorityTests(TestCase):
    def test_uses_real_internal_link_to_team_page_even_if_not_guessed(self):
        """'/notre-equipe' n'est dans aucune liste devinée : seul le vrai lien
        trouvé sur la page d'accueil permet de l'atteindre."""
        client = FakeClient({
            "https://agence-test.example": HOMEPAGE_WITH_REAL_LINKS,
            "https://agence-test.example/notre-equipe": TEAM_PAGE,
            "https://agence-test.example/services": SERVICES_PAGE,
        })
        with patch.object(quick_scan.httpx, "Client", return_value=client):
            result = quick_scan.quick_scan_site("https://agence-test.example", max_pages=2)

        self.assertIn("https://agence-test.example/notre-equipe", client.requested_urls)
        self.assertIn("marie@agence-test.example", result["found_emails"])
        self.assertEqual(
            result["email_sources"]["marie@agence-test.example"],
            "https://agence-test.example/notre-equipe",
        )

    def test_real_links_are_requested_before_guessed_commercial_urls(self):
        client = FakeClient({
            "https://agence-test.example": HOMEPAGE_WITH_REAL_LINKS,
            "https://agence-test.example/notre-equipe": TEAM_PAGE,
        })
        with patch.object(quick_scan.httpx, "Client", return_value=client):
            quick_scan.quick_scan_site("https://agence-test.example", max_pages=2)

        # Ordre parmi les pages réellement scannées (hors robots.txt, capturé
        # par le même client mocké) : homepage, puis le vrai lien /notre-equipe
        # — jamais /services deviné en premier alors qu'un vrai lien existe.
        pages = [u for u in client.requested_urls if not u.endswith("/robots.txt")]
        self.assertEqual(pages[0], "https://agence-test.example")
        self.assertEqual(pages[1], "https://agence-test.example/notre-equipe")
        self.assertNotIn("https://agence-test.example/services", pages)


@override_settings(ACQUISITION_QUICK_SCAN_PAGES=3)
class QuickScanGuessedPriorityOrderTests(TestCase):
    def test_guessed_contact_page_reached_before_commercial_pages_with_small_budget(self):
        """Avant le correctif : avec ACQUISITION_QUICK_SCAN_PAGES=5 et l'ancien
        ordre (services/offres/offre/produits en premier), /contact n'était
        jamais atteint. Ici, budget=3 : homepage + 2 pages devinées doivent
        privilégier /contact avant toute page commerciale."""
        client = FakeClient({
            "https://agence-test.example": HOMEPAGE_NO_LINKS,
            "https://agence-test.example/contact": CONTACT_PAGE,
            "https://agence-test.example/services": SERVICES_PAGE,
        })
        with patch.object(quick_scan.httpx, "Client", return_value=client):
            result = quick_scan.quick_scan_site("https://agence-test.example", max_pages=3)

        self.assertIn("https://agence-test.example/contact", client.requested_urls)
        self.assertIn("contact@agence-test.example", result["found_emails"])
        # /services (page commerciale) ne doit pas avoir pris la place de /contact
        # dans le petit budget de pages restant.
        contact_index = client.requested_urls.index("https://agence-test.example/contact")
        self.assertNotIn("https://agence-test.example/services", client.requested_urls[:contact_index])
