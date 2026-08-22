"""Mission 7 — audit correctif round 2 avant merge (baseline af23d38) : 5
défauts résiduels trouvés par un audit indépendant après le premier round de
correctifs. Round de stabilisation : changement minimal nécessaire, aucun
effet domino sur les parcours voisins déjà validés."""
from unittest.mock import Mock, patch

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone

from prospects.models import (
    CompanySearchRun, ContactPerson, CrawlRun, PageAudit, ProductProfile,
    Prospect, ProspectSignal, PublicEmail, SearchCandidate,
)
from prospects.services import crawler, temporal_signals, url_safety
from prospects.services.acquisition_pipeline import _build_prospect_defaults, _finalize_candidate
from prospects.services.enrichment import CompanyWebsiteSource, EnrichmentEngine
from prospects.tasks import audit_site_task
from prospects.tests.factories import make_icp, make_prospect, make_product

PAGE_FIELDS = dict(
    depth=0, http_status=200, response_ms=120, content_type="text/html",
    title="Accueil", meta_description="desc", canonical="",
    h1_count=1, word_count=200, images_count=2, images_without_alt=0,
    internal_links=5, external_links=1, broken_links=0,
    has_https=True, has_viewport=True, has_contact_form=True,
    has_booking=False, has_phone=False, has_email=True, has_cta=False,
    issues=[],
)


def _crawl_result(url, technologies, found_people=None, found_temporal_events=None):
    page = {
        "url": url, "technologies": technologies,
        "found_emails": [], "found_phones": [], "found_contact_forms": [], "found_social_links": [],
        "found_people": found_people or [],
        "found_temporal_events": found_temporal_events or [],
        **PAGE_FIELDS,
    }
    return {"robots_allowed": True, "robots_url": "", "pages": [page]}


# ---------------------------------------------------------------------------
# §1 — audit_site_task ne doit pas crasher sur found_people/found_temporal_events
# ---------------------------------------------------------------------------

class AuditSiteTaskNewCrawlerFieldsTests(TestCase):
    @patch("prospects.tasks.run_pagespeed", return_value={"performance_score": None, "seo_score": None, "accessibility_score": None})
    @patch("prospects.tasks.crawl_site")
    def test_page_with_person_and_dated_job_posting_never_raises(self, mock_crawl, mock_pagespeed):
        prospect = make_prospect(website="https://agence-audit-r2.example")
        mock_crawl.return_value = _crawl_result(
            prospect.website, ["Google Analytics"],
            found_people=[{"full_name": "Julie Martin", "job_title": "CEO", "profile_url": "", "method": "json_ld_person"}],
            found_temporal_events=[{"field_name": "job_posting_growth", "event_date": "2026-08-20", "headline": "Growth Manager", "source_method": "json_ld_dated_content"}],
        )

        result = audit_site_task(prospect.pk)  # ne doit jamais lever de TypeError

        run = CrawlRun.objects.get(prospect=prospect)
        self.assertEqual(run.status, "done")
        self.assertEqual(PageAudit.objects.filter(crawl_run=run).count(), 1)
        self.assertIn("run_id", result)

    @patch("prospects.tasks.run_pagespeed", return_value={"performance_score": None, "seo_score": None, "accessibility_score": None})
    @patch("prospects.tasks.crawl_site")
    def test_site_change_collector_still_works_after_the_fix(self, mock_crawl, mock_pagespeed):
        """Non-régression explicitement demandée : le vrai audit_site_task
        doit toujours détecter un changement technologique après ce correctif."""
        prospect = make_prospect(website="https://agence-audit-r2-siteChange.example")

        mock_crawl.return_value = _crawl_result(prospect.website, ["Google Analytics"])
        audit_site_task(prospect.pk)
        self.assertFalse(ProspectSignal.objects.filter(prospect=prospect, signal_type__startswith="site_change_").exists())

        mock_crawl.return_value = _crawl_result(prospect.website, ["Google Analytics", "HubSpot"])
        audit_site_task(prospect.pk)

        signal = ProspectSignal.objects.get(prospect=prospect, signal_type="site_change_HubSpot")
        self.assertEqual(signal.signal_group, "intent")


# ---------------------------------------------------------------------------
# §2 — SSRF : redirections validées AVANT la requête, streaming plafonné
# ---------------------------------------------------------------------------

class FakeStreamResponse:
    def __init__(self, status_code, headers, chunks):
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks
        self.consumed_chunks = 0

    def iter_bytes(self):
        for chunk in self._chunks:
            self.consumed_chunks += 1
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeStreamClient:
    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    def stream(self, method, url, **kwargs):
        self.requested.append(url)
        if url not in self.responses:
            raise httpx.HTTPError(f"unexpected url {url}")
        return self.responses[url]


class SafeGetRedirectAndSizeTests(TestCase):
    def test_redirect_to_a_private_ip_is_never_fetched(self):
        public_url = "https://exemple.fr/page"
        client = FakeStreamClient({
            public_url: FakeStreamResponse(302, {"location": "http://127.0.0.1/secret"}, []),
        })
        with self.assertRaises(url_safety.UnsafeUrlError):
            url_safety.safe_get(client, public_url)
        self.assertNotIn("http://127.0.0.1/secret", client.requested)

    def test_redirect_to_a_public_url_is_followed(self):
        first = "https://exemple.fr/old-page"
        second = "https://exemple.fr/new-page"
        client = FakeStreamClient({
            first: FakeStreamResponse(301, {"location": second}, []),
            second: FakeStreamResponse(200, {"content-type": "text/html"}, [b"<html>ok</html>"]),
        })
        response = url_safety.safe_get(client, first)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.url, second)
        self.assertIn(second, client.requested)

    def test_too_many_redirects_raises(self):
        chain_urls = [f"https://exemple.fr/hop-{i}" for i in range(10)]
        responses = {}
        for i, url in enumerate(chain_urls[:-1]):
            responses[url] = FakeStreamResponse(302, {"location": chain_urls[i + 1]}, [])
        responses[chain_urls[-1]] = FakeStreamResponse(200, {"content-type": "text/html"}, [b"ok"])
        client = FakeStreamClient(responses)
        with self.assertRaises(url_safety.UnsafeUrlError):
            url_safety.safe_get(client, chain_urls[0], max_redirects=3)

    def test_streamed_body_stops_before_consuming_everything_past_the_limit(self):
        chunk = b"x" * (1024 * 1024)  # 1 Mo par morceau
        total_chunks = 10  # 10 Mo, largement > MAX_RESPONSE_BYTES (5 Mo)
        response = FakeStreamResponse(200, {"content-type": "text/html"}, [chunk] * total_chunks)
        client = FakeStreamClient({"https://exemple.fr/big": response})

        with self.assertRaises(url_safety.UnsafeUrlError):
            url_safety.safe_get(client, "https://exemple.fr/big", max_bytes=5 * 1024 * 1024)

        self.assertLess(response.consumed_chunks, total_chunks)

    def test_content_length_header_over_the_limit_is_rejected_before_reading_the_body(self):
        response = FakeStreamResponse(200, {"content-type": "text/html", "content-length": str(50 * 1024 * 1024)}, [b"x" * 1000])
        client = FakeStreamClient({"https://exemple.fr/huge": response})

        with self.assertRaises(url_safety.UnsafeUrlError):
            url_safety.safe_get(client, "https://exemple.fr/huge", max_bytes=5 * 1024 * 1024)

        self.assertEqual(response.consumed_chunks, 0)

    def test_never_calls_httpx_client_with_follow_redirects_true(self):
        """crawl_site()/quick_scan_site()/sitemap_urls() ne doivent plus
        jamais laisser httpx suivre une redirection lui-même."""
        source = open(crawler.__file__, encoding="utf-8").read()
        self.assertNotIn("follow_redirects=True", source)


# ---------------------------------------------------------------------------
# §3 — cooldown scopé à la source company_website ET au domaine
# ---------------------------------------------------------------------------

class CooldownSourceAndDomainTests(TestCase):
    @override_settings(WEB_ENRICHMENT_COOLDOWN_MINUTES=60)
    @patch("prospects.services.enrichment.crawl_site")
    def test_public_registry_only_run_never_blocks_company_website(self, mocked_crawl_site):
        mocked_crawl_site.return_value = {"pages": []}
        prospect = make_prospect(website="https://ex-entreprise.example")

        EnrichmentEngine(source_keys=["public_registry"]).enrich_prospect(prospect)
        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)

        self.assertEqual(mocked_crawl_site.call_count, 1)

    @override_settings(WEB_ENRICHMENT_COOLDOWN_MINUTES=60)
    @patch("prospects.services.enrichment.crawl_site")
    def test_website_change_between_two_crawls_allows_an_immediate_recrawl(self, mocked_crawl_site):
        mocked_crawl_site.return_value = {"pages": []}
        prospect = make_prospect(website="https://domaine-a.example")

        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        prospect.website = "https://domaine-b.example"
        prospect.save(update_fields=["website"])
        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)

        self.assertEqual(mocked_crawl_site.call_count, 2)

    @override_settings(WEB_ENRICHMENT_COOLDOWN_MINUTES=60)
    @patch("prospects.services.enrichment.crawl_site")
    def test_same_domain_within_the_window_is_not_recrawled(self, mocked_crawl_site):
        mocked_crawl_site.return_value = {"pages": []}
        prospect = make_prospect(website="https://ex-entreprise.example")

        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)

        self.assertEqual(mocked_crawl_site.call_count, 1)

    @override_settings(WEB_ENRICHMENT_COOLDOWN_MINUTES=60)
    @patch("prospects.services.enrichment.crawl_site")
    def test_force_refresh_always_recrawls(self, mocked_crawl_site):
        mocked_crawl_site.return_value = {"pages": []}
        prospect = make_prospect(website="https://ex-entreprise.example")

        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        EnrichmentEngine(source_keys=["company_website"], force_refresh=True).enrich_prospect(prospect)

        self.assertEqual(mocked_crawl_site.call_count, 2)


# ---------------------------------------------------------------------------
# §4 — Email Finder niveau A : une seule persistance canonique
# ---------------------------------------------------------------------------

class SingleCanonicalEmailPersistenceTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.search_run = CompanySearchRun.objects.create(mode="acquisition", product=self.product, icp=self.icp, status="running")

    def test_quick_scan_email_gets_the_same_semantics_as_enrichment(self):
        candidate = SearchCandidate.objects.create(
            search_run=self.search_run, siren="123456791", siret="12345679100013",
            name="Agence Quick Scan", site_url="https://agence-quickscan.example", site_confidence=80,
            quick_scan_data={
                "found_emails": ["julie.martin@agence-quickscan.example"],
                "email_sources": {"julie.martin@agence-quickscan.example": "https://agence-quickscan.example/equipe"},
                "found_phones": [], "found_social_links": [], "technologies_detailed": [],
            },
        )
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        prospect, _ = Prospect.objects.update_or_create(siren=candidate.siren, defaults=defaults)
        candidate.prospect = prospect
        candidate.save(update_fields=["prospect"])

        _finalize_candidate(candidate, candidate.quick_scan_data, [], prospect, self.icp, self.product)

        email = PublicEmail.objects.get(prospect=prospect, email="julie.martin@agence-quickscan.example")
        self.assertEqual(email.verification_status, "public_source_confirmed")
        self.assertEqual(email.source_url, "https://agence-quickscan.example/equipe")

    def test_generic_email_preference_for_primary_is_preserved(self):
        """Règle métier hors périmètre de ce correctif (préférer contact@ à un
        e-mail personnel comme e-mail principal) : doit rester inchangée."""
        candidate = SearchCandidate.objects.create(
            search_run=self.search_run, siren="123456792", siret="12345679200014",
            name="Agence Generic", site_url="https://agence-generic.example", site_confidence=80,
            quick_scan_data={
                "found_emails": ["julie.martin@agence-generic.example", "contact@agence-generic.example"],
                "email_sources": {}, "found_phones": [], "found_social_links": [], "technologies_detailed": [],
            },
        )
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        prospect, _ = Prospect.objects.update_or_create(siren=candidate.siren, defaults=defaults)
        candidate.prospect = prospect
        candidate.save(update_fields=["prospect"])

        _finalize_candidate(candidate, candidate.quick_scan_data, [], prospect, self.icp, self.product)

        prospect.refresh_from_db()
        self.assertEqual(prospect.public_email, "contact@agence-generic.example")
        self.assertTrue(PublicEmail.objects.get(prospect=prospect, email="contact@agence-generic.example").is_primary)


def _html_response(html, url):
    r = Mock(spec=httpx.Response)
    r.status_code = 200
    r.headers = {"content-type": "text/html; charset=utf-8"}
    r.text = html
    r.content = html.encode("utf-8")
    r.url = url
    r.history = []
    r.iter_bytes = lambda: iter([r.content])
    return r


class _StreamCtx:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *args):
        return False


class FakeClient:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, *args, **kwargs):
        if url in self.pages:
            return self.pages[url]
        raise httpx.HTTPError("not found")

    def head(self, url, *args, **kwargs):
        raise httpx.HTTPError("not found")

    def stream(self, method, url, **kwargs):
        if url in self.pages:
            return _StreamCtx(self.pages[url])
        raise httpx.HTTPError("not found")


def _no_robots_policy():
    return Mock(allowed=lambda u: True, crawl_delay=lambda: 0, robots_url="", available=False, sitemaps=lambda: None)


class EnrichmentPathAlsoGetsTheSameSemanticsTests(TestCase):
    """Vérifie l'autre porte d'entrée (Web Intelligence / enrichissement),
    pour prouver que les deux chemins produisent bien la même sémantique."""

    def test_company_website_source_email_is_also_public_source_confirmed(self):
        prospect = make_prospect(website="https://ex-entreprise.example")
        html = "<html><body>Contact : julie.martin@ex-entreprise.example</body></html>"
        client = FakeClient({"https://ex-entreprise.example": _html_response(html, "https://ex-entreprise.example")})
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)

        email = PublicEmail.objects.get(prospect=prospect, email="julie.martin@ex-entreprise.example")
        self.assertEqual(email.verification_status, "public_source_confirmed")


# ---------------------------------------------------------------------------
# §5 — NewsArticle : mot isolé insuffisant pour un faux Intent
# ---------------------------------------------------------------------------

class NewsArticleFalseIntentTests(TestCase):
    def test_generic_acquisition_marketing_headline_is_not_intent(self):
        fact = {"content_type": "newsarticle", "headline": "5 stratégies d'acquisition client en 2026"}
        self.assertIsNone(temporal_signals.classify_dated_fact(fact))

    def test_generic_investment_marketing_headline_is_not_intent(self):
        fact = {"content_type": "newsarticle", "headline": "Comment optimiser vos investissements marketing"}
        self.assertIsNone(temporal_signals.classify_dated_fact(fact))

    def test_generic_digital_acquisition_advice_is_not_intent(self):
        fact = {"content_type": "newsarticle", "headline": "Nos conseils pour l'acquisition digitale"}
        self.assertIsNone(temporal_signals.classify_dated_fact(fact))

    def test_funding_round_is_intent(self):
        fact = {"content_type": "newsarticle", "headline": "Levée de fonds de 5M€ pour accélérer la croissance"}
        self.assertEqual(temporal_signals.classify_dated_fact(fact), "news_acquisition")

    def test_company_acquired_is_intent(self):
        fact = {"content_type": "newsarticle", "headline": "Notre société a été rachetée par GrandGroupe"}
        self.assertEqual(temporal_signals.classify_dated_fact(fact), "news_acquisition")

    def test_merger_is_intent(self):
        fact = {"content_type": "newsarticle", "headline": "Nous fusionnons avec notre partenaire historique"}
        self.assertEqual(temporal_signals.classify_dated_fact(fact), "news_acquisition")

    def test_new_funding_received_is_intent(self):
        fact = {"content_type": "newsarticle", "headline": "Nouveau financement de 2M€ pour notre expansion"}
        self.assertEqual(temporal_signals.classify_dated_fact(fact), "news_acquisition")

    def test_strategic_partnership_announced_is_intent(self):
        fact = {"content_type": "newsarticle", "headline": "Annonce d'un partenariat stratégique avec Acme"}
        self.assertEqual(temporal_signals.classify_dated_fact(fact), "news_acquisition")
