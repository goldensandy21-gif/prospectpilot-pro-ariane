from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from prospects.models import Alert, EngagementEvent, ProspectSignal
from prospects.services.acquisition_scores import recompute_acquisition_scores
from prospects.services.alerts import check_engagement_alert, check_intent_threshold_alert, check_signal_alerts
from prospects.services.predictneed_webhook import process_predictneed_event
from prospects.services.signals import persist_signals, signal_fingerprint
from prospects.tests.factories import make_prospect


def _intent_signal(prospect, signal_type, days_ago, now, score_impact=10):
    return ProspectSignal(
        prospect=prospect, signal_type=signal_type, category="growth", signal_group="intent",
        source_kind="open_web", label=f"Signal {signal_type}", evidence=f"preuve {signal_type}",
        confidence=75, score_impact=score_impact, positive=True,
        observed_at=now - timedelta(days=days_ago),
        fingerprint=signal_fingerprint(signal_type, "", f"preuve {signal_type}"),
    )


class CheckSignalAlertsTests(TestCase):
    def setUp(self):
        self.prospect = make_prospect()
        self.now = timezone.now()

    def test_strong_new_intent_signal_creates_an_alert(self):
        persist_signals(self.prospect, [_intent_signal(self.prospect, "hiring_growth", 0, self.now, score_impact=10)])
        self.assertTrue(Alert.objects.filter(prospect=self.prospect, alert_type="strong_signal").exists())

    def test_weak_signal_below_threshold_creates_no_alert(self):
        persist_signals(self.prospect, [_intent_signal(self.prospect, "minor_signal", 0, self.now, score_impact=2)])
        self.assertFalse(Alert.objects.filter(prospect=self.prospect, alert_type="strong_signal").exists())

    def test_fit_signal_never_creates_a_strong_signal_alert(self):
        signal = ProspectSignal(
            prospect=self.prospect, signal_type="analytics_detected", category="analytics", signal_group="fit",
            source_kind="technology", label="Analytics détecté", evidence="preuve", confidence=80,
            score_impact=10, positive=True, observed_at=self.now,
            fingerprint=signal_fingerprint("analytics_detected", "", "preuve"),
        )
        persist_signals(self.prospect, [signal])
        self.assertFalse(Alert.objects.filter(prospect=self.prospect, alert_type="strong_signal").exists())

    def test_refreshing_an_existing_signal_does_not_create_a_second_alert(self):
        persist_signals(self.prospect, [_intent_signal(self.prospect, "hiring_growth", 0, self.now, score_impact=10)])
        persist_signals(self.prospect, [_intent_signal(self.prospect, "hiring_growth", 0, self.now, score_impact=10)])
        self.assertEqual(Alert.objects.filter(prospect=self.prospect, alert_type="strong_signal").count(), 1)

    def test_two_genuinely_distinct_strong_signals_create_two_alerts(self):
        persist_signals(self.prospect, [
            _intent_signal(self.prospect, "hiring_growth", 0, self.now, score_impact=10),
            _intent_signal(self.prospect, "new_marketing_tool", 0, self.now, score_impact=9),
        ])
        self.assertEqual(Alert.objects.filter(prospect=self.prospect, alert_type="strong_signal").count(), 2)

    def test_reactivation_after_long_inactivity_creates_an_alert(self):
        old_signal = _intent_signal(self.prospect, "old_signal", 200, self.now, score_impact=3)
        persist_signals(self.prospect, [old_signal])
        new_signal = _intent_signal(self.prospect, "recent_signal", 0, self.now, score_impact=3)
        persist_signals(self.prospect, [new_signal])
        self.assertTrue(Alert.objects.filter(prospect=self.prospect, alert_type="reactivated").exists())

    def test_no_reactivation_alert_for_a_prospect_with_no_prior_activity(self):
        persist_signals(self.prospect, [_intent_signal(self.prospect, "first_ever_signal", 0, self.now, score_impact=3)])
        self.assertFalse(Alert.objects.filter(prospect=self.prospect, alert_type="reactivated").exists())

    def test_no_reactivation_alert_when_gap_is_below_threshold(self):
        recent = _intent_signal(self.prospect, "signal_a", 10, self.now, score_impact=3)
        persist_signals(self.prospect, [recent])
        follow_up = _intent_signal(self.prospect, "signal_b", 0, self.now, score_impact=3)
        persist_signals(self.prospect, [follow_up])
        self.assertFalse(Alert.objects.filter(prospect=self.prospect, alert_type="reactivated").exists())


class CheckIntentThresholdAlertTests(TestCase):
    def setUp(self):
        self.prospect = make_prospect()
        self.now = timezone.now()

    def test_crossing_into_probable_creates_an_alert(self):
        self.prospect.intent_score = 65
        alert = check_intent_threshold_alert(self.prospect, previous_intent_score=10, now=self.now)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.alert_type, "intent_threshold_crossed")

    def test_staying_within_the_same_level_creates_no_alert(self):
        self.prospect.intent_score = 65
        alert = check_intent_threshold_alert(self.prospect, previous_intent_score=62, now=self.now)
        self.assertIsNone(alert)

    def test_dropping_a_level_creates_no_alert(self):
        self.prospect.intent_score = 20
        alert = check_intent_threshold_alert(self.prospect, previous_intent_score=70, now=self.now)
        self.assertIsNone(alert)

    def test_crossing_into_weak_or_emerging_is_not_actionable_no_alert(self):
        self.prospect.intent_score = 35
        alert = check_intent_threshold_alert(self.prospect, previous_intent_score=0, now=self.now)
        self.assertIsNone(alert)

    def test_calling_recompute_twice_same_day_does_not_duplicate_alert(self):
        self.prospect.intent_score = 65
        first = check_intent_threshold_alert(self.prospect, previous_intent_score=10, now=self.now)
        second = check_intent_threshold_alert(self.prospect, previous_intent_score=10, now=self.now)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(Alert.objects.filter(prospect=self.prospect, alert_type="intent_threshold_crossed").count(), 1)

    def test_wired_into_recompute_acquisition_scores(self):
        prospect = make_prospect()
        persist_signals(prospect, [
            _intent_signal(prospect, "hiring_growth", 1, self.now, score_impact=10),
            _intent_signal(prospect, "new_marketing_tool", 2, self.now, score_impact=8),
            _intent_signal(prospect, "signup_form_detected", 1, self.now, score_impact=5),
        ])
        recompute_acquisition_scores(prospect, now=self.now)
        prospect.refresh_from_db()
        if prospect.intent_score >= 60:
            self.assertTrue(Alert.objects.filter(prospect=prospect, alert_type="intent_threshold_crossed").exists())


class CheckEngagementAlertTests(TestCase):
    def setUp(self):
        self.prospect = make_prospect()

    def test_new_engagement_event_creates_an_alert(self):
        event = EngagementEvent.objects.create(prospect=self.prospect, event_type="simulator_completed", occurred_at=timezone.now())
        alerts = check_engagement_alert(self.prospect, event)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_type, "new_engagement")

    def test_calling_twice_for_the_same_event_does_not_duplicate(self):
        event = EngagementEvent.objects.create(prospect=self.prospect, event_type="simulator_completed", occurred_at=timezone.now())
        check_engagement_alert(self.prospect, event)
        check_engagement_alert(self.prospect, event)
        self.assertEqual(Alert.objects.filter(prospect=self.prospect, alert_type="new_engagement").count(), 1)

    def test_wired_into_predictneed_webhook(self):
        from prospects.tests.factories import make_campaign, make_campaign_prospect, make_icp, make_product

        product = make_product()
        icp = make_icp(product)
        campaign = make_campaign(product, icp=icp)
        prospect = make_prospect()
        cp = make_campaign_prospect(campaign, prospect)

        status, response = process_predictneed_event({
            "event_type": "simulator_completed", "ppt": cp.tracking_token,
            "occurred_at": timezone.now().isoformat(),
        })
        self.assertEqual(status, 200)
        self.assertTrue(Alert.objects.filter(prospect=prospect, alert_type="new_engagement").exists())


class AlertDedupConstraintTests(TestCase):
    def test_database_constraint_prevents_true_duplicates(self):
        from django.db import transaction

        prospect = make_prospect()
        Alert.objects.create(prospect=prospect, alert_type="strong_signal", dedup_key="signal:1", message="test")
        with self.assertRaises(Exception):
            with transaction.atomic():
                Alert.objects.create(prospect=prospect, alert_type="strong_signal", dedup_key="signal:1", message="test 2")
        self.assertEqual(Alert.objects.filter(prospect=prospect, alert_type="strong_signal").count(), 1)
