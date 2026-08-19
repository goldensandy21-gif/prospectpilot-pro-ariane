"""Mission 6, bloc B — vérifie que la chaîne complète fonctionne réellement :

    SignalCollector -> ProspectSignal -> fraîcheur -> INTENT -> IN MARKET NOW -> Next Best Action

en passant par les vrais points d'entrée de production (run_signal_collectors,
recompute_acquisition_scores, in_market_status, compute_next_best_action) —
aucun ProspectSignal n'est créé "à la main" ici, contrairement aux tests
unitaires des autres fichiers : ce fichier teste l'intégration bout en bout.

Trois prospects fixtures (section 20) :
- A : bon FIT, aucun signal récent -> attendu WAIT/NURTURE, aucune action concrète.
- B : bon FIT + signaux récents (quick scan) -> attendu INTENT élevé, action concrète.
- C : B + engagement PredictNeed réel -> attendu ENGAGEMENT nettement supérieur à B.
"""
from django.test import TestCase
from django.utils import timezone

from prospects.models import CompanySearchRun, EngagementEvent, SearchCandidate
from prospects.services.acquisition_scores import recompute_acquisition_scores
from prospects.services.in_market_status import in_market_status
from prospects.services.next_best_action import compute_next_best_action
from prospects.services.signal_collectors import QuickScanSignalCollector, run_signal_collectors
from prospects.tests.factories import make_prospect, make_public_email

RICH_QUICK_SCAN = {
    "pages_checked": 6,
    "worth_full_analysis": True,
    "business_type": "agence",
    "has_contact_form": True,
    "has_booking": True,
    "has_signup": True,
    "has_landing_pages": True,
    "has_lead_magnet": True,
}


def _attach_quick_scan(prospect, quick_scan_data):
    run = CompanySearchRun.objects.create(mode="manual")
    SearchCandidate.objects.create(
        search_run=run, siren="123456789", name=prospect.name,
        prospect=prospect, quick_scan_data=quick_scan_data, status="scanned",
    )


class SignalIntelligenceChainEndToEndTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def _build_prospect_a(self):
        """Bon FIT, aucun signal récent."""
        prospect = make_prospect(name="Prospect A", siret="20000000000001")
        prospect.icp_fit_score = 75
        prospect.save(update_fields=["icp_fit_score"])
        return prospect

    def _build_prospect_b(self, name="Prospect B", siret="20000000000002"):
        """Bon FIT + signaux récents détectés via le vrai pipeline de collecte
        (quick scan riche en indicateurs de conversion/croissance)."""
        prospect = make_prospect(name=name, siret=siret)
        prospect.icp_fit_score = 78
        prospect.save(update_fields=["icp_fit_score"])
        make_public_email(prospect, email=f"contact@{siret}.example")
        _attach_quick_scan(prospect, RICH_QUICK_SCAN)
        saved, errors = run_signal_collectors(prospect, collectors=[QuickScanSignalCollector()])
        self.assertEqual(errors, [])
        self.assertGreater(len(saved), 0)
        return prospect

    def _build_prospect_c(self):
        """B + engagement PredictNeed réel (visite + simulateur terminé)."""
        prospect = self._build_prospect_b(name="Prospect C", siret="20000000000003")
        EngagementEvent.objects.create(prospect=prospect, event_type="product_visited", occurred_at=self.now)
        EngagementEvent.objects.create(prospect=prospect, event_type="simulator_completed", occurred_at=self.now)
        return prospect

    def test_prospect_a_has_no_intent_and_no_concrete_action(self):
        prospect = self._build_prospect_a()
        result = recompute_acquisition_scores(prospect, now=self.now)
        self.assertEqual(result["intent_score"], 0)
        self.assertEqual(result["engagement_score"], 0)

        status = in_market_status(prospect)
        self.assertEqual(status["code"], "no_signal")

        nba = compute_next_best_action(prospect, now=self.now)
        self.assertIn(nba["code"], {"WAIT", "NURTURE"})

    def test_prospect_b_has_real_intent_signals_and_a_concrete_action(self):
        prospect = self._build_prospect_b()
        result = recompute_acquisition_scores(prospect, now=self.now)
        self.assertGreater(result["intent_score"], 40)
        self.assertEqual(result["engagement_score"], 0)

        status = in_market_status(prospect)
        self.assertIn(status["code"], {"emerging", "probable", "strong"})
        self.assertNotIn("veut acheter", status["phrase"].lower())

        nba = compute_next_best_action(prospect, now=self.now)
        self.assertIn(nba["code"], {"EMAIL", "LINKEDIN_CONNECT", "WATCH"})

    def test_prospect_c_has_strictly_higher_engagement_than_b(self):
        prospect_b = self._build_prospect_b()
        result_b = recompute_acquisition_scores(prospect_b, now=self.now)

        prospect_c = self._build_prospect_c()
        result_c = recompute_acquisition_scores(prospect_c, now=self.now)

        self.assertEqual(result_b["engagement_score"], 0)
        self.assertGreater(result_c["engagement_score"], result_b["engagement_score"])

    def test_a_b_c_are_clearly_and_monotonically_differentiated(self):
        """Le cœur de la section 20 : un utilisateur doit pouvoir distinguer
        A/B/C d'un coup d'œil sur intent_score puis engagement_score."""
        prospect_a = self._build_prospect_a()
        prospect_b = self._build_prospect_b()
        prospect_c = self._build_prospect_c()

        result_a = recompute_acquisition_scores(prospect_a, now=self.now)
        result_b = recompute_acquisition_scores(prospect_b, now=self.now)
        result_c = recompute_acquisition_scores(prospect_c, now=self.now)

        self.assertEqual(result_a["intent_score"], 0)
        self.assertLess(result_a["intent_score"], result_b["intent_score"])
        self.assertLessEqual(result_b["intent_score"], result_c["intent_score"] + 1)

        self.assertEqual(result_a["engagement_score"], 0)
        self.assertEqual(result_b["engagement_score"], 0)
        self.assertGreater(result_c["engagement_score"], 0)

        nba_a = compute_next_best_action(prospect_a, now=self.now)
        nba_c = compute_next_best_action(prospect_c, now=self.now)
        self.assertIn(nba_a["code"], {"WAIT", "NURTURE"})
        self.assertNotIn(nba_c["code"], {"WAIT", "NURTURE"})
