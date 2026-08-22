"""Mission 7 — audit correctif avant merge (baseline 72d95f5) : 10 défauts de
câblage trouvés par un audit indépendant, non couverts par les 474 tests
existants. Chaque classe ci-dessous correspond à un point numéroté de
l'audit."""
from unittest.mock import Mock, patch

import httpx
from bs4 import BeautifulSoup
from django.test import TestCase, override_settings
from django.utils import timezone

from prospects.models import (
    Campaign, CompanySearchRun, ContactPerson, ProductProfile, Prospect,
    ProspectEvidence, ProspectSignal, PublicEmail, SearchCandidate,
)
from prospects.services import crawler, structured_data, url_safety
from prospects.services.acquisition_pipeline import _build_prospect_defaults, _finalize_candidate
from prospects.services.email_intelligence import infer_domain_email_pattern
from prospects.services.enrichment import CompanyWebsiteSource, EnrichmentEngine
from prospects.services.linkedin_orchestration import linkedin_profile_url
from prospects.services.signal_collectors import RecentActivitySignalCollector
from prospects.tests.factories import make_icp, make_prospect, make_product


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


def _xml_response(content, url):
    r = Mock(spec=httpx.Response)
    r.status_code = 200
    r.headers = {"content-type": "application/xml"}
    r.content = content.encode("utf-8")
    r.text = content
    r.url = url
    r.history = []
    r.iter_bytes = lambda: iter([r.content])
    return r


class _StreamCtx:
    """Audit correctif round 2, §2 — safe_get() lit via client.stream()."""

    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *args):
        return False


class FakeClient:
    """Sert des pages HTML/XML pré-enregistrées par URL — jamais de requête réseau réelle."""

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
            return self.pages[url]
        raise httpx.HTTPError("not found")

    def head(self, url, *args, **kwargs):
        raise httpx.HTTPError("not found")

    def stream(self, method, url, **kwargs):
        self.requested_urls.append(url)
        if url in self.pages:
            return _StreamCtx(self.pages[url])
        raise httpx.HTTPError("not found")


def _no_robots_policy():
    return Mock(allowed=lambda u: True, crawl_delay=lambda: 0, robots_url="", available=False,
                sitemaps=lambda: None)


# ---------------------------------------------------------------------------
# §1 — sitemap réellement branché dans le crawl
# ---------------------------------------------------------------------------

TEAM_PAGE_UNLINKED = """
<html><body><h3>Julie Martin</h3><p>Directrice Marketing</p></body></html>
"""
HOMEPAGE_NO_TEAM_LINK = "<html><body>Bienvenue chez nous, sans lien vers l'équipe.</body></html>"
SITEMAP_WITH_TEAM = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ex-entreprise.example/equipe</loc><lastmod>2020-01-01</lastmod></url>
</urlset>"""


class SitemapWiredIntoCrawlTests(TestCase):
    def test_sitemap_only_page_is_actually_fetched_by_crawl_site(self):
        client = FakeClient({
            "https://ex-entreprise.example": _html_response(HOMEPAGE_NO_TEAM_LINK, "https://ex-entreprise.example"),
            "https://ex-entreprise.example/sitemap.xml": _xml_response(SITEMAP_WITH_TEAM, "https://ex-entreprise.example/sitemap.xml"),
            "https://ex-entreprise.example/equipe": _html_response(TEAM_PAGE_UNLINKED, "https://ex-entreprise.example/equipe"),
        })
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                data = crawler.crawl_site("https://ex-entreprise.example", max_pages=6, check_broken_links=False)

        fetched_urls = {p["url"] for p in data["pages"]}
        self.assertIn("https://ex-entreprise.example/equipe", fetched_urls)
        team_page = next(p for p in data["pages"] if p["url"].endswith("/equipe"))
        self.assertEqual([person["full_name"] for person in team_page["found_people"]], ["Julie Martin"])

    def test_max_pages_budget_is_still_respected_with_sitemap_urls(self):
        client = FakeClient({
            "https://ex-entreprise.example": _html_response(HOMEPAGE_NO_TEAM_LINK, "https://ex-entreprise.example"),
            "https://ex-entreprise.example/sitemap.xml": _xml_response(SITEMAP_WITH_TEAM, "https://ex-entreprise.example/sitemap.xml"),
            "https://ex-entreprise.example/equipe": _html_response(TEAM_PAGE_UNLINKED, "https://ex-entreprise.example/equipe"),
        })
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                data = crawler.crawl_site("https://ex-entreprise.example", max_pages=1, check_broken_links=False)
        self.assertLessEqual(len(data["pages"]), 1)

    def test_lastmod_is_never_used_as_an_event_date(self):
        """lastmod=2020-01-01 (vieux) ne doit jamais devenir un ProspectEvidence
        daté — _extract_temporal_events ignore totalement le sitemap."""
        client = FakeClient({
            "https://ex-entreprise.example": _html_response(HOMEPAGE_NO_TEAM_LINK, "https://ex-entreprise.example"),
            "https://ex-entreprise.example/sitemap.xml": _xml_response(SITEMAP_WITH_TEAM, "https://ex-entreprise.example/sitemap.xml"),
            "https://ex-entreprise.example/equipe": _html_response(TEAM_PAGE_UNLINKED, "https://ex-entreprise.example/equipe"),
        })
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                data = crawler.crawl_site("https://ex-entreprise.example", max_pages=6, check_broken_links=False)
        team_page = next(p for p in data["pages"] if p["url"].endswith("/equipe"))
        self.assertEqual(team_page["found_temporal_events"], [])

    def test_end_to_end_enrichment_finds_the_unlinked_team_page_person(self):
        prospect = make_prospect(website="https://ex-entreprise.example")
        client = FakeClient({
            "https://ex-entreprise.example": _html_response(HOMEPAGE_NO_TEAM_LINK, "https://ex-entreprise.example"),
            "https://ex-entreprise.example/sitemap.xml": _xml_response(SITEMAP_WITH_TEAM, "https://ex-entreprise.example/sitemap.xml"),
            "https://ex-entreprise.example/equipe": _html_response(TEAM_PAGE_UNLINKED, "https://ex-entreprise.example/equipe"),
        })
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)

        self.assertTrue(ContactPerson.objects.filter(prospect=prospect, full_name="Julie Martin").exists())


# ---------------------------------------------------------------------------
# §2 — enrichissement -> signal -> score, sans appel manuel
# ---------------------------------------------------------------------------

DATED_JOB_PAGE = """<html><body>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Growth Manager", "datePublished": "2026-08-20"}
</script>
</body></html>"""
UNDATED_JOB_PAGE = "<html><body>On recrute un Growth Manager, sans date.</body></html>"


class EnrichmentSignalScoreWiringTests(TestCase):
    def _run(self, career_html):
        prospect = make_prospect(website="https://ex-entreprise.example")
        homepage = '<html><body><a href="/carrieres">Carrières</a></body></html>'
        client = FakeClient({
            "https://ex-entreprise.example": _html_response(homepage, "https://ex-entreprise.example"),
            "https://ex-entreprise.example/carrieres": _html_response(career_html, "https://ex-entreprise.example/carrieres"),
        })
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        prospect.refresh_from_db()
        return prospect

    def test_dated_job_posting_produces_a_persisted_intent_signal_without_manual_call(self):
        prospect = self._run(DATED_JOB_PAGE)
        self.assertTrue(
            ProspectSignal.objects.filter(prospect=prospect, signal_group="intent", signal_type="activity_job_posting_growth").exists()
        )
        self.assertGreater(prospect.intent_score, 0)
        self.assertGreater(prospect.predictneed_acquisition_score, 0)

    def test_same_page_without_a_date_produces_no_intent_signal(self):
        prospect = self._run(UNDATED_JOB_PAGE)
        self.assertFalse(ProspectSignal.objects.filter(prospect=prospect, signal_group="intent").exists())
        self.assertEqual(prospect.intent_score, 0)


# ---------------------------------------------------------------------------
# §3 — people dans le pipeline d'acquisition (Recherche intelligente)
# ---------------------------------------------------------------------------

class AcquisitionPipelinePeopleTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.search_run = CompanySearchRun.objects.create(mode="acquisition", product=self.product, icp=self.icp, status="running")

    def test_finalize_candidate_persists_found_people_via_contact_person(self):
        candidate = SearchCandidate.objects.create(
            search_run=self.search_run, siren="123456789", siret="12345678900011",
            name="Agence Test", site_url="https://agence-test.example", site_confidence=80,
            quick_scan_data={
                "found_emails": [], "found_phones": [], "found_social_links": [],
                "technologies_detailed": [],
                "found_people": [{
                    "full_name": "Marc Dupuis", "job_title": "CEO", "profile_url": "",
                    "method": "json_ld_person", "source_url": "https://agence-test.example/equipe",
                }],
            },
        )
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        prospect, _ = Prospect.objects.update_or_create(siren=candidate.siren, defaults=defaults)
        candidate.prospect = prospect
        candidate.save(update_fields=["prospect"])

        _finalize_candidate(candidate, candidate.quick_scan_data, [], prospect, self.icp, self.product)

        contact = ContactPerson.objects.get(prospect=prospect, full_name="Marc Dupuis")
        self.assertEqual(contact.job_title, "CEO")
        self.assertEqual(contact.source_url, "https://agence-test.example/equipe")

    def test_no_found_people_never_crashes(self):
        candidate = SearchCandidate.objects.create(
            search_run=self.search_run, siren="123456790", siret="12345679000012",
            name="Agence Sans Equipe", site_url="https://agence-sans-equipe.example", site_confidence=80,
            quick_scan_data={"found_emails": [], "found_phones": [], "found_social_links": [], "technologies_detailed": []},
        )
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        prospect, _ = Prospect.objects.update_or_create(siren=candidate.siren, defaults=defaults)
        candidate.prospect = prospect
        candidate.save(update_fields=["prospect"])
        _finalize_candidate(candidate, candidate.quick_scan_data, [], prospect, self.icp, self.product)
        self.assertEqual(ContactPerson.objects.filter(prospect=prospect).count(), 0)


# ---------------------------------------------------------------------------
# §4 — Email Finder niveau A réellement utilisé par store_email()
# ---------------------------------------------------------------------------

PROFESSIONAL_EMAIL_PAGE = "<html><body>Contact : julie.martin@ex-entreprise.example</body></html>"
FREE_DOMAIN_EMAIL_PAGE = "<html><body>Contact : julie.martin@gmail.com</body></html>"


class EmailFinderLevelAWiringTests(TestCase):
    def _run(self, page_html):
        prospect = make_prospect(website="https://ex-entreprise.example")
        client = FakeClient({
            "https://ex-entreprise.example": _html_response(page_html, "https://ex-entreprise.example"),
        })
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        return prospect

    def test_professional_email_found_on_page_is_public_source_confirmed(self):
        prospect = self._run(PROFESSIONAL_EMAIL_PAGE)
        email = PublicEmail.objects.get(prospect=prospect, email="julie.martin@ex-entreprise.example")
        self.assertEqual(email.verification_status, "public_source_confirmed")
        self.assertEqual(email.source_url, "https://ex-entreprise.example")

    def test_free_domain_email_keeps_deliverability_unknown(self):
        prospect = self._run(FREE_DOMAIN_EMAIL_PAGE)
        email = PublicEmail.objects.get(prospect=prospect, email="julie.martin@gmail.com")
        self.assertEqual(email.verification_status, "deliverability_unknown")

    def test_public_source_confirmed_is_never_used_for_inferred_emails(self):
        from prospects.services.email_intelligence import propose_inferred_email
        prospect = make_prospect(website="https://ex-entreprise.example")
        ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", email="julie.martin@ex-entreprise.example", is_active=True)
        ContactPerson.objects.create(prospect=prospect, full_name="Marc Dupuis", email="marc.dupuis@ex-entreprise.example", is_active=True)
        target = ContactPerson.objects.create(prospect=prospect, full_name="Nina Roy", is_active=True)
        evidence = propose_inferred_email(prospect, target)
        self.assertEqual(evidence.verification_status, "pattern_inferred")
        self.assertNotEqual(evidence.verification_status, "public_source_confirmed")


# ---------------------------------------------------------------------------
# §5 — profile_url réservé à de vraies URLs LinkedIn
# ---------------------------------------------------------------------------

class LinkedInProfileUrlHostnameTests(TestCase):
    def _blocks(self, html):
        soup = BeautifulSoup(html, "lxml")
        return structured_data.extract_json_ld_blocks(soup)

    def test_person_url_pointing_to_company_site_is_not_treated_as_linkedin(self):
        html = """<script type="application/ld+json">
        {"@type": "Person", "name": "Julie Martin", "url": "https://ex-entreprise.example/equipe/julie"}
        </script>"""
        persons = structured_data.find_persons(self._blocks(html))
        self.assertEqual(persons[0]["profile_url"], "")
        self.assertEqual(persons[0]["bio_url"], "https://ex-entreprise.example/equipe/julie")

    def test_sameas_list_with_linkedin_among_others_picks_linkedin(self):
        html = """<script type="application/ld+json">
        {"@type": "Person", "name": "Julie Martin",
         "sameAs": ["https://twitter.com/juliemartin", "https://linkedin.com/in/julie-martin"]}
        </script>"""
        persons = structured_data.find_persons(self._blocks(html))
        self.assertEqual(persons[0]["profile_url"], "https://linkedin.com/in/julie-martin")

    def test_no_linkedin_anywhere_leaves_profile_url_empty(self):
        html = """<script type="application/ld+json">
        {"@type": "Person", "name": "Julie Martin", "sameAs": ["https://twitter.com/juliemartin"]}
        </script>"""
        persons = structured_data.find_persons(self._blocks(html))
        self.assertEqual(persons[0]["profile_url"], "")

    def test_linkedin_profile_url_helper_is_empty_when_no_real_linkedin_found(self):
        prospect = make_prospect()
        ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", profile_url="", is_active=True)
        self.assertEqual(linkedin_profile_url(prospect), "")

    def test_linkedin_profile_url_helper_returns_a_real_linkedin_link(self):
        prospect = make_prospect()
        ContactPerson.objects.create(
            prospect=prospect, full_name="Julie Martin",
            profile_url="https://linkedin.com/in/julie-martin", is_active=True, confidence_score=80,
        )
        self.assertEqual(linkedin_profile_url(prospect), "https://linkedin.com/in/julie-martin")


# ---------------------------------------------------------------------------
# §6 — pattern email : sources indépendantes seulement
# ---------------------------------------------------------------------------

class EmailPatternIndependentSourcesTests(TestCase):
    def test_contacts_inferred_purely_from_splitting_the_email_never_count(self):
        prospect = make_prospect(website="https://ex-entreprise.example")
        # Reproduit exactement store_email()/split_person_from_email() :
        # "Sales Europe" / "Marketing Team" n'ont jamais été vus indépendamment,
        # juste découpés depuis l'adresse elle-même.
        ContactPerson.objects.create(
            prospect=prospect, full_name="Sales Europe", email="sales.europe@ex-entreprise.example",
            is_active=True, raw_payload={"inferred_from_email": True},
        )
        ContactPerson.objects.create(
            prospect=prospect, full_name="Marketing Team", email="marketing.team@ex-entreprise.example",
            is_active=True, raw_payload={"inferred_from_email": True},
        )
        self.assertIsNone(infer_domain_email_pattern(prospect))

    def test_independently_sourced_contacts_are_used_for_the_pattern(self):
        prospect = make_prospect(website="https://ex-entreprise.example")
        ContactPerson.objects.create(
            prospect=prospect, full_name="Julie Martin", email="julie.martin@ex-entreprise.example",
            is_active=True, raw_payload={"method": "json_ld_person"},
        )
        ContactPerson.objects.create(
            prospect=prospect, full_name="Marc Dupuis", email="marc.dupuis@ex-entreprise.example",
            is_active=True, raw_payload={"method": "heuristic_team_page_text"},
        )
        result = infer_domain_email_pattern(prospect)
        self.assertIsNotNone(result)
        self.assertEqual(result["pattern"], "first.last")

    def test_mix_of_inferred_and_independent_only_counts_independent(self):
        prospect = make_prospect(website="https://ex-entreprise.example")
        ContactPerson.objects.create(
            prospect=prospect, full_name="Sales Europe", email="sales.europe@ex-entreprise.example",
            is_active=True, raw_payload={"inferred_from_email": True},
        )
        ContactPerson.objects.create(
            prospect=prospect, full_name="Julie Martin", email="julie.martin@ex-entreprise.example",
            is_active=True, raw_payload={"method": "json_ld_person"},
        )
        # Un seul contact indépendant confirmé : toujours en dessous du minimum de 2.
        self.assertIsNone(infer_domain_email_pattern(prospect))


# ---------------------------------------------------------------------------
# §7 — INTENT : contenu marketing générique ne doit pas gonfler l'Intent
# ---------------------------------------------------------------------------

GROWTH_BLOG_PAGE = """<html><body>
<script type="application/ld+json">
{"@type": "BlogPosting", "headline": "5 conseils marketing pour votre SEO", "datePublished": "2026-08-15"}
</script>
</body></html>"""
GROWTH_HIRING_PAGE = """<html><body>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Growth Manager", "datePublished": "2026-08-20"}
</script>
</body></html>"""


class DatedContentIsNotIntentTests(TestCase):
    def test_dated_content_published_is_classified_fit_not_intent(self):
        collector = RecentActivitySignalCollector()
        self.assertEqual(collector.FIELD_CONFIG["dated_content_published"]["signal_group"], "fit")
        self.assertEqual(collector.FIELD_CONFIG["job_posting_growth"]["signal_group"], "intent")
        self.assertEqual(collector.FIELD_CONFIG["news_acquisition"]["signal_group"], "intent")

    def test_scenario_a_generic_marketing_blog_produces_fit_not_intent(self):
        prospect = make_prospect(website="https://agence-a.example")
        client = FakeClient({"https://agence-a.example": _html_response(GROWTH_BLOG_PAGE, "https://agence-a.example")})
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        prospect.refresh_from_db()
        self.assertEqual(prospect.intent_score, 0)
        self.assertTrue(ProspectSignal.objects.filter(prospect=prospect, signal_group="fit", signal_type="activity_dated_content_published").exists())

    def test_scenario_b_adds_real_growth_hiring_and_intent_goes_up(self):
        prospect = make_prospect(website="https://agence-b.example")
        client = FakeClient({"https://agence-b.example": _html_response(GROWTH_HIRING_PAGE, "https://agence-b.example")})
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        prospect.refresh_from_db()
        self.assertGreater(prospect.intent_score, 0)

    def test_scenario_c_engagement_pushes_priority_to_the_top(self):
        from prospects.services.predictneed_scoring import score_prospect

        prospect = make_prospect(website="https://agence-c.example")
        client = FakeClient({"https://agence-c.example": _html_response(GROWTH_HIRING_PAGE, "https://agence-c.example")})
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        prospect.refresh_from_db()
        priority_before = prospect.predictneed_acquisition_score

        from prospects.models import EngagementEvent
        EngagementEvent.objects.create(
            prospect=prospect, event_type="simulator_completed", source="predictneed",
            occurred_at=timezone.now(),
        )
        score_prospect(prospect)
        prospect.refresh_from_db()
        self.assertGreaterEqual(prospect.predictneed_acquisition_score, priority_before)
        self.assertGreater(prospect.engagement_score, 0)


# ---------------------------------------------------------------------------
# §8 — cache / cooldown de crawl
# ---------------------------------------------------------------------------

class CrawlCooldownTests(TestCase):
    @override_settings(WEB_ENRICHMENT_COOLDOWN_MINUTES=60)
    @patch("prospects.services.enrichment.crawl_site")
    def test_second_enrichment_shortly_after_reuses_the_first_without_recrawling(self, mocked_crawl_site):
        mocked_crawl_site.return_value = {"pages": []}
        prospect = make_prospect(website="https://ex-entreprise.example")

        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)

        self.assertEqual(mocked_crawl_site.call_count, 1)

    @override_settings(WEB_ENRICHMENT_COOLDOWN_MINUTES=60)
    @patch("prospects.services.enrichment.crawl_site")
    def test_force_refresh_bypasses_the_cooldown(self, mocked_crawl_site):
        mocked_crawl_site.return_value = {"pages": []}
        prospect = make_prospect(website="https://ex-entreprise.example")

        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        EnrichmentEngine(source_keys=["company_website"], force_refresh=True).enrich_prospect(prospect)

        self.assertEqual(mocked_crawl_site.call_count, 2)

    @override_settings(WEB_ENRICHMENT_COOLDOWN_MINUTES=0)
    @patch("prospects.services.enrichment.crawl_site")
    def test_zero_minute_cooldown_never_blocks_a_second_crawl(self, mocked_crawl_site):
        mocked_crawl_site.return_value = {"pages": []}
        prospect = make_prospect(website="https://ex-entreprise.example")

        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)
        EnrichmentEngine(source_keys=["company_website"]).enrich_prospect(prospect)

        self.assertEqual(mocked_crawl_site.call_count, 2)


# ---------------------------------------------------------------------------
# §9 — garde-fou SSRF, jamais de requête réelle dans ces tests
# ---------------------------------------------------------------------------

class IsSafeUrlTests(TestCase):
    def test_rejects_non_http_scheme(self):
        self.assertFalse(url_safety.is_safe_url("ftp://example.com/"))

    def test_rejects_localhost_literal(self):
        self.assertFalse(url_safety.is_safe_url("http://localhost/"))

    def test_rejects_loopback_ip(self):
        self.assertFalse(url_safety.is_safe_url("http://127.0.0.1/"))

    def test_rejects_private_ip(self):
        self.assertFalse(url_safety.is_safe_url("http://10.0.0.5/"))
        self.assertFalse(url_safety.is_safe_url("http://192.168.1.1/"))

    def test_rejects_link_local_cloud_metadata_ip(self):
        self.assertFalse(url_safety.is_safe_url("http://169.254.169.254/"))

    def test_accepts_a_public_ip_literal(self):
        self.assertTrue(url_safety.is_safe_url("http://93.184.216.34/"))

    def test_accepts_a_public_hostname(self):
        with patch.object(url_safety.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            self.assertTrue(url_safety.is_safe_url("https://example.com/"))

    def test_rejects_a_hostname_resolving_to_a_private_ip(self):
        """Défense contre le DNS rebinding : un hostname public en apparence
        mais qui résout vers une IP privée doit être bloqué."""
        with patch.object(url_safety.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            self.assertFalse(url_safety.is_safe_url("https://evil.example/"))

    def test_dns_failure_is_treated_as_unsafe(self):
        with patch.object(url_safety.socket, "getaddrinfo", side_effect=url_safety.socket.gaierror()):
            self.assertFalse(url_safety.is_safe_url("https://ne-existe-pas.example/"))

    def test_never_raises_on_malformed_url(self):
        self.assertFalse(url_safety.is_safe_url("http://[::1"))


class AssertSafeResponseTests(TestCase):
    def _response(self, url, history=None, content=b"ok"):
        r = Mock()
        r.url = url
        r.history = history or []
        r.content = content
        return r

    def test_raises_when_final_url_is_unsafe(self):
        with self.assertRaises(url_safety.UnsafeUrlError):
            url_safety.assert_safe_response(self._response("http://127.0.0.1/"))

    def test_raises_when_a_redirect_step_is_unsafe(self):
        hop = self._response("http://127.0.0.1/")
        with self.assertRaises(url_safety.UnsafeUrlError):
            url_safety.assert_safe_response(self._response("http://93.184.216.34/", history=[hop]))

    def test_raises_when_content_exceeds_the_size_cap(self):
        with self.assertRaises(url_safety.UnsafeUrlError):
            url_safety.assert_safe_response(self._response(
                "http://93.184.216.34/", content=b"x" * (url_safety.MAX_RESPONSE_BYTES + 1),
            ))

    def test_does_not_raise_for_a_safe_small_response(self):
        url_safety.assert_safe_response(self._response("http://93.184.216.34/"))


class CrawlerSsrfWiringTests(TestCase):
    def test_crawl_site_never_fetches_a_private_ip_seed(self):
        client = FakeClient({})
        with patch.object(crawler.httpx, "Client", return_value=client):
            with patch.object(crawler.RobotsPolicy, "load", return_value=_no_robots_policy()):
                crawler.crawl_site("http://127.0.0.1", max_pages=3, check_broken_links=False)
        self.assertEqual(client.requested_urls, [])

    def test_sitemap_urls_never_fetches_a_private_ip(self):
        entries = crawler.sitemap_urls("http://10.0.0.5", policy=_no_robots_policy())
        self.assertEqual(entries, [])
