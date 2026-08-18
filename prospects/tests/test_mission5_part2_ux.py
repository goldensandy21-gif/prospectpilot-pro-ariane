"""Mission 5, partie 2 — parcours unifié Recherche → Sélection → Prospects →
Campagne → Aperçu PredictNeed, menu simplifié, Dashboard basé sur le score
PredictNeed, absence de l'ancien e-mail ProspectPilot dans le NOUVEAU parcours
(campaign_preview) — l'ancien outil reste dispo mais secondaire sur la fiche."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from prospects.models import CompanySearchRun, Prospect, SearchCandidate
from .factories import (
    make_campaign, make_campaign_prospect, make_icp, make_prospect, make_product,
    make_public_email,
)


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester", password="pw12345!")
        self.client.force_login(self.user)


class NoDuplicateSelectionTests(LoggedInTestCase):
    def setUp(self):
        super().setUp()
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.search_run = CompanySearchRun.objects.create(
            mode="acquisition", product=self.product, icp=self.icp, status="done",
        )

    def test_add_to_prospects_selects_existing_prospect_without_duplicate(self):
        prospect = make_prospect(name="Déjà créé techniquement", source="acquisition_pipeline_predictneed", siret="")
        candidate = SearchCandidate.objects.create(
            search_run=self.search_run, siren="111222333", name="Déjà créé techniquement",
            status="converted", prospect=prospect,
        )
        total_before = Prospect.objects.count()

        response = self.client.post(
            reverse("acquisition_search_run_detail", args=[self.search_run.pk]),
            {"selected": [str(candidate.pk)]},
        )

        self.assertEqual(Prospect.objects.count(), total_before)  # aucun doublon
        prospect.refresh_from_db()
        self.assertTrue(prospect.selected_for_prospecting)
        self.assertIsNotNone(prospect.selected_at)

    def test_candidate_without_prospect_yet_is_not_silently_added(self):
        candidate = SearchCandidate.objects.create(
            search_run=self.search_run, siren="999888777", name="Pas encore de site", status="preselected",
        )
        response = self.client.post(
            reverse("acquisition_search_run_detail", args=[self.search_run.pk]),
            {"selected": [str(candidate.pk)]},
        )
        self.assertEqual(Prospect.objects.count(), 0)

    def test_selecting_twice_does_not_duplicate_or_error(self):
        prospect = make_prospect(name="Deux fois", source="acquisition_pipeline_predictneed", siret="")
        candidate = SearchCandidate.objects.create(
            search_run=self.search_run, siren="555444333", name="Deux fois",
            status="converted", prospect=prospect,
        )
        self.client.post(reverse("acquisition_search_run_detail", args=[self.search_run.pk]), {"selected": [str(candidate.pk)]})
        self.client.post(reverse("acquisition_search_run_detail", args=[self.search_run.pk]), {"selected": [str(candidate.pk)]})
        self.assertEqual(Prospect.objects.filter(name="Deux fois").count(), 1)


class ProspectListVisibilityTests(LoggedInTestCase):
    def test_historical_prospect_visible_by_default(self):
        make_prospect(name="Historique", source="api_recherche_entreprises", selected_for_prospecting=True, siret="")
        response = self.client.get(reverse("prospect_list"))
        self.assertContains(response, "Historique")

    def test_unselected_technical_candidate_not_visible_by_default(self):
        make_prospect(name="Technique non retenu", source="acquisition_pipeline_predictneed", selected_for_prospecting=False, siret="")
        response = self.client.get(reverse("prospect_list"))
        self.assertNotContains(response, "Technique non retenu")

    def test_with_email_filter(self):
        make_prospect(name="Avec email", selected_for_prospecting=True, public_email="a@b.example", siret="")
        make_prospect(name="Sans email", selected_for_prospecting=True, public_email="", siret="")
        response = self.client.get(reverse("prospect_list"), {"filter": "with_email"})
        self.assertContains(response, "Avec email")
        self.assertNotContains(response, "Sans email")

    def test_grade_a_filter(self):
        make_prospect(name="Grade A", selected_for_prospecting=True, predictneed_grade="A", siret="")
        make_prospect(name="Grade B", selected_for_prospecting=True, predictneed_grade="B", siret="")
        response = self.client.get(reverse("prospect_list"), {"filter": "A"})
        self.assertContains(response, "Grade A")
        self.assertNotContains(response, "Grade B")


class CampaignCreateFromSelectionTests(LoggedInTestCase):
    def setUp(self):
        super().setUp()
        self.product = make_product()
        self.icp = make_icp(self.product)

    def test_prospects_query_param_restricts_selection(self):
        p1 = make_prospect(
            name="Choisi", selected_for_prospecting=True, predictneed_grade="A",
            outbound_eligible=True, predictneed_excluded=False, predictneed_icp=self.icp, siret="",
        )
        p2 = make_prospect(
            name="Pas choisi", selected_for_prospecting=True, predictneed_grade="A",
            outbound_eligible=True, predictneed_excluded=False, predictneed_icp=self.icp, siret="",
        )
        response = self.client.get(reverse("campaign_create"), {"grade": "A", "prospects": str(p1.pk)})
        prospects_shown = list(response.context["prospects"])
        self.assertIn(p1, prospects_shown)
        self.assertNotIn(p2, prospects_shown)

    def test_abc_grade_and_custom_threshold(self):
        make_prospect(
            name="Grade C haut score", selected_for_prospecting=True, predictneed_grade="C",
            outbound_eligible=True, predictneed_excluded=False, predictneed_acquisition_score=55, siret="",
        )
        response = self.client.get(reverse("campaign_create"), {"grade": "ABC", "min_score": "50"})
        self.assertContains(response, "Grade C haut score")


class DashboardUsesPredictNeedScoreTests(LoggedInTestCase):
    def test_dashboard_shows_predictneed_kpis_not_priority_score_label(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Prospects retenus")
        self.assertContains(response, "Prêts à contacter")
        self.assertContains(response, "MRR attribué")
        self.assertNotContains(response, "Score moyen")  # ancien KPI priority_score

    def test_dashboard_priority_prospects_use_predictneed_grade(self):
        make_prospect(
            name="Priorité PredictNeed", selected_for_prospecting=True,
            predictneed_grade="A", predictneed_excluded=False,
            predictneed_acquisition_score=91, siret="",
        )
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Priorité PredictNeed")


class SimplifiedMenuTests(LoggedInTestCase):
    def test_menu_has_the_six_main_entries(self):
        response = self.client.get(reverse("dashboard"))
        for label in ["Dashboard", "Trouver des prospects", "Prospects", "Campagnes", "Résultats", "Réglages"]:
            self.assertContains(response, label)

    def test_no_ambiguous_global_header_buttons(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "data-history-back")
        content = response.content.decode()
        # "Ajouter" ne doit plus être un bouton global du header (hors page Prospects elle-même).
        self.assertNotIn('href="/prospects/new/">Ajouter<', content)


class CampaignPreviewNoLegacyBrandingTests(LoggedInTestCase):
    def setUp(self):
        super().setUp()
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.campaign = make_campaign(self.product, self.icp, status="active")
        self.prospect = make_prospect(selected_for_prospecting=True, siret="")
        make_public_email(self.prospect)
        self.member = make_campaign_prospect(self.campaign, self.prospect)

    def test_no_prospectpilot_branding_in_predictneed_preview(self):
        response = self.client.get(reverse("campaign_preview", args=[self.campaign.pk]), {"cp": self.member.pk})
        content = response.content.decode()
        self.assertNotIn("Découvrir ProspectPilot Pro", content)
        self.assertNotIn("unsplash", content.lower())
        self.assertNotIn("Lyon, France", content)

    def test_preview_shows_all_required_fields(self):
        response = self.client.get(reverse("campaign_preview", args=[self.campaign.pk]), {"cp": self.member.pk})
        for label in ["Expéditeur", "Reply-To", "Destinataire", "Objet", "CTA", "Footer conformité", "Désinscription"]:
            self.assertContains(response, label)
