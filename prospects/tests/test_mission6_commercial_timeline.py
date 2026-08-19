from django.test import TestCase
from django.utils import timezone

from prospects.models import AgentBrief, ContactLog, ProspectSignal
from prospects.services.commercial_timeline import build_prospect_timeline
from prospects.services.signals import signal_fingerprint
from prospects.tests.factories import make_prospect


class CommercialTimelineMission6Tests(TestCase):
    def setUp(self):
        self.prospect = make_prospect()
        self.now = timezone.now()

    def test_signal_detection_appears_in_timeline(self):
        ProspectSignal.objects.create(
            prospect=self.prospect, signal_type="hiring_growth", category="growth",
            signal_group="intent", source_kind="open_web", label="Recrutement Growth",
            evidence="preuve", confidence=70, score_impact=8, positive=True,
            observed_at=self.now, fingerprint=signal_fingerprint("hiring_growth", "", "preuve"),
        )
        entries = build_prospect_timeline(self.prospect)
        labels = [e["label"] for e in entries]
        self.assertTrue(any("Recrutement Growth" in label for label in labels))

    def test_linkedin_invitation_appears_in_timeline(self):
        ContactLog.objects.create(
            prospect=self.prospect, channel="linkedin", subject="Invitation LinkedIn",
            outcome="invitation_sent",
        )
        entries = build_prospect_timeline(self.prospect)
        labels = [e["label"] for e in entries]
        self.assertIn("Invitation LinkedIn envoyée", labels)

    def test_score_recompute_appears_in_timeline(self):
        self.prospect.intent_score = 60
        self.prospect.engagement_score = 20
        self.prospect.scores_computed_at = self.now
        self.prospect.save(update_fields=["intent_score", "engagement_score", "scores_computed_at"])
        entries = build_prospect_timeline(self.prospect)
        labels = [e["label"] for e in entries]
        self.assertIn("Scores INTENT/ENGAGEMENT recalculés", labels)

    def test_notable_nba_appears_but_wait_does_not(self):
        AgentBrief.objects.create(prospect=self.prospect, next_best_action="EMAIL — Intent 70, contact disponible.")
        entries = build_prospect_timeline(self.prospect)
        labels = [e["label"] for e in entries]
        self.assertIn("Action recommandée : EMAIL", labels)

    def test_wait_nba_is_not_shown_to_avoid_flooding_timeline(self):
        AgentBrief.objects.create(prospect=self.prospect, next_best_action="WAIT — Rien à signaler.")
        entries = build_prospect_timeline(self.prospect)
        labels = [e["label"] for e in entries]
        self.assertFalse(any("WAIT" in label for label in labels))

    def test_timeline_stays_sorted_chronologically(self):
        ProspectSignal.objects.create(
            prospect=self.prospect, signal_type="hiring_growth", category="growth",
            signal_group="intent", source_kind="open_web", label="Recrutement Growth",
            evidence="preuve", confidence=70, score_impact=8, positive=True,
            observed_at=self.now, fingerprint=signal_fingerprint("hiring_growth", "", "preuve"),
        )
        self.prospect.scores_computed_at = self.now
        self.prospect.save(update_fields=["scores_computed_at"])
        entries = build_prospect_timeline(self.prospect)
        timestamps = [e["at"] for e in entries]
        self.assertEqual(timestamps, sorted(timestamps))
