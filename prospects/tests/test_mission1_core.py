"""Mission 1 — recherche, pré-score, site, technologies, signaux, score, AgentBrief."""
import json
from unittest.mock import Mock, patch

import httpx
from django.test import TestCase

from prospects.services import company_search, prescoring, site_discovery, technology
from prospects.services.agent_brief import generate_agent_brief
from prospects.services.predictneed_scoring import score_prospect
from prospects.services.signals import (
    build_competitor_detections,
    build_signals_from_technologies,
    persist_competitor_detections,
    persist_signals,
    persist_technologies,
)
from prospects.models import Competitor, Suppression
from .factories import make_icp, make_product, make_prospect, make_public_email


def _fake_response(status_code, json_body=None, text="", headers=None):
    r = Mock(spec=httpx.Response)
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.text = text
    r.headers = headers or {}
    r.raise_for_status = Mock()
    return r


class CompanySearchAPITests(TestCase):
    def test_dedupe_rows_by_siren(self):
        rows = [{"siren": "111"}, {"siren": "111"}, {"siren": "222", "siret": "222x"}]
        out = company_search.dedupe_rows(rows)
        self.assertEqual([r["siren"] for r in out], ["111", "222"])

    def test_build_params_supports_multiple_naf_and_new_filters(self):
        params, naf = company_search.build_params(
            naf_codes=["7311Z", "6201Z"], section_activite_principale=["M"],
            categorie_entreprise="PME", est_qualiopi=True, ca_min=100000,
        )
        self.assertEqual(params["activite_principale"], "73.11Z,62.01Z")
        self.assertEqual(params["section_activite_principale"], "M")
        self.assertEqual(params["categorie_entreprise"], "PME")
        self.assertTrue(params["est_qualiopi"])
        self.assertEqual(params["ca_min"], 100000)

    @patch("prospects.services.company_search.httpx.Client")
    def test_429_then_success_respects_retry_after(self, mock_client_cls):
        client = mock_client_cls.return_value.__enter__.return_value
        rate_limited = _fake_response(429, headers={"retry-after": "0"})
        ok = _fake_response(200, {"results": [], "page": 1, "per_page": 25, "total_results": 0, "total_pages": 1})
        client.get.side_effect = [rate_limited, ok]
        with patch("prospects.services.company_search.time.sleep"):
            result = company_search.search_companies(query="test")
        self.assertEqual(result["total_results"], 0)
        self.assertEqual(client.get.call_count, 2)

    @patch("prospects.services.company_search.httpx.Client")
    def test_persistent_429_raises_search_api_error(self, mock_client_cls):
        client = mock_client_cls.return_value.__enter__.return_value
        client.get.return_value = _fake_response(429, headers={"retry-after": "0"})
        with patch("prospects.services.company_search.time.sleep"):
            with self.assertRaises(company_search.SearchAPIError):
                company_search.search_companies(query="test")

    @patch("prospects.services.company_search.httpx.Client")
    def test_timeout_then_success(self, mock_client_cls):
        client = mock_client_cls.return_value.__enter__.return_value
        ok = _fake_response(200, {"results": [], "page": 1, "per_page": 25, "total_results": 0, "total_pages": 1})
        client.get.side_effect = [httpx.TimeoutException("slow"), ok]
        with patch("prospects.services.company_search.time.sleep"):
            result = company_search.search_companies(query="test")
        self.assertEqual(result["total_results"], 0)


class PrescoringTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)

    def test_naf_match_scores_high(self):
        row = {
            "sector": "Agence web", "naf_code": "73.11Z", "employee_band": "12",
            "prospecting_allowed": True, "source_payload": {},
        }
        result = prescoring.registry_pre_score(row, self.icp)
        self.assertGreaterEqual(result["score"], 60)
        self.assertFalse(result["excluded"])

    def test_diffusion_partial_is_hard_excluded(self):
        row = {"prospecting_allowed": False, "sector": "", "naf_code": "", "source_payload": {}}
        result = prescoring.registry_pre_score(row, self.icp)
        self.assertTrue(result["excluded"])
        self.assertEqual(result["score"], 0)

    def test_preselect_top_candidates_respects_target_count(self):
        rows = [
            ({"siren": str(i)}, {"score": 100 - i, "reasons": [], "excluded": False, "exclusion_reason": ""})
            for i in range(10)
        ]
        chosen = prescoring.preselect_top_candidates(rows, target_count=3)
        self.assertEqual(len(chosen), 3)
        self.assertEqual([row["siren"] for row, _ in chosen], ["0", "1", "2"])


class SiteDiscoveryConfidenceTests(TestCase):
    def test_siren_match_gives_strong_confidence(self):
        text = "Agence Exemple - SIREN 123456789 - mentions légales"
        score, evidence = site_discovery.evaluate_site_confidence(
            text, "Agence Exemple", "Agence Exemple", city="Lyon", siren="123456789",
        )
        self.assertGreaterEqual(score, 60)
        self.assertIn("siren_match", evidence)

    def test_no_evidence_gives_low_confidence(self):
        score, evidence = site_discovery.evaluate_site_confidence(
            "Page sans rapport", "", "Agence Exemple", city="Lyon", siren="123456789",
        )
        self.assertLess(score, 20)


class TechnologyAndSignalTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.prospect = make_prospect()
        Competitor.objects.create(name="Hotjar", category="behaviour_analytics", scoring_bonus=8)

    def test_detect_technologies_detailed_includes_evidence(self):
        html = '<script src="https://static.hotjar.com/c/hotjar-123.js"></script>'
        results = technology.detect_technologies_detailed(html, source_url="https://x.example")
        names = {r["technology"] for r in results}
        self.assertIn("Hotjar", names)
        self.assertTrue(all(r["evidence"] for r in results))

    def test_behaviour_analytics_signal_is_positive_not_negative(self):
        techs = [{"technology": "Hotjar", "category": "behaviour_analytics", "source_url": "", "evidence": "x", "confidence": 80}]
        signals = build_signals_from_technologies(self.prospect, techs)
        behaviour_signals = [s for s in signals if s.signal_type == "behaviour_analytics_detected"]
        self.assertEqual(len(behaviour_signals), 1)
        self.assertTrue(behaviour_signals[0].positive)

    def test_competitor_detection_created_from_known_technology(self):
        techs = [{"technology": "Hotjar", "category": "behaviour_analytics", "source_url": "", "evidence": "x", "confidence": 80}]
        detections = build_competitor_detections(self.prospect, techs)
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].competitor.name, "Hotjar")


class ScoringHardRulesTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)

    def test_prospecting_not_allowed_zeroes_score(self):
        prospect = make_prospect(prospecting_allowed=False)
        make_public_email(prospect)
        result = score_prospect(prospect, icp=self.icp, product=self.product)
        self.assertEqual(result["predictneed_acquisition_score"], 0)
        self.assertTrue(result["predictneed_excluded"])
        self.assertFalse(result["outbound_eligible"])

    def test_no_email_blocks_outbound_but_score_not_forced_excluded(self):
        prospect = make_prospect()
        result = score_prospect(prospect, icp=self.icp, product=self.product)
        self.assertFalse(result["outbound_eligible"])
        self.assertEqual(result["contactability_score"], 0)

    def test_suppression_hard_excludes(self):
        prospect = make_prospect()
        make_public_email(prospect, email="opposed@agence-exemple.example")
        Suppression.objects.create(email="opposed@agence-exemple.example")
        result = score_prospect(prospect, icp=self.icp, product=self.product)
        self.assertTrue(result["predictneed_excluded"])
        self.assertEqual(result["predictneed_acquisition_score"], 0)

    def test_domain_exclusion(self):
        self.icp.excluded_domains = ["agence-exemple.example"]
        self.icp.save()
        prospect = make_prospect(website="https://agence-exemple.example")
        make_public_email(prospect)
        result = score_prospect(prospect, icp=self.icp, product=self.product)
        self.assertTrue(result["predictneed_excluded"])

    def test_good_prospect_gets_positive_grade(self):
        prospect = make_prospect()
        make_public_email(prospect)
        result = score_prospect(prospect, icp=self.icp, product=self.product)
        self.assertGreater(result["predictneed_acquisition_score"], 0)
        self.assertIn(result["predictneed_grade"], ["A", "B", "C", "D"])
        self.assertTrue(result["predictneed_score_reasons"])


class AgentBriefNoHallucinationTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)

    def test_brief_without_signals_does_not_invent_observation(self):
        prospect = make_prospect()
        make_public_email(prospect)
        brief = generate_agent_brief(prospect, icp=self.icp, product=self.product)
        self.assertEqual(brief.relevant_signals, [])
        # Sans ProspectEvidence/ProspectSignal, aucune observation fabriquée :
        self.assertIn("Correspond aux critères", brief.why_this_company)

    def test_brief_recommended_contact_reflects_real_source(self):
        prospect = make_prospect()
        make_public_email(prospect, source_type="contact_page", source_url="https://agence-exemple.example/contact")
        brief = generate_agent_brief(prospect, icp=self.icp, product=self.product)
        self.assertIn("marie@agence-exemple.example", brief.recommended_contact)
        self.assertIn("contact_page", brief.recommended_contact_reason)

    def test_brief_evidence_urls_come_from_stored_signals(self):
        prospect = make_prospect()
        make_public_email(prospect)
        persist_technologies(prospect, [{"technology": "Hotjar", "category": "behaviour_analytics", "source_url": "https://agence-exemple.example", "evidence": "x", "confidence": 80}])
        persist_signals(prospect, build_signals_from_technologies(prospect, [{"technology": "Hotjar", "category": "behaviour_analytics", "source_url": "https://agence-exemple.example", "evidence": "x", "confidence": 80}]))
        brief = generate_agent_brief(prospect, icp=self.icp, product=self.product)
        self.assertIn("https://agence-exemple.example", brief.evidence_urls)
