"""Mission 6, bloc G — la liste Prospects et la fiche prospect exposent
FIT/INTENT/ENGAGEMENT/PRIORITÉ, le statut IN MARKET, la NBA et les nouveaux
filtres, sans nouveau menu principal (voir base.html, inchangé)."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import EngagementEvent, ProspectSignal
from prospects.services.signals import signal_fingerprint
from prospects.tests.factories import make_prospect


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="x", is_staff=True)
        self.client.force_login(self.user)


class ProspectListMission6Tests(LoggedInTestCase):
    def setUp(self):
        super().setUp()
        self.prospect = make_prospect(selected_for_prospecting=True)
        self.prospect.intent_score = 70
        self.prospect.engagement_score = 20
        self.prospect.save(update_fields=["intent_score", "engagement_score"])

    def test_list_renders_new_columns(self):
        response = self.client.get(reverse("prospect_list"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        for header in ["FIT", "INTENT", "ENGAGEMENT", "Priorité", "Dernier signal", "Action recommandée"]:
            self.assertIn(header, content)

    def test_list_shows_computed_scores_for_a_prospect(self):
        response = self.client.get(reverse("prospect_list"))
        self.assertContains(response, self.prospect.name)
        self.assertContains(response, "70")  # intent_score

    def test_intent_min_filter_excludes_low_intent_prospects(self):
        low = make_prospect(name="Prospect faible intent", siret="50000000000001", selected_for_prospecting=True)
        response = self.client.get(reverse("prospect_list"), {"intent_min": "50"})
        self.assertContains(response, self.prospect.name)
        self.assertNotContains(response, low.name)

    def test_has_email_filter_excludes_prospects_without_email(self):
        no_email = make_prospect(name="Prospect sans email", siret="50000000000002", selected_for_prospecting=True, public_email="")
        response = self.client.get(reverse("prospect_list"), {"has_email": "1"})
        self.assertNotContains(response, no_email.name)

    def test_nba_filter_only_shows_matching_prospects(self):
        self.prospect.icp_fit_score = 20
        self.prospect.intent_score = 0
        self.prospect.save(update_fields=["icp_fit_score", "intent_score"])
        response = self.client.get(reverse("prospect_list"), {"nba": "WAIT"})
        self.assertContains(response, self.prospect.name)

    def test_default_sort_is_by_predictneed_acquisition_score_not_legacy_priority_score(self):
        """Correctif d'audit (section 7) : le tri par défaut doit refléter
        le score canonique "Priorité" (qui incorpore Intent/Engagement),
        jamais l'ancien priority_score technique hérité de Meta.ordering."""
        high = make_prospect(name="Haute priorite", siret="90000000000001", selected_for_prospecting=True)
        high.predictneed_acquisition_score = 90
        high.priority_score = 10  # légua délibérément à l'inverse de predictneed_acquisition_score
        high.save(update_fields=["predictneed_acquisition_score", "priority_score"])

        low = make_prospect(name="Basse priorite", siret="90000000000002", selected_for_prospecting=True)
        low.predictneed_acquisition_score = 20
        low.priority_score = 95
        low.save(update_fields=["predictneed_acquisition_score", "priority_score"])

        response = self.client.get(reverse("prospect_list"))
        content = response.content.decode()
        self.assertLess(content.index(high.name), content.index(low.name))

    def test_no_top_level_menu_item_added(self):
        response = self.client.get(reverse("prospect_list"))
        content = response.content.decode()
        # Mission 6, section 14 : le menu principal reste Dashboard / Trouver
        # des prospects / Prospects / Campagnes / Résultats / Réglages.
        for expected in ["Dashboard", "Trouver des prospects", "Prospects", "Campagnes", "Résultats", "Réglages"]:
            self.assertIn(expected, content)


class ProspectDetailMission6Tests(LoggedInTestCase):
    def setUp(self):
        super().setUp()
        self.prospect = make_prospect()
        self.prospect.intent_score = 65
        self.prospect.engagement_score = 10
        self.prospect.save(update_fields=["intent_score", "engagement_score"])

    def test_detail_renders_why_contact_now_section(self):
        response = self.client.get(reverse("prospect_detail", args=[self.prospect.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pourquoi contacter cette entreprise maintenant ?")
        self.assertContains(response, "Action recommandée")

    def test_detail_shows_in_market_phrase_never_absolute(self):
        response = self.client.get(reverse("prospect_detail", args=[self.prospect.pk]))
        content = response.content.decode()
        self.assertNotIn("veut acheter", content.lower())

    def test_detail_with_a_real_signal_shows_last_signal(self):
        ProspectSignal.objects.create(
            prospect=self.prospect, signal_type="hiring_growth", category="growth", signal_group="intent",
            source_kind="open_web", label="Recrutement Growth détecté", evidence="preuve", confidence=75,
            score_impact=8, positive=True, observed_at=timezone.now(),
            fingerprint=signal_fingerprint("hiring_growth", "", "preuve"),
        )
        response = self.client.get(reverse("prospect_detail", args=[self.prospect.pk]))
        self.assertContains(response, "Recrutement Growth détecté")

    def test_detail_with_engagement_event_renders_without_error(self):
        EngagementEvent.objects.create(prospect=self.prospect, event_type="simulator_completed")
        response = self.client.get(reverse("prospect_detail", args=[self.prospect.pk]))
        self.assertEqual(response.status_code, 200)
