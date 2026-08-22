from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from prospects.models import EngagementEvent, ProspectSignal
from prospects.services.acquisition_scores import recompute_acquisition_scores
from prospects.services.engagement_scoring import apply_engagement_score, compute_engagement_score
from prospects.services.intent_scoring import apply_intent_score, compute_intent_score
from prospects.services.signals import signal_fingerprint
from prospects.tests.factories import make_prospect


def _intent_signal(prospect, signal_type, days_ago, score_impact=10, now=None):
    now = now or timezone.now()
    return ProspectSignal.objects.create(
        prospect=prospect, signal_type=signal_type, category="growth", signal_group="intent",
        source_kind="open_web", label=f"Signal {signal_type}", evidence=f"preuve {signal_type}",
        confidence=75, score_impact=score_impact, positive=True,
        observed_at=now - timedelta(days=days_ago),
        fingerprint=signal_fingerprint(signal_type, "", f"preuve {signal_type}"),
    )


def _fit_signal(prospect, signal_type="analytics_detected"):
    return ProspectSignal.objects.create(
        prospect=prospect, signal_type=signal_type, category="analytics", signal_group="fit",
        source_kind="technology", label="Outil analytics détecté", evidence="preuve fit",
        confidence=80, score_impact=6, positive=True, observed_at=timezone.now(),
        fingerprint=signal_fingerprint(signal_type, "", "preuve fit"),
    )


class ComputeIntentScoreTests(TestCase):
    def setUp(self):
        self.prospect = make_prospect()
        self.now = timezone.now()

    def test_no_intent_signals_gives_zero_score(self):
        score, reasons = compute_intent_score(self.prospect, now=self.now)
        self.assertEqual(score, 0)
        self.assertIn("Aucun signal d'intention", reasons[0])

    def test_fit_only_signals_never_contribute_to_intent(self):
        """Un outil analytics détecté est un indice de FIT, jamais d'INTENT à
        lui seul (mission 6, section 5 — avertissement explicite)."""
        _fit_signal(self.prospect)
        score, reasons = compute_intent_score(self.prospect, now=self.now)
        self.assertEqual(score, 0)

    def test_single_recent_intent_signal_raises_score_above_base(self):
        _intent_signal(self.prospect, "hiring_growth", days_ago=1, now=self.now)
        score, reasons = compute_intent_score(self.prospect, now=self.now)
        self.assertGreater(score, 20)
        self.assertTrue(any("Signal hiring_growth" in r for r in reasons))

    def test_intent_signal_with_unknown_date_never_raises_the_score(self):
        """Correctif d'audit : intent + observed_at=None + detected_at="maintenant"
        (auto_now_add, ligne créée à l'instant) => intent_score ne monte pas —
        jamais de repli implicite sur la date de création de la ligne."""
        ProspectSignal.objects.create(
            prospect=self.prospect, signal_type="undated_intent", category="timing",
            signal_group="intent", source_kind="open_web", label="Signal sans date réelle",
            evidence="preuve", confidence=75, score_impact=10, positive=True,
            observed_at=None,
            fingerprint=signal_fingerprint("undated_intent", "", "preuve"),
        )
        score, reasons = compute_intent_score(self.prospect, now=self.now)
        self.assertEqual(score, 0)

    def test_repetition_of_recent_signals_scores_higher_than_single_signal(self):
        single_prospect = make_prospect(siret="11111111111111")
        _intent_signal(single_prospect, "hiring_growth", days_ago=1, now=self.now)
        single_score, _ = compute_intent_score(single_prospect, now=self.now)

        repeated_prospect = make_prospect(siret="22222222222222")
        _intent_signal(repeated_prospect, "hiring_growth", days_ago=1, now=self.now)
        _intent_signal(repeated_prospect, "new_marketing_tool", days_ago=2, now=self.now)
        repeated_score, reasons = compute_intent_score(repeated_prospect, now=self.now)

        self.assertGreater(repeated_score, single_score)
        self.assertTrue(any("bonus de répétition" in r for r in reasons))

    def test_old_intent_signal_contributes_less_than_recent_one(self):
        recent_prospect = make_prospect(siret="33333333333333")
        _intent_signal(recent_prospect, "hiring_growth", days_ago=1, score_impact=10, now=self.now)
        recent_score, _ = compute_intent_score(recent_prospect, now=self.now)

        old_prospect = make_prospect(siret="44444444444444")
        _intent_signal(old_prospect, "hiring_growth", days_ago=200, score_impact=10, now=self.now)
        old_score, old_reasons = compute_intent_score(old_prospect, now=self.now)

        self.assertGreater(recent_score, old_score)

    def test_apply_intent_score_persists_on_prospect(self):
        _intent_signal(self.prospect, "hiring_growth", days_ago=1, now=self.now)
        score, reasons = apply_intent_score(self.prospect, now=self.now)
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.intent_score, score)
        self.assertEqual(self.prospect.intent_score_reasons, reasons)
        self.assertIsNotNone(self.prospect.scores_computed_at)


class ComputeEngagementScoreTests(TestCase):
    def setUp(self):
        self.prospect = make_prospect()
        self.now = timezone.now()

    def test_no_events_gives_zero_score(self):
        score, reasons = compute_engagement_score(self.prospect, now=self.now)
        self.assertEqual(score, 0)
        self.assertIn("Aucun événement", reasons[0])

    def test_email_sent_alone_does_not_count_as_engagement(self):
        EngagementEvent.objects.create(
            prospect=self.prospect, event_type="email_sent", occurred_at=self.now,
        )
        score, _ = compute_engagement_score(self.prospect, now=self.now)
        self.assertEqual(score, 0)

    def test_simulator_completed_scores_higher_than_link_clicked(self):
        clicked_prospect = make_prospect(siret="55555555555555")
        EngagementEvent.objects.create(
            prospect=clicked_prospect, event_type="link_clicked", occurred_at=self.now,
        )
        clicked_score, _ = compute_engagement_score(clicked_prospect, now=self.now)

        completed_prospect = make_prospect(siret="66666666666666")
        EngagementEvent.objects.create(
            prospect=completed_prospect, event_type="simulator_completed", occurred_at=self.now,
        )
        completed_score, _ = compute_engagement_score(completed_prospect, now=self.now)

        self.assertGreater(completed_score, clicked_score)

    def test_old_event_contributes_less_than_recent_event(self):
        recent_prospect = make_prospect(siret="77777777777777")
        EngagementEvent.objects.create(
            prospect=recent_prospect, event_type="product_visited", occurred_at=self.now - timedelta(days=1),
        )
        recent_score, _ = compute_engagement_score(recent_prospect, now=self.now)

        old_prospect = make_prospect(siret="88888888888888")
        EngagementEvent.objects.create(
            prospect=old_prospect, event_type="product_visited", occurred_at=self.now - timedelta(days=200),
        )
        old_score, _ = compute_engagement_score(old_prospect, now=self.now)

        self.assertGreater(recent_score, old_score)

    def test_apply_engagement_score_persists_on_prospect(self):
        EngagementEvent.objects.create(
            prospect=self.prospect, event_type="signup_completed", occurred_at=self.now,
        )
        score, reasons = apply_engagement_score(self.prospect, now=self.now)
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.engagement_score, score)
        self.assertEqual(self.prospect.engagement_score_reasons, reasons)


class RecomputeAcquisitionScoresTests(TestCase):
    def test_recompute_writes_both_scores_in_one_call(self):
        prospect = make_prospect()
        now = timezone.now()
        _intent_signal(prospect, "hiring_growth", days_ago=1, now=now)
        EngagementEvent.objects.create(prospect=prospect, event_type="simulator_started", occurred_at=now)

        result = recompute_acquisition_scores(prospect, now=now)

        prospect.refresh_from_db()
        self.assertEqual(prospect.intent_score, result["intent_score"])
        self.assertEqual(prospect.engagement_score, result["engagement_score"])
        self.assertGreater(prospect.intent_score, 0)
        self.assertGreater(prospect.engagement_score, 0)
        self.assertEqual(result["fit_score"], prospect.icp_fit_score)


class ThreeFixtureProspectsScenarioTests(TestCase):
    """Mission 6, section 20 — A (bon fit, pas d'intent) / B (fit + signaux
    récents) / C (fit + signaux + engagement PredictNeed) doivent produire des
    scores clairement différenciés et compréhensibles."""

    def test_prospect_a_b_c_are_clearly_differentiated(self):
        now = timezone.now()

        prospect_a = make_prospect(name="Prospect A - bon fit sans intent", siret="10000000000001")
        prospect_a.icp_fit_score = 80
        prospect_a.save(update_fields=["icp_fit_score"])

        prospect_b = make_prospect(name="Prospect B - fit + signaux intent recents", siret="10000000000002")
        prospect_b.icp_fit_score = 82
        prospect_b.save(update_fields=["icp_fit_score"])
        _intent_signal(prospect_b, "hiring_growth", days_ago=2, score_impact=10, now=now)
        _intent_signal(prospect_b, "new_marketing_tool", days_ago=4, score_impact=8, now=now)

        prospect_c = make_prospect(name="Prospect C - fit + signaux + engagement", siret="10000000000003")
        prospect_c.icp_fit_score = 85
        prospect_c.save(update_fields=["icp_fit_score"])
        _intent_signal(prospect_c, "hiring_growth", days_ago=1, score_impact=10, now=now)
        _intent_signal(prospect_c, "new_marketing_tool", days_ago=2, score_impact=8, now=now)
        EngagementEvent.objects.create(prospect=prospect_c, event_type="product_visited", occurred_at=now)
        EngagementEvent.objects.create(prospect=prospect_c, event_type="simulator_completed", occurred_at=now)

        result_a = recompute_acquisition_scores(prospect_a, now=now)
        result_b = recompute_acquisition_scores(prospect_b, now=now)
        result_c = recompute_acquisition_scores(prospect_c, now=now)

        # A : bon fit mais aucune intention détectée.
        self.assertEqual(result_a["intent_score"], 0)
        self.assertEqual(result_a["engagement_score"], 0)

        # B : intent nettement supérieur à A, toujours aucun engagement réel.
        self.assertGreater(result_b["intent_score"], result_a["intent_score"])
        self.assertEqual(result_b["engagement_score"], 0)

        # C : intent comparable ou supérieur à B, et engagement strictement positif
        # (contrairement à A et B) — c'est ce qui doit le distinguer le plus nettement.
        self.assertGreaterEqual(result_c["intent_score"], result_b["intent_score"] - 5)
        self.assertGreater(result_c["engagement_score"], 0)
        self.assertGreater(result_c["engagement_score"], result_b["engagement_score"])
