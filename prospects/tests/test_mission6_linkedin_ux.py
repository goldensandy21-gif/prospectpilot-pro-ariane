"""Mission 6 — expérience LinkedIn type Hunter.io (Discover -> Companies ->
People -> Leads -> Lists -> Campaigns -> Results), construite entièrement à
partir des modèles existants : Prospect=Company, ContactPerson=People,
PublicSocialLink/ContactPerson.profile_url=LinkedIn, ContactLog=historique,
Campaign/CampaignProspect=campagnes. Aucun nouveau modèle."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import Campaign, CampaignProspect, ContactLog, ContactPerson, EmailSequence, EmailStep
from prospects.tests.factories import make_campaign, make_icp, make_prospect, make_product


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="x", is_staff=True)
        self.client.force_login(self.user)


class CampaignCreateSequenceSelectionTests(LoggedInTestCase):
    """Correctif d'audit : la séquence choisie ne doit plus jamais être
    silencieusement écrasée par la séquence e-mail par défaut."""

    def test_selecting_an_existing_multichannel_sequence_is_respected(self):
        product = make_product()
        icp = make_icp(product)
        sequence = EmailSequence.objects.create(product=product, name="Séquence LinkedIn perso")
        EmailStep.objects.create(sequence=sequence, order=1, delay_days=0, channel="linkedin_connect", name="Invitation")
        prospect = make_prospect()
        prospect.predictneed_icp = icp
        prospect.icp_fit_score = 80
        prospect.selected_for_prospecting = True
        prospect.outbound_eligible = True
        prospect.predictneed_excluded = False
        prospect.save()

        response = self.client.post(reverse("campaign_create"), {
            "name": "Campagne LinkedIn test", "product": product.pk, "icp": icp.pk,
            "sequence": sequence.pk, "objective": "", "score_threshold": 50,
            "daily_send_limit": 30, "total_limit": 200, "selected": [str(prospect.pk)],
        })
        self.assertEqual(response.status_code, 302)
        campaign = Campaign.objects.get(name="Campagne LinkedIn test")
        self.assertEqual(campaign.sequence_id, sequence.pk)

    def test_leaving_sequence_blank_still_falls_back_to_the_default(self):
        product = make_product()
        icp = make_icp(product)
        prospect = make_prospect()
        prospect.predictneed_icp = icp
        prospect.selected_for_prospecting = True
        prospect.outbound_eligible = True
        prospect.predictneed_excluded = False
        prospect.save()

        response = self.client.post(reverse("campaign_create"), {
            "name": "Campagne par défaut", "product": product.pk, "icp": icp.pk,
            "sequence": "", "objective": "", "score_threshold": 50,
            "daily_send_limit": 30, "total_limit": 200, "selected": [str(prospect.pk)],
        })
        self.assertEqual(response.status_code, 302)
        campaign = Campaign.objects.get(name="Campagne par défaut")
        self.assertIsNotNone(campaign.sequence_id)
        self.assertTrue(campaign.sequence.steps.filter(channel="email").exists())


class LinkedInBoardTests(LoggedInTestCase):
    def test_board_renders_200(self):
        response = self.client.get(reverse("linkedin_board"))
        self.assertEqual(response.status_code, 200)

    def test_prospect_with_linkedin_and_no_contact_log_is_a_prospecter(self):
        prospect = make_prospect(selected_for_prospecting=True)
        ContactPerson.objects.create(prospect=prospect, full_name="A", profile_url="https://linkedin.com/in/a", is_active=True)
        response = self.client.get(reverse("linkedin_board"))
        self.assertContains(response, prospect.name)
        self.assertContains(response, "À prospecter")

    def test_prospect_without_linkedin_is_absent_from_the_board(self):
        prospect = make_prospect(selected_for_prospecting=True, name="Sans LinkedIn Corp")
        response = self.client.get(reverse("linkedin_board"))
        self.assertNotContains(response, "Sans LinkedIn Corp")

    def test_invitation_sent_moves_to_envoyees_bucket(self):
        prospect = make_prospect(selected_for_prospecting=True)
        ContactPerson.objects.create(prospect=prospect, full_name="A", profile_url="https://linkedin.com/in/a", is_active=True)
        ContactLog.objects.create(prospect=prospect, channel="linkedin", outcome="invitation_sent")
        response = self.client.get(reverse("linkedin_board"))
        content = response.content.decode()
        self.assertIn(prospect.name, content)

    def test_reply_moves_to_reponses_bucket(self):
        prospect = make_prospect(selected_for_prospecting=True)
        ContactPerson.objects.create(prospect=prospect, full_name="A", profile_url="https://linkedin.com/in/a", is_active=True)
        ContactLog.objects.create(prospect=prospect, channel="linkedin", outcome="replied")
        response = self.client.get(reverse("linkedin_board"))
        self.assertEqual(response.context["buckets"]["reponses"][0]["prospect"].pk, prospect.pk)

    def test_stale_acceptance_moves_to_a_relancer_bucket(self):
        prospect = make_prospect(selected_for_prospecting=True)
        ContactPerson.objects.create(prospect=prospect, full_name="A", profile_url="https://linkedin.com/in/a", is_active=True)
        log = ContactLog.objects.create(prospect=prospect, channel="linkedin", outcome="invitation_accepted")
        ContactLog.objects.filter(pk=log.pk).update(updated_at=timezone.now() - timezone.timedelta(days=10))
        response = self.client.get(reverse("linkedin_board"))
        self.assertEqual(response.context["buckets"]["a_relancer"][0]["prospect"].pk, prospect.pk)

    def test_recent_acceptance_stays_in_acceptees_bucket(self):
        prospect = make_prospect(selected_for_prospecting=True)
        ContactPerson.objects.create(prospect=prospect, full_name="A", profile_url="https://linkedin.com/in/a", is_active=True)
        ContactLog.objects.create(prospect=prospect, channel="linkedin", outcome="invitation_accepted")
        response = self.client.get(reverse("linkedin_board"))
        self.assertEqual(response.context["buckets"]["acceptees"][0]["prospect"].pk, prospect.pk)

    def test_failed_invitation_moves_to_echecs_bucket(self):
        prospect = make_prospect(selected_for_prospecting=True)
        ContactPerson.objects.create(prospect=prospect, full_name="A", profile_url="https://linkedin.com/in/a", is_active=True)
        ContactLog.objects.create(prospect=prospect, channel="linkedin", outcome="invitation_failed")
        response = self.client.get(reverse("linkedin_board"))
        self.assertEqual(response.context["buckets"]["echecs"][0]["prospect"].pk, prospect.pk)

    def test_unselected_prospect_is_excluded_from_the_board(self):
        """Le board n'affiche que des prospects réellement sélectionnés pour
        la prospection — pas un simple candidat technique."""
        prospect = make_prospect(selected_for_prospecting=False, name="Non sélectionné Corp")
        ContactPerson.objects.create(prospect=prospect, full_name="A", profile_url="https://linkedin.com/in/a", is_active=True)
        response = self.client.get(reverse("linkedin_board"))
        self.assertNotContains(response, "Non sélectionné Corp")


class ContactPersonDetailTests(LoggedInTestCase):
    def test_detail_renders_200(self):
        prospect = make_prospect()
        contact = ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont", job_title="Responsable Growth",
            profile_url="https://linkedin.com/in/alex-dupont", email="alex@example.com",
            confidence_score=80, is_active=True,
        )
        response = self.client.get(reverse("contact_person_detail", args=[contact.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alex Dupont")
        self.assertContains(response, "Responsable Growth")
        self.assertContains(response, prospect.name)

    def test_detail_shows_fit_intent_engagement_and_nba(self):
        prospect = make_prospect()
        prospect.icp_fit_score = 70
        prospect.intent_score = 55
        prospect.engagement_score = 20
        prospect.save()
        contact = ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont", job_title="Responsable Growth",
            profile_url="https://linkedin.com/in/alex-dupont", is_active=True,
        )
        response = self.client.get(reverse("contact_person_detail", args=[contact.pk]))
        content = response.content.decode()
        self.assertIn("70", content)
        self.assertIn("55", content)
        self.assertIn("20", content)

    def test_prospect_link_points_back_to_company_fiche(self):
        prospect = make_prospect()
        contact = ContactPerson.objects.create(prospect=prospect, full_name="Alex Dupont", is_active=True)
        response = self.client.get(reverse("contact_person_detail", args=[contact.pk]))
        self.assertContains(response, reverse("prospect_detail", args=[prospect.pk]))


class ProspectListDecisionMakerColumnTests(LoggedInTestCase):
    def test_list_shows_decision_maker_job_title_and_links_to_contact_detail(self):
        prospect = make_prospect(selected_for_prospecting=True)
        contact = ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont", job_title="Responsable Growth",
            is_active=True, confidence_score=80,
        )
        response = self.client.get(reverse("prospect_list"))
        self.assertContains(response, "Responsable Growth")
        self.assertContains(response, reverse("contact_person_detail", args=[contact.pk]))


class BulkEnrichTests(LoggedInTestCase):
    @patch("prospects.views.enrich_prospect_task")
    def test_bulk_enrich_queues_a_task_per_selected_prospect(self, mocked_task):
        p1 = make_prospect(selected_for_prospecting=True)
        p2 = make_prospect(selected_for_prospecting=True)
        response = self.client.post(reverse("prospect_bulk_enrich"), {"selected": [str(p1.pk), str(p2.pk)]})
        self.assertRedirects(response, reverse("prospect_list"))
        self.assertEqual(mocked_task.delay.call_count, 2)

    @patch("prospects.views.enrich_prospect_task")
    def test_bulk_enrich_never_touches_a_non_selected_prospect(self, mocked_task):
        outside = make_prospect(selected_for_prospecting=False)
        self.client.post(reverse("prospect_bulk_enrich"), {"selected": [str(outside.pk)]})
        mocked_task.delay.assert_not_called()


class NoTopLevelMenuAdditionTests(LoggedInTestCase):
    def test_menu_still_has_the_same_six_top_level_entries(self):
        response = self.client.get(reverse("prospect_list"))
        content = response.content.decode()
        for expected in ["Dashboard", "Trouver des prospects", "Prospects", "Campagnes", "Résultats", "Réglages"]:
            self.assertIn(expected, content)

    def test_linkedin_board_is_reachable_from_the_menu(self):
        response = self.client.get(reverse("prospect_list"))
        self.assertContains(response, reverse("linkedin_board"))
