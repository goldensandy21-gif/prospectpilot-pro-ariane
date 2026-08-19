from django.test import TestCase
from django.utils import timezone

from prospects.models import ProspectSignal
from prospects.services.signals import persist_signals, signal_fingerprint
from prospects.tests.factories import make_prospect


def _signal(prospect, signal_type="hiring_growth", value="", evidence="", **overrides):
    defaults = {
        "category": "growth", "signal_group": "intent", "source_kind": "open_web",
        "label": "Recrutement Growth", "value": value, "source_url": "https://example.com/jobs",
        "evidence": evidence, "confidence": 70, "score_impact": 8, "positive": True,
        "observed_at": timezone.now(),
    }
    defaults.update(overrides)
    return ProspectSignal(
        prospect=prospect, signal_type=signal_type,
        fingerprint=signal_fingerprint(signal_type, value, evidence),
        **defaults,
    )


class PersistSignalsDedupTests(TestCase):
    def setUp(self):
        self.prospect = make_prospect()

    def test_identical_redetection_refreshes_instead_of_duplicating(self):
        first = _signal(self.prospect, evidence="Offre publiée le 12/01.")
        persist_signals(self.prospect, [first])
        second = _signal(self.prospect, evidence="Offre publiée le 12/01.")
        persist_signals(self.prospect, [second])
        self.assertEqual(
            ProspectSignal.objects.filter(prospect=self.prospect, signal_type="hiring_growth").count(), 1,
        )

    def test_distinct_events_with_same_signal_type_are_both_kept(self):
        """Recrutement Growth en janvier puis à nouveau en août : deux
        événements réels distincts, jamais fusionnés (mission 6, section 3)."""
        january = _signal(self.prospect, evidence="Offre Growth publiée en janvier.")
        persist_signals(self.prospect, [january])
        august = _signal(self.prospect, evidence="Nouvelle offre Growth publiée en août.")
        persist_signals(self.prospect, [august])
        self.assertEqual(
            ProspectSignal.objects.filter(prospect=self.prospect, signal_type="hiring_growth").count(), 2,
        )

    def test_same_signal_type_different_source_kind_kept_distinct(self):
        website = _signal(self.prospect, evidence="preuve", source_kind="website")
        persist_signals(self.prospect, [website])
        linkedin = _signal(self.prospect, evidence="preuve", source_kind="linkedin")
        persist_signals(self.prospect, [linkedin])
        self.assertEqual(
            ProspectSignal.objects.filter(prospect=self.prospect, signal_type="hiring_growth").count(), 2,
        )

    def test_refresh_updates_last_checked_at_without_new_row(self):
        first = _signal(self.prospect, evidence="preuve stable")
        saved = persist_signals(self.prospect, [first])[0]
        original_checked = saved.last_checked_at
        second = _signal(self.prospect, evidence="preuve stable")
        refreshed = persist_signals(self.prospect, [second])[0]
        self.assertEqual(saved.pk, refreshed.pk)
        self.assertGreaterEqual(refreshed.last_checked_at, original_checked)
