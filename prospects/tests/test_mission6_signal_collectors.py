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


class SiteChangeSignalCollectorTests(TestCase):
    def test_no_prior_history_produces_no_signal(self):
        """Sans scan antérieur, on ne peut pas distinguer un vrai changement
        d'une simple première visite du site — donc aucun signal."""
        prospect = make_prospect()
        ProspectTechnology.objects.create(prospect=prospect, technology="Google Analytics", category="analytics")
        self.assertEqual(SiteChangeSignalCollector().collect(prospect), [])

    def test_new_technology_since_prior_scan_produces_a_dated_intent_signal(self):
        now = timezone.now()
        prospect = make_prospect()
        old_tech = ProspectTechnology.objects.create(prospect=prospect, technology="Google Analytics", category="analytics")
        ProspectTechnology.objects.filter(pk=old_tech.pk).update(detected_at=now - timedelta(days=30))

        new_tech = ProspectTechnology.objects.create(prospect=prospect, technology="HubSpot", category="crm")
        ProspectTechnology.objects.filter(pk=new_tech.pk).update(detected_at=now - timedelta(days=1))

        signals = SiteChangeSignalCollector().collect(prospect)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal_group, "intent")
        self.assertIsNotNone(signals[0].observed_at)

    def test_technology_within_buffer_of_first_scan_is_not_flagged_as_change(self):
        """Une technologie détectée il y a 2 jours, sans AUCUNE technologie
        plus ancienne que le buffer, ne prouve rien de plus qu'une première
        visite récente — pas de signal."""
        now = timezone.now()
        prospect = make_prospect()
        tech = ProspectTechnology.objects.create(prospect=prospect, technology="Google Analytics", category="analytics")
        ProspectTechnology.objects.filter(pk=tech.pk).update(detected_at=now - timedelta(days=2))
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
