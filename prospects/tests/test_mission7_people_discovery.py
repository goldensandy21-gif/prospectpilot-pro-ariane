"""Mission 7C — People Discovery : extraction de personnes réelles sur les
pages publiques d'une entreprise (schema.org Person + heuristique de page
équipe), jamais un scraping LinkedIn, jamais un nom fabriqué. Alimente
ContactPerson (existant) via EnrichmentEngine — pas de nouveau modèle."""
from unittest.mock import Mock, patch

import httpx
from bs4 import BeautifulSoup
from django.test import TestCase

from prospects.models import ContactPerson, EnrichmentSource
from prospects.services import people_extraction
from prospects.services.enrichment import CompanyWebsiteSource, EnrichmentEngine, EvidenceCandidate
from prospects.tests.factories import make_prospect


def _soup(html):
    return BeautifulSoup(html, "lxml")


class LooksLikeTeamPageTests(TestCase):
    def test_equipe_path_is_recognized(self):
        self.assertTrue(people_extraction.looks_like_team_page("https://ex.fr/equipe"))

    def test_about_path_is_recognized(self):
        self.assertTrue(people_extraction.looks_like_team_page("https://ex.fr/a-propos"))

    def test_unrelated_path_is_not_recognized(self):
        self.assertFalse(people_extraction.looks_like_team_page("https://ex.fr/produits/widget"))


class HeuristicExtractionTests(TestCase):
    def test_pairs_a_name_with_an_adjacent_job_title(self):
        html = """
        <div><h3>Julie Martin</h3><p>Directrice Marketing</p></div>
        <div><h3>Marc Dupuis</h3><p>Responsable Growth</p></div>
        """
        people = people_extraction.extract_people_heuristic(_soup(html))
        names = {p["full_name"] for p in people}
        self.assertEqual(names, {"Julie Martin", "Marc Dupuis"})
        titles = {p["full_name"]: p["job_title"] for p in people}
        self.assertEqual(titles["Julie Martin"], "Directrice Marketing")

    def test_name_without_a_nearby_title_is_never_returned(self):
        html = "<div><h3>Julie Martin</h3><p>Nous sommes une agence passionnée.</p></div>"
        people = people_extraction.extract_people_heuristic(_soup(html))
        self.assertEqual(people, [])

    def test_role_only_text_without_a_name_produces_nothing(self):
        html = "<p>Notre équipe growth est composée de spécialistes.</p>"
        people = people_extraction.extract_people_heuristic(_soup(html))
        self.assertEqual(people, [])

    def test_lowercase_words_are_never_mistaken_for_a_name(self):
        html = "<div><h3>bienvenue chez nous</h3><p>Growth manager</p></div>"
        people = people_extraction.extract_people_heuristic(_soup(html))
        self.assertEqual(people, [])


class ExtractPeopleFromPageTests(TestCase):
    def test_json_ld_person_is_returned_regardless_of_url(self):
        html = '<script type="application/ld+json">{"@type":"Person","name":"Alex Roy","jobTitle":"CEO"}</script>'
        people = people_extraction.extract_people_from_page("https://ex.fr/blog/article", _soup(html))
        self.assertEqual([p["full_name"] for p in people], ["Alex Roy"])

    def test_heuristic_text_ignored_outside_a_team_page(self):
        html = "<div><h3>Julie Martin</h3><p>Directrice Marketing</p></div>"
        people = people_extraction.extract_people_from_page("https://ex.fr/blog/article", _soup(html))
        self.assertEqual(people, [])

    def test_heuristic_text_used_on_a_team_page(self):
        html = "<div><h3>Julie Martin</h3><p>Directrice Marketing</p></div>"
        people = people_extraction.extract_people_from_page("https://ex.fr/equipe", _soup(html))
        self.assertEqual([p["full_name"] for p in people], ["Julie Martin"])

    def test_deduplicates_a_person_found_by_both_methods(self):
        html = (
            '<script type="application/ld+json">{"@type":"Person","name":"Julie Martin","jobTitle":"CEO"}</script>'
            "<div><h3>Julie Martin</h3><p>Directrice Marketing</p></div>"
        )
        people = people_extraction.extract_people_from_page("https://ex.fr/equipe", _soup(html))
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["method"], "json_ld_person")


class StorePersonTests(TestCase):
    def setUp(self):
        self.prospect = make_prospect()
        self.source = EnrichmentSource.objects.create(key="company_website", name="Site officiel", source_type="company_website")
        self.engine = EnrichmentEngine(source_keys=["company_website"])

    def _candidate(self, name, confidence, status, job_title="", profile_url=""):
        return EvidenceCandidate(
            field_name="person", value=name, value_type="person",
            confidence_score=confidence, verification_status=status,
            source_key="company_website", source_url="https://ex.fr/equipe",
            raw_payload={"job_title": job_title, "profile_url": profile_url},
        )

    def test_creates_a_contact_person(self):
        self.engine.store_person(self.prospect, self.source, self._candidate("Julie Martin", 80, "public_source_confirmed", "CEO"))
        contact = ContactPerson.objects.get(prospect=self.prospect, full_name="Julie Martin")
        self.assertEqual(contact.job_title, "CEO")
        self.assertEqual(contact.verification_status, "public_source_confirmed")

    def test_deduplicates_entreprise_plus_personne_case_insensitive(self):
        self.engine.store_person(self.prospect, self.source, self._candidate("Julie Martin", 55, "format_valid", "CEO"))
        self.engine.store_person(self.prospect, self.source, self._candidate("julie martin", 80, "public_source_confirmed", "CEO"))
        self.assertEqual(ContactPerson.objects.filter(prospect=self.prospect).count(), 1)

    def test_higher_confidence_wins_and_lower_confidence_never_overwrites(self):
        self.engine.store_person(self.prospect, self.source, self._candidate("Julie Martin", 80, "public_source_confirmed", "CEO"))
        self.engine.store_person(self.prospect, self.source, self._candidate("Julie Martin", 55, "format_valid", "Autre poste"))
        contact = ContactPerson.objects.get(prospect=self.prospect, full_name="Julie Martin")
        self.assertEqual(contact.confidence_score, 80)
        self.assertEqual(contact.job_title, "CEO")

    def test_never_overwrites_a_known_job_title_with_a_blank_one(self):
        self.engine.store_person(self.prospect, self.source, self._candidate("Julie Martin", 55, "format_valid", "CEO"))
        self.engine.store_person(self.prospect, self.source, self._candidate("Julie Martin", 90, "public_source_confirmed", ""))
        contact = ContactPerson.objects.get(prospect=self.prospect, full_name="Julie Martin")
        self.assertEqual(contact.job_title, "CEO")

    def test_blank_name_is_never_stored(self):
        self.engine.store_person(self.prospect, self.source, self._candidate("", 80, "public_source_confirmed"))
        self.assertEqual(ContactPerson.objects.filter(prospect=self.prospect).count(), 0)

    def test_two_different_people_are_both_kept(self):
        self.engine.store_person(self.prospect, self.source, self._candidate("Julie Martin", 80, "public_source_confirmed"))
        self.engine.store_person(self.prospect, self.source, self._candidate("Marc Dupuis", 80, "public_source_confirmed"))
        self.assertEqual(ContactPerson.objects.filter(prospect=self.prospect).count(), 2)


def _html_response(html, url):
    r = Mock(spec=httpx.Response)
    r.status_code = 200
    r.headers = {"content-type": "text/html; charset=utf-8"}
    r.text = html
    r.url = url
    return r


class FakeCrawlClient:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, *args, **kwargs):
        if url in self.pages:
            return _html_response(self.pages[url], url)
        raise httpx.HTTPError("not found")

    def head(self, url, *args, **kwargs):
        raise httpx.HTTPError("not found")


class CompanyWebsiteSourceEndToEndTests(TestCase):
    """Vérifie que le pipeline d'enrichissement réel (celui déjà branché sur
    le bouton "Enrichir" et l'enrichissement en masse) fait remonter une
    vraie personne détectée sur la page équipe — sans deuxième crawl,
    sans intervention manuelle."""

    def test_enrich_prospect_creates_a_contact_person_from_the_team_page(self):
        prospect = make_prospect(website="https://ex-entreprise.example")
        team_html = """
        <html><body>
        <div><h3>Julie Martin</h3><p>Directrice Marketing</p></div>
        </body></html>
        """
        homepage_html = '<html><body><a href="/equipe">Notre équipe</a></body></html>'
        client = FakeCrawlClient({
            "https://ex-entreprise.example": homepage_html,
            "https://ex-entreprise.example/equipe": team_html,
        })
        fake_policy = Mock(allowed=lambda u: True, crawl_delay=lambda: 0, robots_url="", available=False)
        with patch("prospects.services.crawler.httpx.Client", return_value=client):
            with patch("prospects.services.crawler.RobotsPolicy.load", return_value=fake_policy):
                engine = EnrichmentEngine(source_keys=["company_website"])
                engine.enrich_prospect(prospect)

        contact = ContactPerson.objects.get(prospect=prospect, full_name="Julie Martin")
        self.assertEqual(contact.job_title, "Directrice Marketing")
        self.assertEqual(contact.source_url, "https://ex-entreprise.example/equipe")
