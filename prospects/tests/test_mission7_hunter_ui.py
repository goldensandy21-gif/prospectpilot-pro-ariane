"""Mission 7F — interface Hunter-like « Trouver des prospects › Web
Intelligence » : Entreprises | Personnes | Web Intelligence (données), sous
le même menu que le reste de l'application (aucun nouveau menu de premier
niveau). Les actions en masse ne créent ni ne modifient jamais rien en
dehors des chemins déjà validés (sélection, enrich_prospect_task, création
de campagne avec ses propres garde-fous)."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import ContactPerson, ProspectEvidence
from prospects.tests.factories import make_prospect


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="x")
        self.client.force_login(self.user)


class WebIntelligenceHubTabsTests(LoggedInTestCase):
    def test_entreprises_tab_renders_all_prospects_not_only_selected(self):
        selected = make_prospect(name="Selected Corp", selected_for_prospecting=True)
        not_selected = make_prospect(name="Not Selected Corp", selected_for_prospecting=False)
        response = self.client.get(reverse("web_intelligence_hub"), {"tab": "entreprises"})
        self.assertContains(response, "Selected Corp")
        self.assertContains(response, "Not Selected Corp")

    def test_only_new_filter_excludes_already_selected_prospects(self):
        make_prospect(name="Alpha Corp", selected_for_prospecting=True)
        make_prospect(name="Beta Corp", selected_for_prospecting=False)
        response = self.client.get(reverse("web_intelligence_hub"), {"tab": "entreprises", "only_new": "1"})
        self.assertNotContains(response, "Alpha Corp")
        self.assertContains(response, "Beta Corp")

    def test_personnes_tab_lists_people_across_all_prospects(self):
        prospect = make_prospect()
        ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", job_title="CEO", is_active=True)
        response = self.client.get(reverse("web_intelligence_hub"), {"tab": "personnes"})
        self.assertContains(response, "Julie Martin")

    def test_personnes_tab_filters_by_job_title(self):
        prospect = make_prospect()
        ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", job_title="CEO", is_active=True)
        ContactPerson.objects.create(prospect=prospect, full_name="Marc Dupuis", job_title="Growth Manager", is_active=True)
        response = self.client.get(reverse("web_intelligence_hub"), {"tab": "personnes", "job_title": "growth"})
        self.assertContains(response, "Marc Dupuis")
        self.assertNotContains(response, "Julie Martin")

    def test_data_tab_lists_evidence(self):
        prospect = make_prospect()
        ProspectEvidence.objects.create(
            prospect=prospect, field_name="job_posting_growth", value="Growth Manager",
            normalized_value="growth manager", value_type="other", confidence_score=85,
            verification_status="verified", source_url="https://ex.fr/carrieres",
        )
        response = self.client.get(reverse("web_intelligence_hub"), {"tab": "data"})
        self.assertContains(response, "job_posting_growth")

    def test_no_new_top_level_menu_entry(self):
        response = self.client.get(reverse("web_intelligence_hub"))
        content = response.content.decode()
        for expected in ["Dashboard", "Trouver des prospects", "Prospects", "Campagnes", "Résultats", "Réglages"]:
            self.assertIn(expected, content)
        self.assertIn(reverse("web_intelligence_hub"), content)


class AddToProspectsTests(LoggedInTestCase):
    def test_adds_selected_prospects(self):
        p1 = make_prospect(selected_for_prospecting=False)
        p2 = make_prospect(selected_for_prospecting=False)
        response = self.client.post(reverse("web_intelligence_add_to_prospects"), {
            "selected": [str(p1.pk), str(p2.pk)], "source": "prospect", "tab": "entreprises",
        })
        self.assertRedirects(response, reverse("web_intelligence_hub") + "?tab=entreprises")
        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertTrue(p1.selected_for_prospecting)
        self.assertTrue(p2.selected_for_prospecting)

    def test_adds_the_prospects_of_selected_people(self):
        prospect = make_prospect(selected_for_prospecting=False)
        contact = ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", is_active=True)
        self.client.post(reverse("web_intelligence_add_to_prospects"), {
            "selected": [str(contact.pk)], "source": "person", "tab": "personnes",
        })
        prospect.refresh_from_db()
        self.assertTrue(prospect.selected_for_prospecting)

    def test_get_never_modifies_anything(self):
        prospect = make_prospect(selected_for_prospecting=False)
        self.client.get(reverse("web_intelligence_add_to_prospects"))
        prospect.refresh_from_db()
        self.assertFalse(prospect.selected_for_prospecting)


class BulkEnrichTests(LoggedInTestCase):
    @patch("prospects.views.enrich_prospect_task")
    def test_enriches_any_prospect_even_if_not_yet_selected(self, mocked_task):
        prospect = make_prospect(selected_for_prospecting=False)
        response = self.client.post(reverse("web_intelligence_bulk_enrich"), {
            "selected": [str(prospect.pk)], "source": "prospect", "tab": "entreprises",
        })
        self.assertRedirects(response, reverse("web_intelligence_hub") + "?tab=entreprises")
        mocked_task.delay.assert_called_once_with(prospect.pk, None, self.user.pk)

    @patch("prospects.views.enrich_prospect_task")
    def test_enriches_the_distinct_prospects_of_selected_people(self, mocked_task):
        prospect = make_prospect(selected_for_prospecting=False)
        c1 = ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", is_active=True)
        c2 = ContactPerson.objects.create(prospect=prospect, full_name="Marc Dupuis", is_active=True)
        self.client.post(reverse("web_intelligence_bulk_enrich"), {
            "selected": [str(c1.pk), str(c2.pk)], "source": "person", "tab": "personnes",
        })
        self.assertEqual(mocked_task.delay.call_count, 1)


class AddToCampaignRedirectTests(LoggedInTestCase):
    def test_redirects_to_campaign_create_with_prospect_ids(self):
        p1 = make_prospect()
        p2 = make_prospect()
        response = self.client.post(reverse("web_intelligence_add_to_campaign"), {
            "selected": [str(p1.pk), str(p2.pk)], "source": "prospect", "tab": "entreprises",
        })
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertIn(reverse("campaign_create"), location)
        self.assertIn(str(p1.pk), location)
        self.assertIn(str(p2.pk), location)

    def test_never_creates_a_campaign_by_itself(self):
        from prospects.models import Campaign
        prospect = make_prospect()
        self.client.post(reverse("web_intelligence_add_to_campaign"), {
            "selected": [str(prospect.pk)], "source": "prospect", "tab": "entreprises",
        })
        self.assertEqual(Campaign.objects.count(), 0)

    def test_resolves_person_selection_to_their_prospect(self):
        prospect = make_prospect()
        contact = ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", is_active=True)
        response = self.client.post(reverse("web_intelligence_add_to_campaign"), {
            "selected": [str(contact.pk)], "source": "person", "tab": "personnes",
        })
        self.assertIn(str(prospect.pk), response.headers["Location"])

    def test_empty_selection_redirects_back_without_crashing(self):
        response = self.client.post(reverse("web_intelligence_add_to_campaign"), {
            "selected": [], "source": "prospect", "tab": "entreprises",
        })
        self.assertRedirects(response, reverse("web_intelligence_hub") + "?tab=entreprises")
