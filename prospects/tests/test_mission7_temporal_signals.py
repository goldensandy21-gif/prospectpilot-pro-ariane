"""Mission 7E — Temporal Signal Intelligence : seuls des faits datés réels
(structured data / meta de date sur le site propre du prospect) deviennent
des signaux INTENT. FIT != INTENT : jamais de date de collecte confondue
avec la date de l'évènement. France Travail reste dormant sans identifiants."""
from datetime import timedelta
from unittest.mock import Mock, patch

import httpx
from bs4 import BeautifulSoup
from django.test import TestCase, override_settings
from django.utils import timezone

from prospects.models import ProspectEvidence
from prospects.services import crawler, france_travail, temporal_signals
from prospects.services.enrichment import CompanyWebsiteSource, EnrichmentEngine
from prospects.services.signal_collectors import DEFAULT_COLLECTORS, RecentActivitySignalCollector
from prospects.tests.factories import make_prospect


class ClassifyDatedFactTests(TestCase):
    def test_job_posting_with_growth_keyword_is_job_posting_growth(self):
        fact = {"content_type": "jobposting", "headline": "Recherche Growth Manager"}
        self.assertEqual(temporal_signals.classify_dated_fact(fact), "job_posting_growth")

    def test_job_posting_without_a_relevant_keyword_is_ignored(self):
        fact = {"content_type": "jobposting", "headline": "Recherche standardiste"}
        self.assertIsNone(temporal_signals.classify_dated_fact(fact))

    def test_news_article_with_acquisition_keyword_is_news_acquisition(self):
        fact = {"content_type": "newsarticle", "headline": "Levée de fonds de 5M€"}
        self.assertEqual(temporal_signals.classify_dated_fact(fact), "news_acquisition")

    def test_blog_post_with_growth_keyword_is_dated_content_published(self):
        fact = {"content_type": "blogposting", "headline": "Notre nouvelle stratégie growth"}
        self.assertEqual(temporal_signals.classify_dated_fact(fact), "dated_content_published")

    def test_unrelated_blog_post_produces_nothing(self):
        fact = {"content_type": "blogposting", "headline": "Nos vacances d'été"}
        self.assertIsNone(temporal_signals.classify_dated_fact(fact))


class ExtractTemporalEventsTests(TestCase):
    def _soup(self, html):
        return BeautifulSoup(html, "lxml")

    def test_json_ld_dated_job_posting_is_extracted(self):
        html = """<script type="application/ld+json">
        {"@type": "JobPosting", "title": "Growth Manager", "datePublished": "2026-08-20"}
        </script>"""
        events = crawler._extract_temporal_events(
            "https://ex.fr/carrieres/growth-manager", self._soup(html), "growth manager",
            crawler.extract_json_ld_blocks(self._soup(html)),
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["field_name"], "job_posting_growth")
        self.assertEqual(events[0]["event_date"], "2026-08-20")
        self.assertEqual(events[0]["source_method"], "json_ld_dated_content")

    def test_meta_fallback_on_a_career_page_with_growth_keyword(self):
        html = '<meta property="article:published_time" content="2026-08-18T10:00:00Z">'
        soup = self._soup(html)
        events = crawler._extract_temporal_events(
            "https://ex.fr/carrieres/growth", soup, "on recrute un growth manager", [],
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "2026-08-18")
        self.assertEqual(events[0]["source_method"], "meta_published_time")

    def test_meta_fallback_on_an_unrelated_page_produces_nothing(self):
        html = '<meta property="article:published_time" content="2026-08-18T10:00:00Z">'
        soup = self._soup(html)
        events = crawler._extract_temporal_events("https://ex.fr/produits/widget", soup, "un widget bleu", [])
        self.assertEqual(events, [])

    def test_no_date_at_all_produces_no_event(self):
        html = "<html><body>On recrute un growth manager, sans date.</body></html>"
        soup = self._soup(html)
        events = crawler._extract_temporal_events("https://ex.fr/carrieres/growth", soup, "on recrute un growth manager", [])
        self.assertEqual(events, [])


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


class EndToEndTemporalSignalTests(TestCase):
    """Reproduit le pattern A/B/C de mission 6 : sans date réelle -> pas
    d'Intent ; avec une date réelle explicite -> Intent."""

    def _run_enrichment(self, homepage_html, career_html):
        prospect = make_prospect(website="https://ex-entreprise.example")
        client = FakeCrawlClient({
            "https://ex-entreprise.example": homepage_html,
            "https://ex-entreprise.example/carrieres": career_html,
        })
        fake_policy = Mock(allowed=lambda u: True, crawl_delay=lambda: 0, robots_url="", available=False)
        with patch("prospects.services.crawler.httpx.Client", return_value=client):
            with patch("prospects.services.crawler.RobotsPolicy.load", return_value=fake_policy):
                engine = EnrichmentEngine(source_keys=["company_website"])
                engine.enrich_prospect(prospect)
        return prospect

    def test_career_page_without_a_date_produces_no_intent_signal(self):
        homepage = '<html><body><a href="/carrieres">Carrières</a></body></html>'
        career = "<html><body>On recrute un Growth Manager, sans date.</body></html>"
        prospect = self._run_enrichment(homepage, career)

        self.assertFalse(prospect.evidence_items.filter(field_name="job_posting_growth").exists())
        signals = RecentActivitySignalCollector().collect(prospect)
        self.assertEqual(signals, [])

    def test_career_page_with_a_real_date_produces_an_intent_signal(self):
        homepage = '<html><body><a href="/carrieres">Carrières</a></body></html>'
        career = """<html><body>
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "Growth Manager", "datePublished": "2026-08-20"}
        </script>
        </body></html>"""
        prospect = self._run_enrichment(homepage, career)

        evidence = ProspectEvidence.objects.get(prospect=prospect, field_name="job_posting_growth")
        self.assertEqual(evidence.raw_payload["event_date"], "2026-08-20")

        signals = RecentActivitySignalCollector().collect(prospect)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal_group, "intent")


class RecentActivityCollectorIsActiveTests(TestCase):
    def test_collector_is_now_in_default_collectors(self):
        self.assertTrue(any(isinstance(c, RecentActivitySignalCollector) for c in DEFAULT_COLLECTORS))


@override_settings(FRANCE_TRAVAIL_CLIENT_ID="", FRANCE_TRAVAIL_CLIENT_SECRET="")
class FranceTravailDormantTests(TestCase):
    def test_is_not_configured_without_credentials(self):
        self.assertFalse(france_travail.is_configured())

    def test_search_never_makes_a_network_call_when_unconfigured(self):
        with patch("prospects.services.france_travail.httpx.Client") as mocked_client:
            result = france_travail.search_recent_offers("Agence Exemple")
        self.assertEqual(result, [])
        mocked_client.assert_not_called()

    def test_source_collect_returns_empty_without_credentials(self):
        prospect = make_prospect()
        self.assertEqual(france_travail.FranceTravailSource().collect(prospect), [])


@override_settings(FRANCE_TRAVAIL_CLIENT_ID="test-id", FRANCE_TRAVAIL_CLIENT_SECRET="test-secret")
class FranceTravailOffersFilterTests(TestCase):
    def test_offers_without_a_relevant_keyword_are_filtered_out(self):
        offers = [{"intitule": "Standardiste", "dateCreation": "2026-08-20T00:00:00"}]
        self.assertEqual(france_travail.offers_to_evidence_candidates(offers), [])

    def test_offers_without_a_date_are_filtered_out(self):
        offers = [{"intitule": "Growth Manager"}]
        self.assertEqual(france_travail.offers_to_evidence_candidates(offers), [])

    def test_relevant_dated_offer_is_kept(self):
        offers = [{
            "intitule": "Growth Manager H/F", "dateCreation": "2026-08-20T00:00:00",
            "origineOffre": {"urlOrigine": "https://francetravail.fr/offre/123"},
        }]
        candidates = france_travail.offers_to_evidence_candidates(offers)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["event_date"], "2026-08-20")
        self.assertEqual(candidates[0]["field_name"], "job_posting_growth")
