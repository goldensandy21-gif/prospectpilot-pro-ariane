"""Mission 6 (correctif d'audit, round 2) — SiteChangeSignalCollector doit
être réellement branché dans le workflow de production (audit_site_task),
pas seulement appelable manuellement dans les tests."""
from unittest.mock import patch

from django.test import TestCase

from prospects.models import ProspectSignal
from prospects.tasks import audit_site_task
from prospects.tests.factories import make_prospect

PAGE_FIELDS = dict(
    depth=0, http_status=200, response_ms=120, content_type="text/html",
    title="Accueil", meta_description="desc", canonical="",
    h1_count=1, word_count=200, images_count=2, images_without_alt=0,
    internal_links=5, external_links=1, broken_links=0,
    has_https=True, has_viewport=True, has_contact_form=True,
    has_booking=False, has_phone=False, has_email=True, has_cta=False,
    issues=[],
)


def _crawl_result(url, technologies):
    page = {
        "url": url, "technologies": technologies,
        "found_emails": [], "found_phones": [], "found_contact_forms": [], "found_social_links": [],
        **PAGE_FIELDS,
    }
    return {"robots_allowed": True, "robots_url": "", "pages": [page]}


class SiteChangeWiringInAuditTaskTests(TestCase):
    @patch("prospects.tasks.run_pagespeed", return_value={"performance_score": None, "seo_score": None, "accessibility_score": None})
    @patch("prospects.tasks.crawl_site")
    def test_second_audit_with_new_technology_creates_a_dated_intent_signal(self, mock_crawl, mock_pagespeed):
        prospect = make_prospect(website="https://agence-audit.example")

        mock_crawl.return_value = _crawl_result(prospect.website, ["Google Analytics"])
        audit_site_task(prospect.pk)
        self.assertFalse(ProspectSignal.objects.filter(prospect=prospect, signal_type__startswith="site_change_").exists())

        mock_crawl.return_value = _crawl_result(prospect.website, ["Google Analytics", "HubSpot"])
        audit_site_task(prospect.pk)

        signal = ProspectSignal.objects.get(prospect=prospect, signal_type="site_change_HubSpot")
        self.assertEqual(signal.signal_group, "intent")
        self.assertIsNotNone(signal.observed_at)

    @patch("prospects.tasks.run_pagespeed", return_value={"performance_score": None, "seo_score": None, "accessibility_score": None})
    @patch("prospects.tasks.crawl_site")
    def test_first_ever_audit_creates_no_change_signal(self, mock_crawl, mock_pagespeed):
        prospect = make_prospect(website="https://agence-premiere.example")
        mock_crawl.return_value = _crawl_result(prospect.website, ["Google Analytics"])
        audit_site_task(prospect.pk)
        self.assertFalse(ProspectSignal.objects.filter(prospect=prospect, signal_type__startswith="site_change_").exists())

    @patch("prospects.tasks.run_pagespeed", return_value={"performance_score": None, "seo_score": None, "accessibility_score": None})
    @patch("prospects.tasks.crawl_site")
    def test_a_collector_failure_never_breaks_the_audit_task(self, mock_crawl, mock_pagespeed):
        prospect = make_prospect(website="https://agence-robuste.example")
        mock_crawl.return_value = _crawl_result(prospect.website, ["Google Analytics"])
        with patch(
            "prospects.services.signal_collectors.run_signal_collectors",
            side_effect=RuntimeError("panne simulée"),
        ):
            result = audit_site_task(prospect.pk)
        self.assertIn("run_id", result)
