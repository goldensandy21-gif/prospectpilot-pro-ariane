"""Mission 6, bloc 7 (correctif d'audit) — "Priorité" (predictneed_acquisition_score)
doit réellement incorporer Intent/Engagement, pas seulement les composantes
pré-Mission-6. Un seul score canonique (pas un cinquième concurrent) :
Intent/Engagement en sont des composantes pondérées."""
from django.test import TestCase
from django.utils import timezone

from prospects.models import EngagementEvent, ProspectSignal
from prospects.services.predictneed_scoring import score_prospect
from prospects.services.signals import signal_fingerprint
from prospects.tests.factories import make_icp, make_product, make_prospect, make_public_email


def _intent_signal(prospect, signal_type, now, score_impact=10):
    return ProspectSignal.objects.create(
        prospect=prospect, signal_type=signal_type, category="timing", signal_group="intent",
        source_kind="open_web", label=f"Signal {signal_type}", evidence="preuve",
        confidence=75, score_impact=score_impact, positive=True, observed_at=now,
        fingerprint=signal_fingerprint(signal_type, "", "preuve"),
    )


class PriorityIncorporatesIntentAndEngagementTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.now = timezone.now()

    def test_two_otherwise_identical_prospects_differ_by_intent(self):
        low = make_prospect(name="Sans intent", siret="80000000000001")
        make_public_email(low)
        result_low = score_prospect(low, icp=self.icp, product=self.product)

        high = make_prospect(name="Avec intent", siret="80000000000002")
        make_public_email(high)
        _intent_signal(high, "hiring_growth", self.now, score_impact=10)
        _intent_signal(high, "news_acquisition", self.now, score_impact=10)
        result_high = score_prospect(high, icp=self.icp, product=self.product)

        self.assertGreater(result_high["predictneed_acquisition_score"], result_low["predictneed_acquisition_score"])
        self.assertGreater(high.intent_score, 0)

    def test_two_otherwise_identical_prospects_differ_by_engagement(self):
        low = make_prospect(name="Sans engagement", siret="80000000000003")
        make_public_email(low)
        result_low = score_prospect(low, icp=self.icp, product=self.product)

        high = make_prospect(name="Avec engagement", siret="80000000000004")
        make_public_email(high)
        EngagementEvent.objects.create(prospect=high, event_type="simulator_completed", occurred_at=self.now)
        result_high = score_prospect(high, icp=self.icp, product=self.product)

        self.assertGreater(result_high["predictneed_acquisition_score"], result_low["predictneed_acquisition_score"])
        self.assertGreater(high.engagement_score, 0)

    def test_score_prospect_refreshes_intent_before_reading_it(self):
        """score_prospect() ne doit jamais utiliser un intent_score obsolète :
        même appelé juste après la création d'un nouveau signal, le score
        canonique doit refléter l'état actuel."""
        prospect = make_prospect()
        make_public_email(prospect)
        _intent_signal(prospect, "hiring_growth", self.now, score_impact=10)

        self.assertEqual(prospect.intent_score, 0)  # pas encore recalculé
        score_prospect(prospect, icp=self.icp, product=self.product)
        self.assertGreater(prospect.intent_score, 0)  # rafraîchi par score_prospect()

    def test_score_reasons_mention_intent_and_engagement(self):
        prospect = make_prospect()
        make_public_email(prospect)
        result = score_prospect(prospect, icp=self.icp, product=self.product)
        reasons_text = " ".join(result["predictneed_score_reasons"])
        self.assertIn("Intent", reasons_text)
        self.assertIn("Engagement", reasons_text)

    def test_existing_icp_weights_without_intent_engagement_keys_still_work(self):
        """Un ICPProfile créé avant ce correctif (weights sans "intent"/
        "engagement") doit hériter des valeurs par défaut, sans migration,
        sans lever d'exception."""
        self.icp.weights = {"icp_fit": 40, "need": 30, "acquisition_maturity": 15, "contactability": 10, "timing": 5}
        self.icp.save(update_fields=["weights"])
        prospect = make_prospect()
        make_public_email(prospect)
        result = score_prospect(prospect, icp=self.icp, product=self.product)
        self.assertGreater(result["predictneed_acquisition_score"], 0)
        weights = self.icp.effective_weights()
        self.assertIn("intent", weights)
        self.assertIn("engagement", weights)
        self.assertAlmostEqual(sum(weights.values()), 100, delta=1)
