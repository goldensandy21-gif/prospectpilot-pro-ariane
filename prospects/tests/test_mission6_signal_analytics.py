from django.test import TestCase
from django.utils import timezone

from prospects.models import ContactLog, ConversionEvent, EmailSequence, EmailStep, EngagementEvent, ProspectSignal, RevenueAttribution
from prospects.services.signal_analytics import (
    conversion_rate_by_channel,
    conversion_rate_by_intent_band,
    mrr_by_channel,
    mrr_by_signal_type,
    signal_to_click_counts,
    signal_to_client_counts,
    signal_to_reply_counts,
    signal_to_signup_counts,
)
from prospects.services.signals import signal_fingerprint
from prospects.tests.factories import make_prospect, make_product


def _signal(prospect, signal_type, signal_group="fit"):
    return ProspectSignal.objects.create(
        prospect=prospect, signal_type=signal_type, category="analytics", signal_group=signal_group,
        source_kind="technology", label=signal_type, evidence="preuve", confidence=80,
        score_impact=5, positive=True, observed_at=timezone.now(),
        fingerprint=signal_fingerprint(signal_type, "", "preuve"),
    )


class SignalToReplyCountsTests(TestCase):
    def test_prospect_with_reply_counted_under_its_signal_type(self):
        prospect = make_prospect()
        _signal(prospect, "hiring_growth", "intent")
        ContactLog.objects.create(prospect=prospect, channel="email", outcome="replied")
        self.assertEqual(signal_to_reply_counts(), {"hiring_growth": 1})

    def test_prospect_without_reply_not_counted(self):
        prospect = make_prospect()
        _signal(prospect, "hiring_growth", "intent")
        self.assertEqual(signal_to_reply_counts(), {})


class SignalToClickCountsTests(TestCase):
    def test_counts_clicks_by_signal_type(self):
        prospect = make_prospect()
        _signal(prospect, "analytics_detected", "fit")
        EngagementEvent.objects.create(prospect=prospect, event_type="link_clicked", occurred_at=timezone.now())
        self.assertEqual(signal_to_click_counts(), {"analytics_detected": 1})


class SignalToSignupAndClientCountsTests(TestCase):
    def test_signup_counted_separately_from_client(self):
        prospect = make_prospect()
        _signal(prospect, "hiring_growth", "intent")
        ConversionEvent.objects.create(prospect=prospect, event_type="signup")
        self.assertEqual(signal_to_signup_counts(), {"hiring_growth": 1})
        self.assertEqual(signal_to_client_counts(), {})

    def test_paying_client_counted_under_client_counts(self):
        prospect = make_prospect()
        _signal(prospect, "hiring_growth", "intent")
        ConversionEvent.objects.create(prospect=prospect, event_type="paying")
        self.assertEqual(signal_to_client_counts(), {"hiring_growth": 1})

    def test_two_prospects_same_signal_type_are_both_counted(self):
        p1 = make_prospect(siret="40000000000001")
        p2 = make_prospect(siret="40000000000002")
        _signal(p1, "hiring_growth", "intent")
        _signal(p2, "hiring_growth", "intent")
        ConversionEvent.objects.create(prospect=p1, event_type="signup")
        ConversionEvent.objects.create(prospect=p2, event_type="signup")
        self.assertEqual(signal_to_signup_counts(), {"hiring_growth": 2})


class ConversionRateByChannelTests(TestCase):
    def test_rate_computed_correctly(self):
        p1 = make_prospect(siret="40000000000010")
        p2 = make_prospect(siret="40000000000011")
        ContactLog.objects.create(prospect=p1, channel="email", outcome="sent")
        ContactLog.objects.create(prospect=p2, channel="email", outcome="sent")
        ConversionEvent.objects.create(prospect=p1, event_type="signup")

        rows = conversion_rate_by_channel()
        email_row = next(r for r in rows if r["channel"] == "email")
        self.assertEqual(email_row["contacted"], 2)
        self.assertEqual(email_row["converted"], 1)
        self.assertEqual(email_row["rate"], 50.0)

    def test_channel_with_no_contacts_is_absent(self):
        rows = conversion_rate_by_channel()
        self.assertEqual(rows, [])


class ConversionRateByIntentBandTests(TestCase):
    def test_prospects_bucketed_by_intent_score(self):
        low = make_prospect(siret="40000000000020")
        low.intent_score = 10
        low.save(update_fields=["intent_score"])

        high = make_prospect(siret="40000000000021")
        high.intent_score = 85
        high.save(update_fields=["intent_score"])
        ConversionEvent.objects.create(prospect=high, event_type="signup")

        rows = {r["band"]: r for r in conversion_rate_by_intent_band()}
        self.assertEqual(rows["strong"]["prospects"], 1)
        self.assertEqual(rows["strong"]["converted"], 1)
        self.assertEqual(rows["strong"]["rate"], 100.0)
        self.assertEqual(rows["no_signal"]["prospects"], 1)  # low.intent_score=10 falls in "no_signal" (0-19)
        self.assertEqual(rows["no_signal"]["converted"], 0)

    def test_empty_band_has_zero_rate_not_a_crash(self):
        rows = conversion_rate_by_intent_band()
        for row in rows:
            self.assertEqual(row["prospects"], 0)
            self.assertEqual(row["rate"], 0)


class MrrBySignalTypeTests(TestCase):
    def test_mrr_attributed_to_each_distinct_signal_type_once(self):
        prospect = make_prospect()
        _signal(prospect, "hiring_growth", "intent")
        _signal(prospect, "analytics_detected", "fit")
        conversion = ConversionEvent.objects.create(prospect=prospect, event_type="paying")
        RevenueAttribution.objects.create(conversion_event=conversion, prospect=prospect, mrr=49)

        result = mrr_by_signal_type()
        self.assertEqual(result["hiring_growth"], 49)
        self.assertEqual(result["analytics_detected"], 49)

    def test_no_revenue_returns_empty_dict(self):
        self.assertEqual(mrr_by_signal_type(), {})


class MrrByChannelTests(TestCase):
    def test_mrr_grouped_by_email_step_channel(self):
        product = make_product()
        sequence = EmailSequence.objects.create(product=product, name="seq")
        linkedin_step = EmailStep.objects.create(sequence=sequence, order=1, channel="linkedin_connect")
        email_step = EmailStep.objects.create(sequence=sequence, order=2, channel="email")

        prospect_a = make_prospect(siret="40000000000030")
        conversion_a = ConversionEvent.objects.create(prospect=prospect_a, event_type="paying")
        RevenueAttribution.objects.create(conversion_event=conversion_a, prospect=prospect_a, mrr=30, email_step=linkedin_step)

        prospect_b = make_prospect(siret="40000000000031")
        conversion_b = ConversionEvent.objects.create(prospect=prospect_b, event_type="paying")
        RevenueAttribution.objects.create(conversion_event=conversion_b, prospect=prospect_b, mrr=70, email_step=email_step)

        rows = {r["channel"]: r for r in mrr_by_channel()}
        self.assertEqual(rows["linkedin_connect"]["mrr"], 30)
        self.assertEqual(rows["email"]["mrr"], 70)

    def test_no_email_step_falls_under_unknown_channel(self):
        prospect = make_prospect()
        conversion = ConversionEvent.objects.create(prospect=prospect, event_type="paying")
        RevenueAttribution.objects.create(conversion_event=conversion, prospect=prospect, mrr=20)
        rows = {r["channel"]: r for r in mrr_by_channel()}
        self.assertEqual(rows["inconnu"]["mrr"], 20)
