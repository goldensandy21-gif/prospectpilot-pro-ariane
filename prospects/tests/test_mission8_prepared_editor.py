"""Workflow final — voir, modifier et programmer les emails dans
ProspectPilot (depuis 32f2f98).

Couvre : la page « Emails préparés » (liste complète, pas seulement la
prochaine étape par prospect), la page détail/édition d'UN
PlannedEmailContent (rendu figé exact, modification sujet+texte
rédactionnel jamais du HTML brut, test facultatif, Programmer individuel),
la sélection multiple + Programmer la sélection, et la levée du verrou
`tested_content_hash` (le test n'est plus une condition de validation)."""
import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import CampaignProspect, EmailAutomationSettings, EmailSend, PlannedEmailContent
from prospects.services.campaign_sequencing import advance_campaign_prospect
from prospects.services.email_automation import (
    apply_manual_edit,
    prepare_planned_content,
    promote_campaign_after_validation,
    send_test_email,
    validate_planned_content,
)

from .factories import make_compliance_profile, make_icp, make_product, make_prospect, make_public_email
from .test_mission8_email_automation import make_planning_campaign


def _current_planning_monday():
    """Reproduit exactement le calcul de « semaine courante » utilisé par
    email_planning_prepared() (fuseau EmailAutomationSettings.timezone_name,
    pas simplement la date UTC naïve) — pour que les scheduled_date fabriqués
    dans les tests tombent bien dans la même semaine que celle affichée."""
    tz = ZoneInfo(EmailAutomationSettings.current().timezone_name)
    today = timezone.now().astimezone(tz).date()
    return today - datetime.timedelta(days=today.weekday())


class PreparedEmailsListTests(TestCase):
    """Tous les emails préparés apparaissent dans l'interface."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="editorA", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.members = []
        self.steps = list(self.campaign.sequence.steps.order_by("order"))
        for i in range(3):
            prospect = make_prospect(name=f"Prospect{i}", siret=f"0000000004{i:04d}")
            make_public_email(prospect, email=f"p{i}@example.com")
            member = CampaignProspect.objects.create(campaign=self.campaign, prospect=prospect, status="selected")
            self.members.append(member)

    def test_all_prepared_emails_appear_in_the_interface(self):
        monday_this_week = _current_planning_monday()
        for member in self.members:
            prepare_planned_content(member, self.steps[0], monday_this_week)

        response = self.client.get(reverse("email_planning_prepared"))
        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 3)
        names = {r["planned"].campaign_prospect.prospect.name for r in rows}
        self.assertEqual(names, {"Prospect0", "Prospect1", "Prospect2"})

    def test_only_current_week_planned_content_is_listed(self):
        monday_this_week = _current_planning_monday()
        far_future = monday_this_week + datetime.timedelta(days=60)
        prepare_planned_content(self.members[0], self.steps[0], monday_this_week)
        prepare_planned_content(self.members[1], self.steps[0], far_future)

        response = self.client.get(reverse("email_planning_prepared"))
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["planned"].campaign_prospect.prospect.name, "Prospect0")


class ContentDetailGetViewTests(TestCase):
    """Reproduit le bug production (Server Error 500) : un simple GET de la
    page détail d'un PlannedEmailContent doit toujours rendre 200, jamais
    planter — que ce soit à cause d'un template cassé, d'un mauvais
    reverse(), ou d'un accès invalide à un champ. Le GET ne doit strictement
    rien modifier (ni envoi SMTP, ni statut de programmation)."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="editorG", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect(name="Boulangerie Dupont")
        make_public_email(self.prospect, email="dupont@example.com")
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected")
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned = prepare_planned_content(self.member, self.step1, _current_planning_monday())

    def test_get_content_detail_returns_200_not_500(self):
        response = self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        self.assertEqual(response.status_code, 200)

    def test_get_content_detail_displays_prospect_email_step_and_subject(self):
        response = self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        content = response.content.decode()
        self.assertIn("Boulangerie Dupont", content)
        self.assertIn("dupont@example.com", content)
        self.assertIn(self.step1.name, content)
        self.assertIn(self.planned.subject, content)

    def test_get_content_detail_does_not_send_any_commercial_email(self):
        commercial_before = EmailSend.objects.filter(is_test=False).count()
        outbox_before = len(mail.outbox)
        response = self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmailSend.objects.filter(is_test=False).count(), commercial_before)
        self.assertEqual(len(mail.outbox), outbox_before)

    def test_get_content_detail_does_not_change_approval_or_status(self):
        status_before = self.planned.status
        response = self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        self.assertEqual(response.status_code, 200)
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.status, status_before)
        self.assertIsNone(self.planned.approved_by)
        self.assertIsNone(self.planned.approved_at)

    def test_link_from_prepared_list_to_content_detail_resolves_and_loads(self):
        list_response = self.client.get(reverse("email_planning_prepared"))
        self.assertEqual(list_response.status_code, 200)
        detail_url = reverse("email_planning_content_detail", args=[self.planned.pk])
        self.assertIn(detail_url, list_response.content.decode())

        detail_response = self.client.get(detail_url)
        self.assertEqual(detail_response.status_code, 200)


class ManualEditTests(TestCase):
    """Modification du sujet/texte, portée limitée, invalidation de
    l'approbation existante, préservation conformité/CTA/footer/tracking."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="editorB", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected")
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned = prepare_planned_content(self.member, self.step1, timezone.now().date())

    def test_editing_subject_and_body_changes_content(self):
        old_hash = self.planned.content_hash
        apply_manual_edit(self.planned, "Nouveau sujet personnalisé", "Un texte entièrement écrit à la main.", request=None)
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.subject, "Nouveau sujet personnalisé")
        self.assertIn("Un texte entièrement écrit à la main.", self.planned.text_body)
        self.assertIn("Un texte entièrement écrit à la main.", self.planned.html_body)
        self.assertNotEqual(self.planned.content_hash, old_hash)

    def test_edit_produces_a_new_content_hash(self):
        old_hash = self.planned.content_hash
        apply_manual_edit(self.planned, self.planned.subject, "Texte modifié.", request=None)
        self.planned.refresh_from_db()
        self.assertNotEqual(self.planned.content_hash, old_hash)

    def test_edit_only_affects_this_planned_email_content(self):
        prospect2 = make_prospect(name="Autre Prospect", siret="00000000099999")
        make_public_email(prospect2, email="autre@example.com")
        member2 = CampaignProspect.objects.create(campaign=self.campaign, prospect=prospect2, status="selected")
        planned2 = prepare_planned_content(member2, self.step1, timezone.now().date())
        original_html2 = planned2.html_body
        original_hash2 = planned2.content_hash

        apply_manual_edit(self.planned, "Sujet unique", "Texte unique pour ce seul prospect.", request=None)

        planned2.refresh_from_db()
        self.assertEqual(planned2.html_body, original_html2)
        self.assertEqual(planned2.content_hash, original_hash2)
        self.assertNotIn("Texte unique pour ce seul prospect.", planned2.html_body)

    def test_edit_does_not_touch_email_variant_or_agent_brief(self):
        variant = self.step1.variants.first()
        original_subject_template = variant.subject_template
        original_brief = self.member.agent_brief

        apply_manual_edit(self.planned, "Sujet modifié", "Corps modifié.", request=None)

        variant.refresh_from_db()
        self.assertEqual(variant.subject_template, original_subject_template)
        self.member.refresh_from_db()
        self.assertEqual(self.member.agent_brief, original_brief)

    def test_previous_approval_invalidated_after_edit(self):
        send_test_email(self.member, self.planned, "contact-predict@predictneed-ia.com")
        self.planned.refresh_from_db()
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)
        self.assertEqual(self.planned.status, "validated")

        apply_manual_edit(self.planned, "Sujet re-modifié", "Nouveau texte après programmation.", request=None)
        self.planned.refresh_from_db()

        self.assertEqual(self.planned.status, "modified")
        self.assertIsNone(self.planned.approved_at)
        self.assertIsNone(self.planned.approved_by)

    def test_edit_never_falsifies_tested_content_hash(self):
        send_test_email(self.member, self.planned, "contact-predict@predictneed-ia.com")
        self.planned.refresh_from_db()
        tested_hash_before = self.planned.tested_content_hash
        self.assertTrue(tested_hash_before)

        apply_manual_edit(self.planned, "Sujet re-modifié", "Nouveau texte.", request=None)
        self.planned.refresh_from_db()

        # tested_content_hash reste celui de l'ANCIEN test — jamais mis à
        # jour pour correspondre au nouveau contenu non testé.
        self.assertEqual(self.planned.tested_content_hash, tested_hash_before)
        self.assertNotEqual(self.planned.tested_content_hash, self.planned.content_hash)

    def test_edit_never_marks_stale_afterwards_even_if_prospect_data_changes(self):
        apply_manual_edit(self.planned, "Sujet fixe", "Texte fixe choisi à la main.", request=None)
        self.planned.refresh_from_db()

        self.prospect.name = "NOM CHANGÉ APRÈS ÉDITION MANUELLE"
        self.prospect.save(update_fields=["name"])

        from prospects.services.email_automation import is_content_stale
        self.assertFalse(is_content_stale(self.planned))

    def test_manually_edited_content_preserves_compliance_cta_footer_tracking(self):
        apply_manual_edit(self.planned, "Sujet libre", "Un paragraphe écrit à la main pour ce prospect.", request=None)
        self.planned.refresh_from_db()
        html = self.planned.html_body
        text = self.planned.text_body

        self.assertIn("Se désabonner", html)
        self.assertIn("Se désabonner", text)
        self.assertIn(f"/t/{self.member.tracking_token}/", html)
        self.assertIn(str(self.prospect.unsubscribe_token), html)
        self.assertIn(self.product.sender_name, html)
        self.assertEqual(html.count("<img"), 0)  # jamais de pixel dans le contenu figé

    def test_edit_view_saves_via_post(self):
        response = self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {
            "action": "edit", "subject": "Sujet via la vue", "body_text": "Texte via la vue.",
        })
        self.assertEqual(response.status_code, 302)
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.subject, "Sujet via la vue")
        self.assertIn("Texte via la vue.", self.planned.text_body)


class OptionalTestEmailTests(TestCase):
    """Le test devient facultatif — ne bloque plus Programmer."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="editorC", password="x")
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected")
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned = prepare_planned_content(self.member, self.step1, timezone.now().date())

    def test_programming_possible_without_any_test_sent(self):
        self.assertEqual(self.planned.tested_content_hash, "")
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.status, "validated")

    def test_human_programming_records_approved_by_and_approved_at(self):
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.approved_by, self.user)
        self.assertIsNotNone(self.planned.approved_at)

    def test_test_send_is_not_required_and_stays_optional(self):
        """Envoyer un test reste possible mais n'est plus une étape
        obligatoire du chemin critique."""
        record = send_test_email(self.member, self.planned, "contact-predict@predictneed-ia.com")
        self.assertEqual(record.status, "sent")
        self.planned.refresh_from_db()
        self.assertTrue(self.planned.tested_content_hash)
        # Toujours possible de programmer ensuite — le test n'a rien changé
        # à l'obligation (il n'y en avait pas).
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)


class ProgrammerNeverSendsImmediatelyTests(TestCase):
    """Programmer ≠ Envoyer maintenant. Un email non programmé n'est
    jamais envoyé."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="editorD", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected")
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned = prepare_planned_content(self.member, self.step1, timezone.now().date())

    def test_programmer_click_sends_zero_new_commercial_emailsend_immediately(self):
        outbox_before = len(mail.outbox)
        commercial_before = EmailSend.objects.filter(is_test=False).count()

        response = self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {"action": "programmer"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), outbox_before)
        self.assertEqual(EmailSend.objects.filter(is_test=False).count(), commercial_before)
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.status, "validated")

    def test_unprogrammed_email_is_never_sent_by_the_scheduler(self):
        # Ne programme pas -> reste "to_validate".
        result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "blocked_awaiting_validation")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).count(), 0)

    def test_edited_content_after_programming_becomes_unvalidated_again_and_scheduler_blocks_it(self):
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)
        promote_campaign_after_validation(self.campaign, self.user)

        apply_manual_edit(self.planned, "Sujet changé après programmation", "Texte changé après programmation.", request=None)
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.status, "modified")

        result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "blocked_awaiting_validation")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).count(), 0)


class BulkSelectionProgrammerTests(TestCase):
    """Sélection multiple, Programmer la sélection, jamais un autre email
    non coché."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="editorE", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned_list = []
        for i in range(4):
            prospect = make_prospect(name=f"BulkP{i}", siret=f"0000000005{i:04d}")
            make_public_email(prospect, email=f"bulk{i}@example.com")
            member = CampaignProspect.objects.create(campaign=self.campaign, prospect=prospect, status="selected")
            self.planned_list.append(prepare_planned_content(member, self.step1, timezone.now().date()))

    def test_selecting_two_of_four_programs_only_those_two(self):
        outbox_before = len(mail.outbox)
        selected_ids = [self.planned_list[0].pk, self.planned_list[2].pk]

        response = self.client.post(reverse("email_planning_programmer_selection"), {"planned_ids": selected_ids})
        self.assertEqual(response.status_code, 302)

        self.planned_list[0].refresh_from_db()
        self.planned_list[1].refresh_from_db()
        self.planned_list[2].refresh_from_db()
        self.planned_list[3].refresh_from_db()

        self.assertEqual(self.planned_list[0].status, "validated")
        self.assertEqual(self.planned_list[2].status, "validated")
        self.assertEqual(self.planned_list[1].status, "to_validate")
        self.assertEqual(self.planned_list[3].status, "to_validate")
        self.assertEqual(len(mail.outbox), outbox_before)

    def test_select_all_programs_all(self):
        all_ids = [p.pk for p in self.planned_list]
        self.client.post(reverse("email_planning_programmer_selection"), {"planned_ids": all_ids})
        for p in self.planned_list:
            p.refresh_from_db()
            self.assertEqual(p.status, "validated")

    def test_empty_selection_programs_nothing(self):
        response = self.client.post(reverse("email_planning_programmer_selection"), {})
        self.assertEqual(response.status_code, 302)
        for p in self.planned_list:
            p.refresh_from_db()
            self.assertEqual(p.status, "to_validate")


class ProspectEligibilityGateTests(TestCase):
    """Programmer vérifie que le prospect n'est pas exclu/supprimé/DNC et
    qu'il possède toujours une adresse valide."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="editorF", password="x")
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.step1 = self.campaign.sequence.steps.get(order=1)

    def _member_and_planned(self, **prospect_overrides):
        prospect = make_prospect(**prospect_overrides)
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=self.campaign, prospect=prospect, status="selected")
        planned = prepare_planned_content(member, self.step1, timezone.now().date())
        return member, planned

    def test_excluded_prospect_cannot_be_programmed(self):
        member, planned = self._member_and_planned(name="Exclu", siret="00000000061111", predictneed_excluded=True)
        ok, reason = validate_planned_content(planned, self.user)
        self.assertFalse(ok)
        self.assertEqual(reason, "prospect_not_eligible")

    def test_suppressed_prospect_cannot_be_programmed(self):
        member, planned = self._member_and_planned(name="Oppose", siret="00000000062222", prospecting_allowed=False)
        ok, reason = validate_planned_content(planned, self.user)
        self.assertFalse(ok)
        self.assertEqual(reason, "prospect_not_eligible")

    def test_prospect_without_email_cannot_be_programmed(self):
        prospect = make_prospect(name="SansEmail", siret="00000000063333")
        member = CampaignProspect.objects.create(campaign=self.campaign, prospect=prospect, status="selected")
        planned = prepare_planned_content(member, self.step1, timezone.now().date())
        ok, reason = validate_planned_content(planned, self.user)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_email")

    def test_excluded_campaign_prospect_status_cannot_be_programmed(self):
        member, planned = self._member_and_planned(name="Arrete", siret="00000000064444")
        member.status = "excluded"
        member.save(update_fields=["status"])
        ok, reason = validate_planned_content(planned, self.user)
        self.assertFalse(ok)
        self.assertEqual(reason, "prospect_not_eligible")
