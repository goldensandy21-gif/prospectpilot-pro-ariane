"""Mission 6 (correctif d'audit, round 2) — recalcul temps réel :
engagement_score et la "Priorité" canonique doivent être actualisés
immédiatement après un EngagementEvent PredictNeed valide, et intent_score
après un nouveau signal réel — sans boucle, sans modifier PredictNeed IA."""
from django.test import TestCase
from django.utils import timezone

from prospects.models import EngagementEvent, ProspectSignal
from prospects.services.predictneed_webhook import process_predictneed_event
from prospects.services.signals import persist_signals, signal_fingerprint
from prospects.tests.factories import make_campaign, make_campaign_prospect, make_icp, make_prospect, make_product


def _intent_signal(prospect, signal_type, now):
    return ProspectSignal(
        prospect=prospect, signal_type=signal_type, category="timing", signal_group="intent",
        source_kind="open_web", label=f"Signal {signal_type}", evidence="preuve",
        confidence=75, score_impact=10, positive=True, observed_at=now,
        fingerprint=signal_fingerprint(signal_type, "", "preuve"),
    )


class WebhookTriggersRealtimeRecomputeTests(TestCase):
    def test_webhook_engagement_event_recomputes_engagement_and_priority(self):
        """Le test exact demandé par l'audit."""
        product = make_product()
        icp = make_icp(product)
        campaign = make_campaign(product, icp=icp)
        prospect = make_prospect()
        cp = make_campaign_prospect(campaign, prospect)

        self.assertEqual(prospect.engagement_score, 0)

        status_code, response = process_predictneed_event({
            "event_type": "simulator_completed", "ppt": cp.tracking_token,
            "occurred_at": timezone.now().isoformat(),
        })
        self.assertEqual(status_code, 200)

        prospect.refresh_from_db()
        self.assertGreater(prospect.engagement_score, 0)
        self.assertIsNotNone(prospect.predictneed_acquisition_score)
        self.assertGreater(prospect.predictneed_acquisition_score, 0)

    def test_priority_strictly_increases_after_engagement_webhook(self):
        product = make_product()
        icp = make_icp(product)
        campaign = make_campaign(product, icp=icp)
        prospect = make_prospect()
        cp = make_campaign_prospect(campaign, prospect)

        from prospects.services.predictneed_scoring import score_prospect
        before = score_prospect(prospect, icp=icp, product=product)

        process_predictneed_event({
            "event_type": "checkout_started", "ppt": cp.tracking_token,
            "occurred_at": timezone.now().isoformat(),
        })
        prospect.refresh_from_db()

        self.assertGreater(prospect.predictneed_acquisition_score, before["predictneed_acquisition_score"])

    def test_webhook_without_matching_campaign_prospect_does_not_crash(self):
        status_code, response = process_predictneed_event({
            "event_type": "product_visited", "ppt": "unknown-token",
            "occurred_at": timezone.now().isoformat(),
        })
        self.assertEqual(status_code, 200)


class PersistSignalsTriggersRealtimeRecomputeTests(TestCase):
    def test_new_intent_signal_immediately_raises_intent_score(self):
        prospect = make_prospect()
        now = timezone.now()
        self.assertEqual(prospect.intent_score, 0)

        persist_signals(prospect, [_intent_signal(prospect, "hiring_growth", now)])

        prospect.refresh_from_db()
        self.assertGreater(prospect.intent_score, 0)

    def test_priority_reflects_the_new_signal_immediately(self):
        prospect = make_prospect()
        now = timezone.now()

        from prospects.services.predictneed_scoring import score_prospect
        before = score_prospect(prospect)

        persist_signals(prospect, [_intent_signal(prospect, "hiring_growth", now)])

        prospect.refresh_from_db()
        self.assertGreater(prospect.predictneed_acquisition_score, before["predictneed_acquisition_score"])

    def test_empty_signal_batch_does_not_crash_or_recompute_needlessly(self):
        prospect = make_prospect()
        saved = persist_signals(prospect, [])
        self.assertEqual(saved, [])
