from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from prospects.models import ContactLog, ContactPerson, ProspectSignal, Suppression
from prospects.services.next_best_action import compute_next_best_action
from prospects.services.signals import signal_fingerprint
from prospects.tests.factories import make_prospect


def _intent_signal(prospect, signal_type, days_ago, now, score_impact=10, label=None):
    return ProspectSignal.objects.create(
        prospect=prospect, signal_type=signal_type, category="growth", signal_group="intent",
        source_kind="open_web", label=label or f"Signal {signal_type}", evidence=f"preuve {signal_type}",
        confidence=75, score_impact=score_impact, positive=True,
        observed_at=now - timedelta(days=days_ago),
        fingerprint=signal_fingerprint(signal_type, "", f"preuve {signal_type}"),
    )


class NextBestActionTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def test_do_not_contact_status_gives_stop(self):
        prospect = make_prospect(status="do_not_contact")
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "STOP")

    def test_active_suppression_gives_stop(self):
        prospect = make_prospect()
        Suppression.objects.create(prospect=prospect, active=True, reason="test")
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "STOP")

    def test_replied_contact_log_gives_follow_up(self):
        prospect = make_prospect()
        ContactLog.objects.create(prospect=prospect, channel="email", outcome="replied")
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "FOLLOW_UP")

    def test_optout_contact_log_gives_stop(self):
        prospect = make_prospect()
        ContactLog.objects.create(prospect=prospect, channel="email", outcome="optout")
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "STOP")

    def test_very_recent_contact_without_reply_gives_wait(self):
        prospect = make_prospect()
        log = ContactLog.objects.create(prospect=prospect, channel="email", outcome="sent")
        ContactLog.objects.filter(pk=log.pk).update(contacted_at=self.now - timedelta(hours=6))
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "WAIT")

    def test_high_intent_with_linkedin_and_no_prior_contact_gives_linkedin_connect(self):
        prospect = make_prospect()
        prospect.intent_score = 75
        prospect.save(update_fields=["intent_score"])
        ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont", job_title="Responsable Growth",
            profile_url="https://linkedin.com/in/alex-dupont", is_active=True,
        )
        _intent_signal(prospect, "hiring_growth", days_ago=3, now=self.now, label="Nouveau responsable Growth")
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "LINKEDIN_CONNECT")
        self.assertIn("Intent 75", result["reason"])
        self.assertIn("Nouveau responsable Growth", result["reason"])

    def test_high_intent_already_connected_on_linkedin_gives_linkedin_message(self):
        prospect = make_prospect()
        prospect.intent_score = 70
        prospect.save(update_fields=["intent_score"])
        ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont", profile_url="https://linkedin.com/in/alex-dupont", is_active=True,
        )
        log = ContactLog.objects.create(prospect=prospect, channel="linkedin", outcome="sent")
        ContactLog.objects.filter(pk=log.pk).update(contacted_at=self.now - timedelta(days=10))
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "LINKEDIN_MESSAGE")

    def test_high_intent_no_linkedin_but_email_gives_email(self):
        prospect = make_prospect()
        prospect.intent_score = 70
        prospect.save(update_fields=["intent_score"])
        from prospects.tests.factories import make_public_email
        make_public_email(prospect)
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "EMAIL")

    def test_high_intent_no_channel_gives_watch(self):
        prospect = make_prospect()
        prospect.intent_score = 70
        prospect.save(update_fields=["intent_score"])
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "WATCH")

    def test_emerging_intent_gives_watch(self):
        prospect = make_prospect()
        prospect.intent_score = 35
        prospect.save(update_fields=["intent_score"])
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "WATCH")

    def test_low_intent_good_fit_gives_nurture(self):
        prospect = make_prospect()
        prospect.intent_score = 0
        prospect.icp_fit_score = 70
        prospect.save(update_fields=["intent_score", "icp_fit_score"])
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "NURTURE")

    def test_low_intent_low_fit_gives_wait(self):
        prospect = make_prospect()
        prospect.intent_score = 0
        prospect.icp_fit_score = 20
        prospect.save(update_fields=["intent_score", "icp_fit_score"])
        result = compute_next_best_action(prospect, now=self.now)
        self.assertEqual(result["code"], "WAIT")

    def test_result_always_has_confidence_and_triggering_signal_keys(self):
        prospect = make_prospect()
        result = compute_next_best_action(prospect, now=self.now)
        self.assertIn("confidence", result)
        self.assertIn("triggering_signal", result)
        self.assertIn("reason", result)
        self.assertIn("code", result)
