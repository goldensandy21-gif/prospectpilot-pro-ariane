from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from prospects.services.signal_freshness import (
    signal_age_days,
    signal_effective_impact,
    signal_freshness,
)
from prospects.services.signals import persist_signals, signal_fingerprint
from prospects.tests.factories import make_prospect


class SignalAgeDaysTests(TestCase):
    def test_none_observed_at_returns_none(self):
        self.assertIsNone(signal_age_days(None))

    def test_age_computed_from_now(self):
        now = timezone.now()
        observed = now - timedelta(days=5)
        self.assertAlmostEqual(signal_age_days(observed, now=now), 5.0, places=6)

    def test_future_observed_at_clamped_to_zero(self):
        now = timezone.now()
        observed = now + timedelta(days=1)
        self.assertEqual(signal_age_days(observed, now=now), 0.0)


class SignalFreshnessTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def test_very_fresh_within_3_days(self):
        result = signal_freshness(self.now - timedelta(days=1), now=self.now)
        self.assertEqual(result["label"], "très frais")
        self.assertEqual(result["multiplier"], 1.0)

    def test_boundary_at_exactly_3_days_is_very_fresh(self):
        result = signal_freshness(self.now - timedelta(days=3), now=self.now)
        self.assertEqual(result["label"], "très frais")

    def test_strong_between_4_and_7_days(self):
        result = signal_freshness(self.now - timedelta(days=5), now=self.now)
        self.assertEqual(result["label"], "frais")
        self.assertEqual(result["multiplier"], 0.75)

    def test_medium_between_8_and_30_days(self):
        result = signal_freshness(self.now - timedelta(days=20), now=self.now)
        self.assertEqual(result["label"], "récent")
        self.assertEqual(result["multiplier"], 0.5)

    def test_weak_between_31_and_90_days(self):
        result = signal_freshness(self.now - timedelta(days=60), now=self.now)
        self.assertEqual(result["label"], "ancien")
        self.assertEqual(result["multiplier"], 0.2)

    def test_stale_beyond_90_days_is_not_zeroed_out_but_negligible(self):
        result = signal_freshness(self.now - timedelta(days=200), now=self.now)
        self.assertEqual(result["label"], "obsolète")
        self.assertEqual(result["multiplier"], 0.0)

    def test_unknown_date_returns_unknown_label_not_a_crash(self):
        result = signal_freshness(None, now=self.now)
        self.assertEqual(result["label"], "date inconnue")
        self.assertEqual(result["multiplier"], 0.0)


class SignalEffectiveImpactTests(TestCase):
    def setUp(self):
        self.prospect = make_prospect()
        self.now = timezone.now()

    def test_raw_score_impact_untouched_by_freshness(self):
        signal = persist_signals(self.prospect, [self._signal(days_ago=1, score_impact=8)])[0]
        raw_before = signal.score_impact
        signal_effective_impact(signal, now=self.now)
        signal.refresh_from_db()
        self.assertEqual(signal.score_impact, raw_before)

    def test_effective_impact_decreases_with_age_for_identical_raw_score(self):
        fresh = persist_signals(self.prospect, [self._signal(days_ago=1, score_impact=8, signal_type="a")])[0]
        old = persist_signals(self.prospect, [self._signal(days_ago=60, score_impact=8, signal_type="b")])[0]
        fresh_impact, _ = signal_effective_impact(fresh, now=self.now)
        old_impact, _ = signal_effective_impact(old, now=self.now)
        self.assertGreater(fresh_impact, old_impact)

    def test_negative_signal_effective_impact_is_negative(self):
        signal = persist_signals(self.prospect, [self._signal(days_ago=1, score_impact=6, positive=False)])[0]
        impact, _ = signal_effective_impact(signal, now=self.now)
        self.assertLess(impact, 0)

    def test_intent_signal_with_unknown_observed_at_never_falls_back_to_detected_at(self):
        """Correctif d'audit : un signal intent sans date réelle connue doit
        produire age_days=None, multiplier=0, impact=0 — même si detected_at
        (date de création de la ligne) est "maintenant"."""
        from prospects.models import ProspectSignal

        signal = ProspectSignal.objects.create(
            prospect=self.prospect, signal_type="undated_intent", category="timing",
            signal_group="intent", source_kind="open_web", label="Signal sans date réelle",
            evidence="preuve", confidence=70, score_impact=10, positive=True,
            observed_at=None,  # date réelle inconnue
            fingerprint=signal_fingerprint("undated_intent", "", "preuve"),
        )
        # detected_at (auto_now_add) vaut "maintenant" — ne doit jamais servir de repli.
        self.assertIsNotNone(signal.detected_at)

        impact, freshness = signal_effective_impact(signal, now=self.now)
        self.assertIsNone(freshness["age_days"])
        self.assertEqual(freshness["multiplier"], 0)
        self.assertEqual(impact, 0)

    def _signal(self, days_ago, score_impact, positive=True, signal_type="test_signal"):
        from prospects.models import ProspectSignal
        observed_at = self.now - timedelta(days=days_ago)
        return ProspectSignal(
            prospect=self.prospect, signal_type=signal_type, category="growth",
            signal_group="intent", source_kind="website", label="Signal test",
            value="", source_url="", evidence="preuve",
            confidence=70, score_impact=score_impact, positive=positive,
            observed_at=observed_at,
            fingerprint=signal_fingerprint(signal_type, "", "preuve"),
        )
