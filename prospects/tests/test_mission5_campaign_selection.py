"""Mission 5, section 8 — campaign_create ne doit proposer que les prospects
volontairement sélectionnés (selected_for_prospecting=True), jamais un simple
candidat technique du pipeline non encore choisi, et doit expliquer clairement
pourquoi 0 prospect est contactable plutôt que de cacher silencieusement la liste."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .factories import make_icp, make_prospect, make_product


class CampaignCreateSelectionScopeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester", password="pw12345!")
        self.client.force_login(self.user)
        self.product = make_product()
        self.icp = make_icp(self.product)

    def test_unselected_technical_candidate_never_appears(self):
        make_prospect(
            name="Candidat technique non sélectionné",
            source="acquisition_pipeline_predictneed",
            selected_for_prospecting=False,
            predictneed_grade="A", outbound_eligible=True, predictneed_excluded=False,
            siret="",
        )
        response = self.client.get(reverse("campaign_create"), {"grade": "A"})
        self.assertEqual(list(response.context["prospects"]), [])

    def test_selected_and_eligible_prospect_appears(self):
        prospect = make_prospect(
            name="Prospect sélectionné", source="api_recherche_entreprises",
            selected_for_prospecting=True,
            predictneed_grade="A", outbound_eligible=True, predictneed_excluded=False,
            public_email="contact@prospect.example", siret="",
        )
        response = self.client.get(reverse("campaign_create"), {"grade": "A"})
        self.assertIn(prospect, list(response.context["prospects"]))

    def test_empty_state_explains_why_zero_contactable(self):
        make_prospect(
            name="Sélectionné mais sans email", source="api_recherche_entreprises",
            selected_for_prospecting=True, public_email="",
            predictneed_grade="A", outbound_eligible=False, predictneed_excluded=False,
            siret="",
        )
        response = self.client.get(reverse("campaign_create"), {"grade": "A"})
        reasons = response.context["empty_state_reasons"]
        self.assertIsNotNone(reasons)
        self.assertEqual(reasons["total_selected"], 1)
        self.assertEqual(reasons["without_email"], 1)
        self.assertContains(response, "sans e-mail")

    def test_empty_state_when_nothing_selected_at_all(self):
        response = self.client.get(reverse("campaign_create"), {"grade": "A"})
        reasons = response.context["empty_state_reasons"]
        self.assertEqual(reasons["total_selected"], 0)
        self.assertContains(response, "Trouver des prospects")
