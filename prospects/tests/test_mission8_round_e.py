"""Round E — 3 derniers bloquants avant pré-déploiement (depuis 9af0f2a).

1) un e-mail is_test=True ne porte jamais le vrai List-Unsubscribe du
   prospect (ni List-Unsubscribe-Post) ;
2) paused/cancelled/completed restent bloquées par TOUTES les actions
   globales Planning (Envoyer les tests, Valider et programmer), pas
   seulement build_week_plan() ;
3) « Préparer la semaine » prépare aussi les étapes suivantes (J4/J8/J14)
   d'un nouveau J0 dont la date tombe dans la même semaine, sans jamais
   avancer current_step ni créer d'EmailSend commercial — le moteur réel
   garde ses garde-fous inchangés (délai réel, réponse qui arrête tout).
"""
import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import (
    CampaignProspect,
    ContactLog,
    EmailAutomationSettings,
    EmailSend,
    PlannedEmailContent,
)
from prospects.services.campaign_sequencing import advance_campaign_prospect
from prospects.services.email_automation import (
    build_week_plan,
    prepare_planned_content,
    promote_campaign_after_validation,
    send_test_email,
    validate_planned_content,
)

from .factories import make_compliance_profile, make_icp, make_product, make_prospect, make_public_email
from .test_mission8_round_d import _prepare_test_and_validate
from .test_mission8_email_automation import make_planning_campaign


class TestEmailNeverHasRealListUnsubscribeTests(TestCase):
    """Point 1."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        self.step1 = self.campaign.sequence.steps.get(order=1)

    def test_test_email_has_no_list_unsubscribe_headers_at_all(self):
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        send_test_email(self.member, planned, "contact-predict@predictneed-ia.com")

        sent_msg = mail.outbox[-1]
        self.assertNotIn("List-Unsubscribe", sent_msg.extra_headers)
        self.assertNotIn("List-Unsubscribe-Post", sent_msg.extra_headers)

    def test_commercial_email_keeps_real_list_unsubscribe_and_one_click(self):
        _prepare_test_and_validate(self.member, self.step1, timezone.now().date())
        advance_campaign_prospect(self.member.pk)

        sent_msg = mail.outbox[-1]
        self.assertIn("List-Unsubscribe", sent_msg.extra_headers)
        self.assertIn(str(self.prospect.unsubscribe_token), sent_msg.extra_headers["List-Unsubscribe"])
        self.assertEqual(sent_msg.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_legacy_manual_campaign_test_send_also_gets_no_real_list_unsubscribe(self):
        """Le correctif s'applique à TOUT envoi is_test=True, pas seulement
        au chemin Planning (frozen_content) — même le rendu live legacy."""
        from prospects.services.predictneed_email import send_predictneed_campaign_email

        variant = self.step1.variants.first()
        send_predictneed_campaign_email(
            self.member, email_step=self.step1, email_variant=variant,
            is_test=True, test_recipient="contact-predict@predictneed-ia.com",
        )
        sent_msg = mail.outbox[-1]
        self.assertNotIn("List-Unsubscribe", sent_msg.extra_headers)
        self.assertNotIn("List-Unsubscribe-Post", sent_msg.extra_headers)


class PausedCancelledCompletedBlockedEverywhereTests(TestCase):
    """Point 2."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="testerE2", password="x")
        self.client = Client()
        self.client.force_login(self.user)

    def _make_pending(self, status, i):
        campaign = make_planning_campaign(self.product, self.icp, name=f"CampE2-{status}", status=status)
        prospect = make_prospect(name=f"PE2-{status}", siret=f"0000000003{i:04d}")
        make_public_email(prospect, email=f"pe2-{status}@example.com")
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")
        step1 = campaign.sequence.steps.get(order=1)
        planned = prepare_planned_content(member, step1, timezone.now().date())
        return campaign, member, planned

    def _assert_status_untouched_and_never_validated(self, status, i):
        campaign, member, planned = self._make_pending(status, i)
        old_validated_at = campaign.validated_at
        outbox_before = len(mail.outbox)

        self.client.post(reverse("email_planning_send_tests"))
        self.assertEqual(len(mail.outbox), outbox_before, f"aucun test ne doit être envoyé pour une campagne {status}")
        planned.refresh_from_db()
        self.assertEqual(planned.status, "to_validate")
        self.assertEqual(planned.tested_content_hash, "")

        self.client.post(reverse("email_planning_validate_and_schedule"))
        campaign.refresh_from_db()
        planned.refresh_from_db()
        self.assertEqual(campaign.status, status)
        self.assertEqual(campaign.validated_at, old_validated_at)
        self.assertNotEqual(planned.status, "validated")

    def test_paused_stays_paused(self):
        self._assert_status_untouched_and_never_validated("paused", 1)

    def test_cancelled_stays_cancelled(self):
        self._assert_status_untouched_and_never_validated("cancelled", 2)

    def test_completed_stays_completed(self):
        self._assert_status_untouched_and_never_validated("completed", 3)

    def test_promote_campaign_after_validation_refuses_paused_cancelled_completed(self):
        for i, status in enumerate(("paused", "cancelled", "completed")):
            campaign, member, planned = self._make_pending(status, 10 + i)
            old_validated_at = campaign.validated_at
            promote_campaign_after_validation(campaign, self.user)
            campaign.refresh_from_db()
            self.assertEqual(campaign.status, status)
            self.assertEqual(campaign.validated_at, old_validated_at)


class WeekPlanIncludesFollowupsWithinWeekTests(TestCase):
    """Point 3."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        EmailAutomationSettings.objects.create(new_contacts_per_day=10, daily_total_limit=100)
        self.monday = datetime.datetime(2026, 8, 24, 8, 0, tzinfo=datetime.timezone.utc)

    def _make_member(self, name, siret):
        campaign = make_planning_campaign(self.product, self.icp, name=name)
        prospect = make_prospect(name=name, siret=siret)
        make_public_email(prospect, email=f"{name.lower()}@example.com")
        return campaign, CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")

    def test_j0_monday_plan_includes_j4_friday_but_not_j8_j14(self):
        campaign, member = self._make_member("WeekPlanA", "00000000090001")

        plan = build_week_plan(now=self.monday)
        entries = {(date, step.order) for date, cp, step in plan if cp.pk == member.pk}

        self.assertIn((datetime.date(2026, 8, 24), 1), entries)  # J0 lundi
        self.assertIn((datetime.date(2026, 8, 28), 2), entries)  # J4 vendredi
        orders_present = {order for _date, order in entries}
        self.assertNotIn(3, orders_present)  # J8 (01/09) hors semaine
        self.assertNotIn(4, orders_present)  # J14 (07/09) hors semaine

        member.refresh_from_db()
        self.assertIsNone(member.current_step)  # jamais avancé artificiellement
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member).count(), 0)

    def test_prepare_week_creates_planned_content_for_both_j0_and_j4(self):
        campaign, member = self._make_member("WeekPlanB", "00000000090002")

        plan = build_week_plan(now=self.monday)
        for scheduled_date, cp, step in plan:
            prepare_planned_content(cp, step, scheduled_date)

        step1 = campaign.sequence.steps.get(order=1)
        step2 = campaign.sequence.steps.get(order=2)
        planned_j0 = PlannedEmailContent.objects.get(campaign_prospect=member, email_step=step1)
        planned_j4 = PlannedEmailContent.objects.get(campaign_prospect=member, email_step=step2)
        self.assertEqual(planned_j0.scheduled_date, datetime.date(2026, 8, 24))
        self.assertEqual(planned_j4.scheduled_date, datetime.date(2026, 8, 28))
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member).count(), 0)

    def test_reply_tuesday_blocks_scheduler_forever_but_j4_content_remains_validated(self):
        campaign, member = self._make_member("WeekPlanC", "00000000090003")
        prospect = member.prospect

        plan = build_week_plan(now=self.monday)
        for scheduled_date, cp, step in plan:
            prepare_planned_content(cp, step, scheduled_date)

        step1 = campaign.sequence.steps.get(order=1)
        step2 = campaign.sequence.steps.get(order=2)
        planned_j0 = PlannedEmailContent.objects.get(campaign_prospect=member, email_step=step1)
        planned_j4 = PlannedEmailContent.objects.get(campaign_prospect=member, email_step=step2)
        for planned in (planned_j0, planned_j4):
            send_test_email(member, planned, "contact-predict@predictneed-ia.com")
            planned.refresh_from_db()
            ok, reason = validate_planned_content(planned, None)
            self.assertTrue(ok, reason)

        result_j0 = advance_campaign_prospect(member.pk, now=self.monday)
        self.assertEqual(result_j0["action"], "email")

        # Réponse mardi -> arrête la séquence.
        tuesday = self.monday + datetime.timedelta(days=1)
        ContactLog.objects.create(prospect=prospect, channel="email", subject="Re: bonjour", message="...", outcome="replied")

        friday = self.monday + datetime.timedelta(days=4)
        result_j4 = advance_campaign_prospect(member.pk, now=friday)
        self.assertEqual(result_j4["action"], "stopped")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False, email_step=step2).count(), 0)

        planned_j4.refresh_from_db()
        self.assertEqual(planned_j4.status, "validated")  # contenu conservé, jamais supprimé

    def test_delayed_j0_success_still_enforces_real_4_day_delay_for_j4(self):
        """Le J4 est pré-préparé avec une date prévisionnelle (vendredi)
        calculée depuis le J0 de LUNDI. Si le J0 échoue et ne réussit
        réellement que mercredi, J4 ne doit JAMAIS partir avant 4 jours
        réels après ce succès réel — même si la date prévisionnelle
        (vendredi) est déjà atteinte."""
        campaign, member = self._make_member("WeekPlanD", "00000000090004")

        plan = build_week_plan(now=self.monday)
        for scheduled_date, cp, step in plan:
            prepare_planned_content(cp, step, scheduled_date)

        step1 = campaign.sequence.steps.get(order=1)
        step2 = campaign.sequence.steps.get(order=2)
        planned_j0 = PlannedEmailContent.objects.get(campaign_prospect=member, email_step=step1)
        planned_j4 = PlannedEmailContent.objects.get(campaign_prospect=member, email_step=step2)
        self.assertEqual(planned_j4.scheduled_date, datetime.date(2026, 8, 28))  # vendredi précalculé
        for planned in (planned_j0, planned_j4):
            send_test_email(member, planned, "contact-predict@predictneed-ia.com")
            planned.refresh_from_db()
            ok, reason = validate_planned_content(planned, None)
            self.assertTrue(ok, reason)

        # J0 échoue lundi (SMTP down).
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("SMTP down")):
            result_fail = advance_campaign_prospect(member.pk, now=self.monday)
        self.assertEqual(result_fail["action"], "email_failed")

        # Ne réussit RÉELLEMENT que mercredi (2 jours de retard).
        wednesday = self.monday + datetime.timedelta(days=2)
        result_success = advance_campaign_prospect(member.pk, now=wednesday)
        self.assertEqual(result_success["action"], "email")

        # Vendredi (date précalculée de J4) : seulement 2 jours réels
        # depuis le succès réel de J0 (mercredi) -> J4 ne part PAS.
        friday = self.monday + datetime.timedelta(days=4)
        result_friday = advance_campaign_prospect(member.pk, now=friday)
        self.assertEqual(result_friday["action"], "waiting")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False, email_step=step2).count(), 0)

        # 4 jours réels après le VRAI succès (mercredi) : J4 peut enfin partir.
        real_plus_4 = wednesday + datetime.timedelta(days=4)
        result_later = advance_campaign_prospect(member.pk, now=real_plus_4)
        self.assertEqual(result_later["action"], "email")
