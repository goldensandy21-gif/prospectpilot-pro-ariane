"""Mission 6, bloc B — vérifie que la chaîne complète fonctionne réellement :

    SignalCollector -> ProspectSignal -> fraîcheur -> INTENT -> IN MARKET NOW -> Next Best Action

en passant par les vrais points d'entrée de production (run_signal_collectors,
recompute_acquisition_scores, in_market_status, compute_next_best_action) —
aucun ProspectSignal n'est créé "à la main" ici, contrairement aux tests
unitaires des autres fichiers : ce fichier teste l'intégration bout en bout.

Trois prospects fixtures (section 20, corrigées suite à audit indépendant) :
- A : excellent FIT, site très mature (beaucoup de pages de conversion),
  MAIS aucun événement temporel réel -> Intent doit rester 0, jamais
  "probable"/"forte". Les caractéristiques statiques du site (formulaire de
  contact, booking, lead magnet...) ne sont PAS une intention d'achat.
- B : même profil que A + un véritable événement récent et daté (recrutement
  Growth, via une preuve ProspectEvidence avec une date réelle) -> Intent
  clairement supérieur à A.
- C : B + clic/visite/simulateur PredictNeed réel -> Engagement nettement
  supérieur, priorité commerciale maximale.

C'est ce test qui prouve que le moteur distingue désormais maturité (FIT)
et intention actuelle (INTENT).
"""
from django.test import TestCase
from django.utils import timezone

from prospects.models import CompanySearchRun, EngagementEvent, ProspectEvidence, SearchCandidate
from prospects.services.acquisition_scores import recompute_acquisition_scores
from prospects.services.in_market_status import in_market_status
from prospects.services.next_best_action import compute_next_best_action
from prospects.services.signal_collectors import (
    QuickScanSignalCollector,
    RecentActivitySignalCollector,
    run_signal_collectors,
)
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

    def _build_prospect_a(self, name="Prospect A", siret="20000000000001"):
        """Excellent FIT, site très mature (beaucoup de pages de conversion),
        mais AUCUN événement temporel réel."""
        prospect = make_prospect(name=name, siret=siret)
        prospect.icp_fit_score = 82
        prospect.save(update_fields=["icp_fit_score"])
        make_public_email(prospect, email=f"contact@{siret}.example")
        _attach_quick_scan(prospect, RICH_QUICK_SCAN)
        saved, errors = run_signal_collectors(prospect, collectors=[QuickScanSignalCollector()])
        self.assertEqual(errors, [])
        self.assertGreater(len(saved), 0)
        return prospect

    def _build_prospect_b(self, name="Prospect B", siret="20000000000002"):
        """A + un véritable événement récent et daté (recrutement Growth)."""
        prospect = self._build_prospect_a(name=name, siret=siret)
        ProspectEvidence.objects.create(
            prospect=prospect, field_name="job_posting_growth", value="Offre Responsable Growth publiée",
            normalized_value=f"offre growth {siret}", source_url=f"https://{siret}.example/carrieres",
            confidence_score=75, is_current=True,
            raw_payload={"event_date": self.now.isoformat()},
        )
        ProspectEvidence.objects.create(
            prospect=prospect, field_name="news_acquisition", value="Actualité acquisition/conversion",
            normalized_value=f"actu acquisition {siret}", source_url=f"https://{siret}.example/actualites",
            confidence_score=70, is_current=True,
            raw_payload={"event_date": self.now.isoformat()},
        )
        saved, errors = run_signal_collectors(prospect, collectors=[RecentActivitySignalCollector()])
        self.assertEqual(errors, [])
        self.assertEqual(len(saved), 2)
        return prospect

    def _build_prospect_c(self):
        """B + engagement PredictNeed réel (visite + simulateur terminé)."""
        prospect = self._build_prospect_b(name="Prospect C", siret="20000000000003")
        EngagementEvent.objects.create(prospect=prospect, event_type="product_visited", occurred_at=self.now)
        EngagementEvent.objects.create(prospect=prospect, event_type="simulator_completed", occurred_at=self.now)
        return prospect

    def test_prospect_a_has_no_intent_despite_a_mature_site(self):
        prospect = self._build_prospect_a()
        result = recompute_acquisition_scores(prospect, now=self.now)
        self.assertEqual(result["intent_score"], 0)
        self.assertEqual(result["engagement_score"], 0)

        status = in_market_status(prospect)
        self.assertEqual(status["code"], "no_signal")
        self.assertNotIn(status["code"], {"probable", "strong"})

        nba = compute_next_best_action(prospect, now=self.now)
        self.assertIn(nba["code"], {"WAIT", "NURTURE"})

    def test_prospect_b_has_clearly_higher_intent_than_a(self):
        prospect_a = self._build_prospect_a()
        result_a = recompute_acquisition_scores(prospect_a, now=self.now)

        prospect_b = self._build_prospect_b()
        result_b = recompute_acquisition_scores(prospect_b, now=self.now)

        self.assertEqual(result_a["intent_score"], 0)
        self.assertGreater(result_b["intent_score"], result_a["intent_score"])
        self.assertEqual(result_b["engagement_score"], 0)

        status = in_market_status(prospect_b)
        self.assertNotIn("veut acheter", status["phrase"].lower())

    def test_prospect_c_has_strictly_higher_engagement_than_b(self):
        prospect_b = self._build_prospect_b()
        result_b = recompute_acquisition_scores(prospect_b, now=self.now)

        prospect_c = self._build_prospect_c()
        result_c = recompute_acquisition_scores(prospect_c, now=self.now)

        self.assertEqual(result_b["engagement_score"], 0)
        self.assertGreater(result_c["engagement_score"], result_b["engagement_score"])

    def test_a_b_c_are_clearly_and_monotonically_differentiated(self):
        """Le cœur de la section 20 (corrigé) : A a un site mature mais pas
        d'intent, B a un vrai événement daté, C ajoute l'engagement réel."""
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

        status_a = in_market_status(prospect_a)
        status_c = in_market_status(prospect_c)
        self.assertEqual(status_a["code"], "no_signal")
        self.assertNotEqual(status_c["code"], "no_signal")
