from django.test import TestCase

from prospects.models import ContactPerson, ProspectSignal, ProspectTechnology, PublicSocialLink
from prospects.services.signal_collectors import (
    DecisionMakerSignalCollector,
    QuickScanSignalCollector,
    SignalCollector,
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
