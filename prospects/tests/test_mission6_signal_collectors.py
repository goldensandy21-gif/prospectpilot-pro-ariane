from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from prospects.models import ContactPerson, ProspectEvidence, ProspectSignal, ProspectTechnology, PublicSocialLink
from prospects.services.signal_collectors import (
    DecisionMakerSignalCollector,
    QuickScanSignalCollector,
    RecentActivitySignalCollector,
    SignalCollector,
    SiteChangeSignalCollector,
    SocialPresenceSignalCollector,
    TechnologySignalCollector,
    run_signal_collectors,
)
from prospects.tests.factories import make_prospect


class TechnologySignalCollectorTests(TestCase):
    def test_no_technologies_returns_empty_list(self):
        prospect = make_prospect()
        self.assertEqual(TechnologySignalCollector().collect(prospect), [])

    def test_detected_analytics_technology_produces_a_signal(self):
        prospect = make_prospect()
        ProspectTechnology.objects.create(prospect=prospect, technology="Google Analytics", category="analytics")
        signals = TechnologySignalCollector().collect(prospect)
        self.assertTrue(any(s.signal_type == "analytics_detected" for s in signals))
        self.assertEqual(signals[0].source_kind, "technology")


class SocialPresenceSignalCollectorTests(TestCase):
    def test_no_social_link_returns_empty_list(self):
        prospect = make_prospect()
        self.assertEqual(SocialPresenceSignalCollector().collect(prospect), [])

    def test_active_linkedin_link_produces_a_fit_signal_never_intent(self):
        prospect = make_prospect()
        PublicSocialLink.objects.create(
            prospect=prospect, platform="linkedin", url="https://linkedin.com/company/exemple",
            source_url="https://exemple.example", is_active=True,
        )
        signals = SocialPresenceSignalCollector().collect(prospect)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal_group, "fit")
        self.assertEqual(signals[0].source_kind, "social")

    def test_inactive_social_link_is_ignored(self):
        prospect = make_prospect()
        PublicSocialLink.objects.create(
            prospect=prospect, platform="linkedin", url="https://linkedin.com/company/exemple", is_active=False,
        )
        self.assertEqual(SocialPresenceSignalCollector().collect(prospect), [])


class DecisionMakerSignalCollectorTests(TestCase):
    def test_no_contact_returns_empty_list(self):
        prospect = make_prospect()
        self.assertEqual(DecisionMakerSignalCollector().collect(prospect), [])

    def test_relevant_job_title_produces_a_fit_signal(self):
        prospect = make_prospect()
        ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont", job_title="Responsable marketing",
            is_active=True, confidence_score=80,
        )
        signals = DecisionMakerSignalCollector().collect(prospect)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal_group, "fit")

    def test_irrelevant_job_title_produces_no_signal(self):
        prospect = make_prospect()
        ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont", job_title="Stagiaire logistique",
            is_active=True,
        )
        self.assertEqual(DecisionMakerSignalCollector().collect(prospect), [])

    def test_contact_without_job_title_produces_no_signal(self):
        prospect = make_prospect()
        ContactPerson.objects.create(prospect=prospect, full_name="Alex Dupont", is_active=True)
        self.assertEqual(DecisionMakerSignalCollector().collect(prospect), [])


class RunSignalCollectorsTests(TestCase):
    def test_runs_all_default_collectors_and_persists_via_persist_signals(self):
        prospect = make_prospect()
        PublicSocialLink.objects.create(
            prospect=prospect, platform="linkedin", url="https://linkedin.com/company/exemple", is_active=True,
        )
        ProspectTechnology.objects.create(prospect=prospect, technology="HubSpot", category="crm")

        saved, errors = run_signal_collectors(prospect)

        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(saved), 2)
        self.assertTrue(ProspectSignal.objects.filter(prospect=prospect, signal_type="crm_detected").exists())
        self.assertTrue(ProspectSignal.objects.filter(prospect=prospect, signal_type="social_presence_linkedin").exists())

    def test_running_twice_does_not_duplicate_rows(self):
        prospect = make_prospect()
        PublicSocialLink.objects.create(
            prospect=prospect, platform="linkedin", url="https://linkedin.com/company/exemple", is_active=True,
        )
        run_signal_collectors(prospect, collectors=[SocialPresenceSignalCollector()])
        run_signal_collectors(prospect, collectors=[SocialPresenceSignalCollector()])
        self.assertEqual(
            ProspectSignal.objects.filter(prospect=prospect, signal_type="social_presence_linkedin").count(), 1,
        )

    def test_a_failing_collector_does_not_block_the_others(self):
        class BrokenCollector(SignalCollector):
            name = "broken"

            def collect(self, prospect):
                raise RuntimeError("boom")

        prospect = make_prospect()
        PublicSocialLink.objects.create(
            prospect=prospect, platform="linkedin", url="https://linkedin.com/company/exemple", is_active=True,
        )
        saved, errors = run_signal_collectors(prospect, collectors=[BrokenCollector(), SocialPresenceSignalCollector()])
        self.assertEqual(len(errors), 1)
        self.assertIn("broken", errors[0])
        self.assertEqual(len(saved), 1)


class QuickScanSignalCollectorTests(TestCase):
    def test_no_search_candidate_returns_empty_list(self):
        prospect = make_prospect()
        self.assertEqual(QuickScanSignalCollector().collect(prospect), [])

    def test_static_site_features_are_fit_not_intent(self):
        """Correctif d'audit : un formulaire de contact/booking/lead magnet
        est une caractéristique statique du site (FIT/maturité), jamais une
        intention d'achat actuelle — même détecté aujourd'hui."""
        from prospects.models import CompanySearchRun, SearchCandidate

        prospect = make_prospect()
        run = CompanySearchRun.objects.create(mode="manual")
        SearchCandidate.objects.create(
            search_run=run, siren="111111111", name=prospect.name, prospect=prospect, status="scanned",
            quick_scan_data={
                "pages_checked": 5, "worth_full_analysis": True,
                "has_contact_form": True, "has_booking": True, "has_lead_magnet": True,
            },
        )
        signals = QuickScanSignalCollector().collect(prospect)
        self.assertGreater(len(signals), 0)
        for signal in signals:
            self.assertEqual(signal.signal_group, "fit")


def _audit_summary(prospect, technologies, created_at, status="done", pages_crawled=5, start_url=None):
    from prospects.models import CrawlRun, SiteAuditSummary

    run = CrawlRun.objects.create(
        prospect=prospect, start_url=start_url or prospect.website or "https://example.com",
        status=status, pages_crawled=pages_crawled,
    )
    summary = SiteAuditSummary.objects.create(prospect=prospect, crawl_run=run, technologies=technologies)
    SiteAuditSummary.objects.filter(pk=summary.pk).update(created_at=created_at)
    return summary


class SiteChangeSignalCollectorTests(TestCase):
    """Correctif d'audit (round 2) : un changement doit être prouvé par deux
    états comparables (absent puis présent), jamais déduit du simple fait
    qu'une AUTRE technologie du prospect est ancienne."""

    def test_fewer_than_two_audits_produces_no_signal(self):
        prospect = make_prospect()
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])

        _audit_summary(prospect, ["Google Analytics"], timezone.now())
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])

    def test_technology_absent_then_present_produces_a_dated_intent_signal(self):
        now = timezone.now()
        prospect = make_prospect()
        _audit_summary(prospect, ["Google Analytics"], now - timedelta(days=30))
        _audit_summary(prospect, ["Google Analytics", "HubSpot"], now - timedelta(days=1))

        signals = SiteChangeSignalCollector().collect(prospect)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].value, "HubSpot")
        self.assertEqual(signals[0].signal_group, "intent")
        self.assertIsNotNone(signals[0].observed_at)

    def test_technology_present_in_both_audits_produces_no_signal(self):
        """Une technologie ancienne, simplement reconfirmée par un nouvel
        audit, n'est pas un changement — même si un autre prospect ou une
        autre technologie a bien changé ailleurs."""
        now = timezone.now()
        prospect = make_prospect()
        _audit_summary(prospect, ["Google Analytics"], now - timedelta(days=30))
        _audit_summary(prospect, ["Google Analytics"], now - timedelta(days=1))
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])

    def test_technology_never_absent_before_is_never_flagged_even_if_another_is_old(self):
        """Reproduit exactement le bug corrigé : HubSpot est présent dans
        les DEUX audits (donc jamais prouvé absent avant) — le fait que
        Google Analytics soit ancien ne doit pas le faire conclure "nouveau"."""
        now = timezone.now()
        prospect = make_prospect()
        _audit_summary(prospect, ["Google Analytics", "HubSpot"], now - timedelta(days=60))
        _audit_summary(prospect, ["Google Analytics", "HubSpot"], now - timedelta(days=1))
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])

    def test_failed_previous_audit_is_never_used_as_proof_of_absence(self):
        """Correctif d'audit (round 3) : un CrawlRun en échec ne prouve rien
        — il peut s'être arrêté avant même de voir la page qui porte la
        technologie. Jamais utilisé comme preuve d'absence."""
        now = timezone.now()
        prospect = make_prospect()
        _audit_summary(prospect, ["Google Analytics"], now - timedelta(days=30), status="failed")
        _audit_summary(prospect, ["Google Analytics", "HubSpot"], now - timedelta(days=1))
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])

    def test_failed_current_audit_is_never_used_as_proof_of_presence(self):
        now = timezone.now()
        prospect = make_prospect()
        _audit_summary(prospect, ["Google Analytics"], now - timedelta(days=30))
        _audit_summary(prospect, ["Google Analytics", "HubSpot"], now - timedelta(days=1), status="failed")
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])

    def test_radically_different_coverage_produces_no_signal(self):
        """Comparer un scan de 2 pages à un scan de 40 pages ne prouve rien :
        une technologie "absente" du petit scan peut simplement ne jamais
        avoir été vue, pas avoir disparu du site."""
        now = timezone.now()
        prospect = make_prospect()
        _audit_summary(prospect, ["Google Analytics"], now - timedelta(days=30), pages_crawled=2)
        _audit_summary(prospect, ["Google Analytics", "HubSpot"], now - timedelta(days=1), pages_crawled=40)
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])

    def test_comparable_coverage_still_produces_a_signal(self):
        now = timezone.now()
        prospect = make_prospect()
        _audit_summary(prospect, ["Google Analytics"], now - timedelta(days=30), pages_crawled=8)
        _audit_summary(prospect, ["Google Analytics", "HubSpot"], now - timedelta(days=1), pages_crawled=10)
        signals = SiteChangeSignalCollector().collect(prospect)
        self.assertEqual(len(signals), 1)

    def test_different_start_url_produces_no_signal(self):
        """Deux audits sur des URLs de départ différentes ne sont pas
        comparables (site différent ou changement de domaine principal)."""
        now = timezone.now()
        prospect = make_prospect()
        _audit_summary(prospect, ["Google Analytics"], now - timedelta(days=30), start_url="https://ancien-domaine.example")
        _audit_summary(prospect, ["Google Analytics", "HubSpot"], now - timedelta(days=1), start_url="https://nouveau-domaine.example")
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])

    def test_only_compares_the_two_most_recent_audits(self):
        now = timezone.now()
        prospect = make_prospect()
        _audit_summary(prospect, [], now - timedelta(days=90))
        _audit_summary(prospect, ["HubSpot"], now - timedelta(days=45))
        _audit_summary(prospect, ["HubSpot"], now - timedelta(days=1))
        # HubSpot est apparu il y a 45 jours (entre le 1er et le 2e audit),
        # mais était déjà présent lors du dernier changement de référence
        # (2e vs 3e audit) -> pas un signal "nouveau" à ce stade.
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])


class RecentActivitySignalCollectorTests(TestCase):
    def test_no_evidence_returns_empty_list(self):
        prospect = make_prospect()
        self.assertEqual(RecentActivitySignalCollector().collect(prospect), [])

    def test_evidence_with_real_event_date_produces_a_dated_intent_signal(self):
        prospect = make_prospect()
        ProspectEvidence.objects.create(
            prospect=prospect, field_name="job_posting_growth", value="Offre Responsable Growth",
            normalized_value="offre responsable growth", source_url="https://exemple.example/carrieres",
            confidence_score=70, is_current=True,
            raw_payload={"event_date": "2026-08-10T00:00:00+00:00"},
        )
        signals = RecentActivitySignalCollector().collect(prospect)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal_group, "intent")
        self.assertEqual(signals[0].observed_at.date().isoformat(), "2026-08-10")

    def test_evidence_without_real_event_date_produces_no_signal(self):
        """Jamais de fallback sur collected_at : sans date réelle explicite,
        silence plutôt qu'invention."""
        prospect = make_prospect()
        ProspectEvidence.objects.create(
            prospect=prospect, field_name="job_posting_growth", value="Offre Responsable Growth",
            normalized_value="offre responsable growth 2", source_url="https://exemple.example/carrieres",
            confidence_score=70, is_current=True, raw_payload={},
        )
        self.assertEqual(RecentActivitySignalCollector().collect(prospect), [])

    def test_unlisted_field_name_is_ignored(self):
        prospect = make_prospect()
        ProspectEvidence.objects.create(
            prospect=prospect, field_name="some_other_fact", value="x", normalized_value="x",
            confidence_score=70, is_current=True, raw_payload={"event_date": "2026-08-10T00:00:00+00:00"},
        )
        self.assertEqual(RecentActivitySignalCollector().collect(prospect), [])

    def test_not_current_evidence_is_ignored(self):
        prospect = make_prospect()
        ProspectEvidence.objects.create(
            prospect=prospect, field_name="job_posting_growth", value="Offre ancienne",
            normalized_value="offre ancienne", confidence_score=70, is_current=False,
            raw_payload={"event_date": "2026-08-10T00:00:00+00:00"},
        )
        self.assertEqual(RecentActivitySignalCollector().collect(prospect), [])
