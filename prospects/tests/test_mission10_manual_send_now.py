"""Correctif UX — « Envoyer maintenant » (rattrapage manuel, hors fenêtre/
jour ouvré, depuis 7b32e50).

Un email déjà Programmé (PlannedEmailContent.status="validated") que le
scheduler automatique (fenêtre 09:30-11:00, jours ouvrés uniquement) n'a
pas encore pris en charge doit pouvoir être envoyé manuellement, à tout
moment (y compris hors fenêtre et le week-end — choix explicite de
l'utilisatrice). send_planned_content_now() réutilise directement
advance_campaign_prospect() (le même moteur que le scheduler) : tous les
garde-fous par prospect restent donc actifs (contenu validé non obsolète,
anti-doublon premier contact, DNC/opposition, limites de campagne,
idempotence). Seules la fenêtre horaire/le jour ouvré et
EmailAutomationSettings.active ne sont PAS vérifiés — un clic humain
explicite remplace ces deux contrôles automatiques. Les limites
quotidiennes GLOBALES (EmailAutomationSettings), elles, restent vérifiées."""
import datetime

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import CampaignProspect, EmailAutomationSettings, EmailSend, PlannedEmailContent
from prospects.services.email_automation import (
    prepare_planned_content,
    send_planned_content_now,
    validate_planned_content,
)

from .factories import make_compliance_profile, make_icp, make_product, make_prospect, make_public_email
from .test_mission8_email_automation import make_planning_campaign


def _make_validated_member(product, icp, name, siret, email_suffix, scheduled_date=None):
    campaign = make_planning_campaign(product, icp, name=f"Camp {name}")
    prospect = make_prospect(name=name, siret=siret)
    make_public_email(prospect, email=f"{email_suffix}@example.com")
    member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="selected")
    step1 = campaign.sequence.steps.get(order=1)
    planned = prepare_planned_content(member, step1, scheduled_date or timezone.now().date())
    ok, reason = validate_planned_content(planned, None)
    assert ok, reason
    return member, planned, step1


class SendNowBypassesWindowAndWeekdayTests(TestCase):
    """La fenêtre horaire ET le jour de la semaine sont explicitement
    contournés pour un envoi manuel — choix confirmé par l'utilisatrice."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)

    def test_sends_outside_the_0930_1100_window(self):
        member, planned, _ = _make_validated_member(self.product, self.icp, "HorsFenetre", "00000000080001", "horsfenetre")
        # 14h00 UTC, largement hors 09:30-11:00 Paris.
        now = timezone.now().replace(hour=14, minute=0, second=0, microsecond=0)
        result = send_planned_content_now(planned, None, now=now)
        self.assertEqual(result["action"], "email")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False, status="sent").count(), 1)

    def test_sends_on_a_saturday(self):
        member, planned, _ = _make_validated_member(self.product, self.icp, "Weekend", "00000000080002", "weekend")
        today = timezone.now().date()
        # Prochain samedi >= aujourd'hui, pour rester cohérent avec
        # scheduled_date (jamais dans le futur par rapport à `now`).
        days_ahead = (5 - today.weekday()) % 7
        saturday = today + datetime.timedelta(days=days_ahead)
        planned.scheduled_date = today  # garde scheduled_date <= now.date() (samedi >= aujourd'hui)
        planned.save(update_fields=["scheduled_date"])
        now = timezone.datetime.combine(saturday, datetime.time(10, 0), tzinfo=datetime.timezone.utc)
        result = send_planned_content_now(planned, None, now=now)
        self.assertEqual(result["action"], "email")


class SendNowRequiresProgrammedTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected")
        self.step1 = self.campaign.sequence.steps.get(order=1)

    def test_refuses_content_never_programmed(self):
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        self.assertEqual(planned.status, "to_validate")
        result = send_planned_content_now(planned, None)
        self.assertEqual(result["action"], "not_programmed")
        self.assertEqual(EmailSend.objects.filter(is_test=False).count(), 0)

    def test_refuses_content_modified_after_programming(self):
        from prospects.services.email_automation import apply_manual_edit
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        ok, reason = validate_planned_content(planned, None)
        self.assertTrue(ok, reason)
        apply_manual_edit(planned, planned.subject, "Nouveau texte après programmation.", request=None)
        planned.refresh_from_db()
        self.assertEqual(planned.status, "modified")
        result = send_planned_content_now(planned, None)
        self.assertEqual(result["action"], "not_programmed")
        self.assertEqual(EmailSend.objects.filter(is_test=False).count(), 0)


class SendNowStillRespectsGlobalDailyLimitsTests(TestCase):
    """Les limites quotidiennes GLOBALES (EmailAutomationSettings) restent
    vérifiées — seule la fenêtre horaire est contournée."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)

    def test_refuses_when_daily_total_limit_already_reached(self):
        EmailAutomationSettings.objects.create(daily_total_limit=0, new_contacts_per_day=5)
        member, planned, _ = _make_validated_member(self.product, self.icp, "PlafondTotal", "00000000080003", "plafondtotal")
        result = send_planned_content_now(planned, None)
        self.assertEqual(result["action"], "deferred_daily_total_limit")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False).count(), 0)

    def test_refuses_when_new_contacts_per_day_already_reached(self):
        EmailAutomationSettings.objects.create(daily_total_limit=10, new_contacts_per_day=0)
        member, planned, _ = _make_validated_member(self.product, self.icp, "PlafondNouveaux", "00000000080004", "plafondnouveaux")
        result = send_planned_content_now(planned, None)
        self.assertEqual(result["action"], "deferred_new_contacts_limit")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False).count(), 0)


class SendNowIgnoresActiveToggleTests(TestCase):
    """EmailAutomationSettings.active ne conditionne PAS l'envoi manuel —
    c'est une action humaine distincte du moteur automatique."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)

    def test_sends_even_when_automation_is_inactive(self):
        EmailAutomationSettings.objects.create(active=False)
        member, planned, _ = _make_validated_member(self.product, self.icp, "Inactif", "00000000080005", "inactif")
        result = send_planned_content_now(planned, None)
        self.assertEqual(result["action"], "email")


class SendNowPreservesExistingSafeguardsTests(TestCase):
    """Anti-doublon premier contact, contenu obsolète, idempotence — tous
    hérités intacts de advance_campaign_prospect()."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)

    def test_blocks_duplicate_first_contact(self):
        # Un premier contact commercial déjà envoyé (autre campagne) pour la
        # MÊME adresse doit bloquer un nouveau "premier contact" ailleurs.
        member_a, planned_a, _ = _make_validated_member(self.product, self.icp, "Doublon A", "00000000080006", "doublon")
        result_a = send_planned_content_now(planned_a, None)
        self.assertEqual(result_a["action"], "email")

        campaign_b = make_planning_campaign(self.product, self.icp, name="Camp Doublon B")
        prospect_b = make_prospect(name="Doublon B", siret="00000000080007")
        make_public_email(prospect_b, email="doublon@example.com")  # même adresse
        member_b = CampaignProspect.objects.create(campaign=campaign_b, prospect=prospect_b, status="selected")
        step1_b = campaign_b.sequence.steps.get(order=1)
        planned_b = prepare_planned_content(member_b, step1_b, timezone.now().date())
        ok, reason = validate_planned_content(planned_b, None)
        self.assertTrue(ok, reason)

        result_b = send_planned_content_now(planned_b, None)
        self.assertEqual(result_b["action"], "blocked_duplicate_first_contact")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member_b, is_test=False).count(), 0)

    def test_blocks_stale_content_even_if_validated(self):
        member, planned, step1 = _make_validated_member(self.product, self.icp, "Perime", "00000000080008", "perime")
        # Modifie une donnée source APRÈS la programmation -> rend le
        # contenu figé obsolète (comparé au rendu live).
        member.prospect.name = "Nom Changé Après Programmation"
        member.prospect.save(update_fields=["name"])
        result = send_planned_content_now(planned, None)
        self.assertEqual(result["action"], "blocked_awaiting_validation")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False).count(), 0)

    def test_second_call_never_double_sends(self):
        member, planned, _ = _make_validated_member(self.product, self.icp, "Idempotent", "00000000080009", "idempotent")
        result1 = send_planned_content_now(planned, None)
        self.assertEqual(result1["action"], "email")
        result2 = send_planned_content_now(planned, None)
        self.assertNotEqual(result2["action"], "email")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False, status="sent").count(), 1)

    def test_refuses_when_scheduled_date_is_in_the_future(self):
        future = timezone.now().date() + datetime.timedelta(days=10)
        member, planned, _ = _make_validated_member(self.product, self.icp, "Futur", "00000000080010", "futur", scheduled_date=future)
        result = send_planned_content_now(planned, None)
        self.assertEqual(result["action"], "deferred_not_yet_due")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False).count(), 0)


class IndividualDetailSendNowViewTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="sendnowA", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect(name="Fiche Individuelle")
        make_public_email(self.prospect, email="fiche@example.com")
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected")
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned = prepare_planned_content(self.member, self.step1, timezone.now().date())

    def test_send_now_button_absent_when_not_programmed(self):
        response = self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        content = response.content.decode()
        self.assertNotIn('value="send_now"', content)

    def test_send_now_button_present_once_programmed(self):
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)
        response = self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        content = response.content.decode()
        self.assertIn('value="send_now"', content)

    def test_post_send_now_on_unprogrammed_content_sends_nothing(self):
        outbox_before = len(mail.outbox)
        response = self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {"action": "send_now"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), outbox_before)
        self.assertEqual(EmailSend.objects.filter(is_test=False).count(), 0)

    def test_post_send_now_on_programmed_content_sends_exactly_one_email(self):
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)
        response = self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {"action": "send_now"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member, is_test=False, status="sent").count(), 1)

    def test_get_never_triggers_a_send_even_when_programmed(self):
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)
        outbox_before = len(mail.outbox)
        self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        self.assertEqual(len(mail.outbox), outbox_before)
        self.assertEqual(EmailSend.objects.filter(is_test=False).count(), 0)


class BulkSendSelectionNowViewTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="sendnowB", password="x")
        self.client = Client()
        self.client.force_login(self.user)

        self.member_a, self.planned_a, _ = _make_validated_member(self.product, self.icp, "Bulk Programme A", "00000000090001", "bulkprogrammea")
        self.member_b, self.planned_b, _ = _make_validated_member(self.product, self.icp, "Bulk Programme B", "00000000090002", "bulkprogrammeb")

        campaign_c = make_planning_campaign(self.product, self.icp, name="Camp Bulk Non Programme")
        prospect_c = make_prospect(name="Bulk Non Programme C", siret="00000000090003")
        make_public_email(prospect_c, email="bulknonprogrammec@example.com")
        self.member_c = CampaignProspect.objects.create(campaign=campaign_c, prospect=prospect_c, status="selected")
        step1_c = campaign_c.sequence.steps.get(order=1)
        self.planned_c = prepare_planned_content(self.member_c, step1_c, timezone.now().date())  # jamais Programmé

    def test_bulk_send_now_only_sends_selected_and_programmed_rows(self):
        response = self.client.post(reverse("email_planning_send_selection_now"), {
            "planned_ids": [self.planned_a.pk, self.planned_c.pk],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member_a, is_test=False, status="sent").count(), 1)
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member_c, is_test=False).count(), 0)
        # Non sélectionné du tout -> jamais touché.
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member_b, is_test=False).count(), 0)

    def test_bulk_send_now_with_get_does_nothing(self):
        response = self.client.get(reverse("email_planning_send_selection_now"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EmailSend.objects.filter(is_test=False).count(), 0)

    def test_bulk_send_now_empty_selection_sends_nothing(self):
        response = self.client.post(reverse("email_planning_send_selection_now"), {})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EmailSend.objects.filter(is_test=False).count(), 0)
