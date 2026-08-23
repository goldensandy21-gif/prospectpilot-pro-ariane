"""Round D — DERNIERS VERROUS PRODUCTION EMAIL AUTOMATION (depuis ce6687a).

Couvre les sections A-J :
A) « Valider et programmer » rend la campagne réellement is_sendable ;
B) l'ancien flux d'envoi (campaign_validate/send_batch/send_test,
   send_campaign_batch) est interdit pour planning_managed=True ;
C) un e-mail de test ne peut jamais muter le vrai prospect (liens
   test-safe) ;
D) le threading (In-Reply-To/References) ignore absolument les envois de
   test, et répond au DERNIER envoi commercial, pas au premier ;
E) le traitement d'une réponse IMAP est une transaction atomique réelle,
   capable de réparer un état partiel préexistant ;
F) une séquence Planning finit toujours avec exactement 4 étapes actives ;
G) l'aperçu Planning affiche le contenu figé, jamais un nouveau rendu live ;
I) build_week_plan() ne prépare rien pour paused/cancelled/completed.
"""
import datetime
from email.message import Message
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import (
    Campaign,
    CampaignProspect,
    ContactLog,
    EmailAutomationSettings,
    EmailSend,
    EmailSequence,
    EmailStep,
    EmailVariant,
    EngagementEvent,
    PlannedEmailContent,
    Suppression,
)
from prospects.services.campaign_sequencing import advance_campaign_prospect
from prospects.services.email_automation import (
    adopt_campaign_into_planning,
    build_week_plan,
    normalize_planning_sequence,
    prepare_planned_content,
    promote_campaign_after_validation,
    send_test_email,
    validate_planned_content,
)
from prospects.services.inbound_replies import poll_inbound_replies

from .factories import make_compliance_profile, make_icp, make_product, make_prospect, make_public_email
from .test_mission8_correctif_audit import make_legacy_single_step_campaign
from .test_mission8_email_automation import make_planning_campaign


def _prepare_test_and_validate(member, step, scheduled_date, user=None):
    planned = prepare_planned_content(member, step, scheduled_date)
    send_test_email(member, planned, "contact-predict@predictneed-ia.com")
    planned.refresh_from_db()
    ok, reason = validate_planned_content(planned, user)
    assert ok, reason
    planned.refresh_from_db()
    return planned


class SendableAfterValidationTests(TestCase):
    """Section A."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="testerA", password="x")

    def test_adopted_draft_campaign_becomes_sendable_only_after_prepare_test_validate(self):
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        prospect = make_prospect()
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")

        adopt_campaign_into_planning(campaign)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "draft")
        self.assertIsNone(campaign.validated_at)
        self.assertFalse(campaign.is_sendable)

        step1 = campaign.sequence.steps.get(order=1)
        planned = prepare_planned_content(member, step1, timezone.now().date())
        campaign.refresh_from_db()
        self.assertFalse(campaign.is_sendable, "préparé mais pas encore testé/validé -> toujours pas sendable")

        ok, reason = validate_planned_content(planned, self.user)
        self.assertFalse(ok)
        self.assertEqual(reason, "test_required")
        campaign.refresh_from_db()
        self.assertFalse(campaign.is_sendable)

        send_test_email(member, planned, "contact-predict@predictneed-ia.com")
        planned.refresh_from_db()
        ok, reason = validate_planned_content(planned, self.user)
        self.assertTrue(ok, reason)

        # promote_campaign_after_validation est ce que la vue de production
        # appelle pour CHAQUE campagne ayant reçu >= 1 validation dans le lot.
        promote_campaign_after_validation(campaign, self.user)
        campaign.refresh_from_db()
        self.assertTrue(campaign.is_sendable)
        self.assertEqual(campaign.status, "ready")
        self.assertIsNotNone(campaign.validated_at)
        self.assertEqual(campaign.validated_by, self.user)

    def test_promotion_never_reuses_the_old_pre_adoption_validated_at(self):
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        old_validated_at = campaign.validated_at
        self.assertIsNotNone(old_validated_at)
        adopt_campaign_into_planning(campaign)
        campaign.refresh_from_db()
        self.assertIsNone(campaign.validated_at)

        promote_campaign_after_validation(campaign, self.user)
        campaign.refresh_from_db()
        self.assertGreater(campaign.validated_at, old_validated_at)
        self.assertEqual(campaign.validated_by, self.user)

    def test_view_promotes_campaign_after_successful_batch_validation(self):
        self.client = Client()
        self.client.force_login(self.user)
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        prospect = make_prospect()
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")
        adopt_campaign_into_planning(campaign)
        campaign.refresh_from_db()
        step1 = campaign.sequence.steps.get(order=1)
        planned = prepare_planned_content(member, step1, timezone.now().date())
        send_test_email(member, planned, "contact-predict@predictneed-ia.com")

        response = self.client.post(reverse("email_planning_validate_and_schedule"))
        self.assertEqual(response.status_code, 302)
        campaign.refresh_from_db()
        self.assertTrue(campaign.is_sendable)
        self.assertEqual(campaign.validated_by, self.user)

    def test_no_approved_content_leaves_campaign_not_sendable(self):
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        adopt_campaign_into_planning(campaign)
        campaign.refresh_from_db()
        self.assertFalse(campaign.is_sendable)
        # Aucune préparation/test/validation n'a eu lieu -> reste non-sendable.
        self.assertIsNone(campaign.validated_at)


class LegacyFlowBlockedForPlanningTests(TestCase):
    """Section B."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="testerB", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")

    def test_campaign_validate_refused_for_planning_managed_forged_post(self):
        old_validated_at = self.campaign.validated_at
        response = self.client.post(reverse("campaign_validate", args=[self.campaign.pk]))
        self.assertRedirects(response, reverse("email_planning"))
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.validated_at, old_validated_at)

    def test_campaign_send_batch_refused_for_planning_managed_forged_post(self):
        outbox_before = len(mail.outbox)
        response = self.client.post(reverse("campaign_send_batch", args=[self.campaign.pk]))
        self.assertRedirects(response, reverse("email_planning"))
        self.assertEqual(len(mail.outbox), outbox_before)
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member).count(), 0)

    def test_campaign_send_test_refused_for_planning_managed(self):
        outbox_before = len(mail.outbox)
        response = self.client.post(reverse("campaign_send_test", args=[self.campaign.pk]), {"test_email": "x@example.com"})
        self.assertRedirects(response, reverse("email_planning"))
        self.assertEqual(len(mail.outbox), outbox_before)

    def test_send_campaign_batch_direct_python_call_blocked_even_if_is_sendable(self):
        from prospects.services.campaign_sending import send_campaign_batch

        self.campaign.status = "ready"
        self.campaign.validated_at = timezone.now()
        self.campaign.save(update_fields=["status", "validated_at"])
        self.assertTrue(self.campaign.is_sendable)

        summary = send_campaign_batch(self.campaign)
        self.assertEqual(summary["sent"], 0)
        self.assertTrue(summary["errors"])
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member).count(), 0)


class TestEmailLinkSafetyTests(TestCase):
    """Section C."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        self.step1 = self.campaign.sequence.steps.get(order=1)

    def test_test_email_cta_has_no_real_tracking_link_and_zero_engagement(self):
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        test_record = send_test_email(self.member, planned, "contact-predict@predictneed-ia.com")
        self.assertNotIn(f"/t/{self.member.tracking_token}/", test_record.html_body)
        self.assertNotIn(f"/t/{self.member.tracking_token}/", test_record.text_body)
        self.assertEqual(EngagementEvent.objects.filter(event_type="link_clicked").count(), 0)

    def test_test_email_unsubscribe_link_is_neutralized_never_mutates_prospect(self):
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        test_record = send_test_email(self.member, planned, "contact-predict@predictneed-ia.com")
        # Le VRAI lien de désabonnement (spécifique au prospect) ne doit
        # jamais apparaître — la page de confidentialité, elle, peut
        # légitimement rester réelle (non-mutatrice, cf. section C) et
        # réutilise le même token : on vérifie donc précisément l'URL
        # d'unsubscribe, pas la simple présence du token quelque part.
        real_unsubscribe_path = reverse("unsubscribe", kwargs={"token": self.prospect.unsubscribe_token})
        self.assertNotIn(real_unsubscribe_path, test_record.html_body)
        self.assertIn(reverse("test_unsubscribe_preview"), test_record.html_body)

        response = self.client.get(reverse("test_unsubscribe_preview"))
        self.assertEqual(response.status_code, 200)

        self.prospect.refresh_from_db()
        self.assertTrue(self.prospect.prospecting_allowed)
        self.assertNotEqual(self.prospect.status, "do_not_contact")
        self.assertEqual(Suppression.objects.filter(prospect=self.prospect).count(), 0)
        self.assertEqual(
            CampaignProspect.objects.filter(pk=self.member.pk, status="do_not_contact").count(), 0,
        )

    def test_real_commercial_email_keeps_real_tracking_and_unsubscribe(self):
        planned = _prepare_test_and_validate(self.member, self.step1, timezone.now().date())
        advance_campaign_prospect(self.member.pk)
        record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")
        self.assertIn(f"/t/{self.member.tracking_token}/", record.html_body)
        self.assertIn(str(self.prospect.unsubscribe_token), record.html_body)


class ReplyThreadingIgnoresTestSendsTests(TestCase):
    """Section D."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        self.steps = list(self.campaign.sequence.steps.order_by("order"))

    def _advance(self, step, now, first_run=False):
        if not first_run:
            self.member.current_step_started_at = now - datetime.timedelta(days=10)
            self.member.save(update_fields=["current_step_started_at"])
        result = advance_campaign_prospect(self.member.pk, now=now)
        self.assertEqual(result["action"], "email")
        return EmailSend.objects.filter(campaign_prospect=self.member, is_test=False, email_step=step).latest("id")

    def test_full_j0_j4_j8_j14_threading_ignores_test_sends_and_targets_latest_commercial(self):
        step1, step2, step3, step4 = self.steps
        now = timezone.now()

        _prepare_test_and_validate(self.member, step1, now.date())
        real_j0 = self._advance(step1, now, first_run=True)
        self.assertFalse(real_j0.in_reply_to)

        now += datetime.timedelta(days=5)
        _prepare_test_and_validate(self.member, step2, now.date())
        test_j4 = EmailSend.objects.filter(campaign_prospect=self.member, is_test=True, email_step=step2).latest("id")
        real_j4 = self._advance(step2, now)
        self.assertEqual(real_j4.in_reply_to, real_j0.message_id)
        self.assertNotEqual(real_j4.in_reply_to, test_j4.message_id)

        now += datetime.timedelta(days=5)
        _prepare_test_and_validate(self.member, step3, now.date())
        real_j8 = self._advance(step3, now)
        self.assertEqual(real_j8.in_reply_to, real_j4.message_id)

        now += datetime.timedelta(days=5)
        _prepare_test_and_validate(self.member, step4, now.date())
        real_j14 = self._advance(step4, now)
        self.assertEqual(real_j14.in_reply_to, real_j8.message_id)

        test_message_ids = set(
            EmailSend.objects.filter(campaign_prospect=self.member, is_test=True)
            .exclude(message_id="").values_list("message_id", flat=True),
        )
        self.assertTrue(test_message_ids, "au moins un envoi de test doit avoir un message_id pour que ce test soit significatif")
        for real in (real_j0, real_j4, real_j8, real_j14):
            self.assertNotIn(real.in_reply_to, test_message_ids)
            for test_mid in test_message_ids:
                self.assertNotIn(test_mid, real.references)


class ImapAtomicReplyProcessingTests(TestCase):
    """Section E."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect, email="prospect@example.com")
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        step1 = self.campaign.sequence.steps.get(order=1)
        _prepare_test_and_validate(self.member, step1, timezone.now().date())
        advance_campaign_prospect(self.member.pk)
        self.record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")

    def _raw_message_bytes(self, message_id_header="<crash-d@example.com>"):
        msg = Message()
        msg["In-Reply-To"] = self.record.message_id
        msg["From"] = "prospect@example.com"
        msg["Subject"] = "Re: hello"
        msg["Message-ID"] = message_id_header
        return msg.as_bytes()

    def _fake_conn(self, raw_messages):
        conn = mock.MagicMock()
        conn.search.return_value = ("OK", [b" ".join(str(i + 1).encode() for i in range(len(raw_messages)))])
        conn.fetch.side_effect = [
            ("OK", [(f"{i+1} (BODY.PEEK[] {{{len(raw)}}}".encode(), raw)]) for i, raw in enumerate(raw_messages)
        ]
        return conn

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_crash_after_event_but_before_contactlog_is_repaired_on_next_poll(self, mock_imap_cls):
        # Simule EXACTEMENT le crash visé : EngagementEvent déjà créé
        # (contact_log_id encore None), ContactLog jamais créé.
        EngagementEvent.objects.create(
            campaign_prospect=self.member, prospect=self.prospect, campaign=self.campaign,
            event_type="email_replied", source="prospectpilot",
            metadata={"email_send_id": self.record.pk, "contact_log_id": None},
            idempotency_key="inbound-reply:<crash-d@example.com>",
        )
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").count(), 0)

        raw = self._raw_message_bytes()
        mock_imap_cls.return_value = self._fake_conn([raw])
        result = poll_inbound_replies()

        self.assertEqual(result["results"][0]["action"], "repaired")
        self.assertEqual(EngagementEvent.objects.filter(event_type="email_replied", prospect=self.prospect).count(), 1)
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").count(), 1)
        self.prospect.refresh_from_db()
        self.assertIsNotNone(self.prospect.last_replied_at)

        result2 = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result2["action"], "stopped")

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_fully_completed_message_is_never_reprocessed(self, mock_imap_cls):
        raw = self._raw_message_bytes(message_id_header="<complete-d@example.com>")
        mock_imap_cls.return_value = self._fake_conn([raw])
        result1 = poll_inbound_replies()
        self.assertEqual(result1["results"][0]["action"], "matched")

        mock_imap_cls.return_value = self._fake_conn([raw])
        result2 = poll_inbound_replies()
        self.assertEqual(result2["results"][0]["action"], "already_processed")
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").count(), 1)
        self.assertEqual(EngagementEvent.objects.filter(event_type="email_replied", prospect=self.prospect).count(), 1)


class ExactlyFourActiveStepsTests(TestCase):
    """Section F."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)

    def _make_six_step_sequence(self, name):
        sequence = EmailSequence.objects.create(product=self.product, icp=self.icp, name=name)
        for order in range(1, 7):
            step = EmailStep.objects.create(
                sequence=sequence, order=order, delay_days=(4 if order > 1 else 0),
                name=f"Etape {order}", channel="email", active=True,
            )
            EmailVariant.objects.create(step=step, name="Standard", subject_template="{{ company_name }} — standard", cta_type="simulator")
        return sequence

    def test_legacy_six_step_sequence_adopted_gives_exactly_four_active_steps(self):
        sequence = self._make_six_step_sequence("Legacy 6 etapes")
        campaign = Campaign.objects.create(
            name="Legacy 6 etapes camp", product=self.product, icp=self.icp, sequence=sequence,
            status="ready", validated_at=timezone.now(), daily_send_limit=10, total_limit=10, planning_managed=False,
        )

        adopt_campaign_into_planning(campaign)
        campaign.refresh_from_db()
        clone = campaign.sequence

        self.assertNotEqual(clone.pk, sequence.pk)
        active_orders = list(clone.steps.filter(active=True).order_by("order").values_list("order", flat=True))
        self.assertEqual(active_orders, [1, 2, 3, 4])
        self.assertEqual(clone.steps.count(), 6, "les étapes 5/6 sont clonées mais jamais actives, jamais supprimées")
        self.assertFalse(clone.steps.get(order=5).active)
        self.assertFalse(clone.steps.get(order=6).active)

        # L'originale reste totalement inchangée (jamais modifiée par l'adoption).
        sequence.refresh_from_db()
        self.assertEqual(sequence.steps.count(), 6)
        self.assertTrue(sequence.steps.get(order=5).active)
        self.assertTrue(sequence.steps.get(order=6).active)

    def test_sequence_completes_after_j14_never_reaches_step_5(self):
        sequence = self._make_six_step_sequence("Legacy 6 direct")
        normalize_planning_sequence(sequence)  # séquence dédiée, jamais partagée : sûr à normaliser directement
        campaign = Campaign.objects.create(
            name="Legacy 6 direct camp", product=self.product, icp=self.icp, sequence=sequence,
            status="ready", validated_at=timezone.now(), planning_managed=True, daily_send_limit=100, total_limit=100,
        )
        prospect = make_prospect()
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")

        now = timezone.now()
        for order in (1, 2, 3, 4):
            step = campaign.sequence.steps.get(order=order)
            _prepare_test_and_validate(member, step, now.date())
            member.current_step_started_at = now - datetime.timedelta(days=10)
            member.save(update_fields=["current_step_started_at"])
            result = advance_campaign_prospect(member.pk, now=now)
            self.assertEqual(result["action"], "email")
            now += datetime.timedelta(days=5)

        result_final = advance_campaign_prospect(member.pk, now=now)
        self.assertEqual(result_final["action"], "sequence_complete")


class PlanningPreviewShowsFrozenContentTests(TestCase):
    """Section G."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="testerG", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        self.step1 = self.campaign.sequence.steps.get(order=1)

    def test_preview_shows_frozen_content_not_a_live_rerender(self):
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        self.prospect.name = "NOM CHANGE APRES PREPARATION"
        self.prospect.save(update_fields=["name"])

        response = self.client.get(reverse("email_planning_preview", args=[self.member.pk]), {"step": self.step1.pk})
        self.assertEqual(response.status_code, 200)
        selected = response.context["selected"]
        self.assertEqual(selected["planned"].pk, planned.pk)
        self.assertEqual(selected["planned"].html_body, planned.html_body)
        self.assertNotIn("NOM CHANGE APRES PREPARATION", selected["planned"].html_body)

    def test_preview_lists_all_four_steps(self):
        response = self.client.get(reverse("email_planning_preview", args=[self.member.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 4)


class PausedCancelledCompletedNotPreparedTests(TestCase):
    """Section I."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        EmailAutomationSettings.objects.create(new_contacts_per_day=10, daily_total_limit=100)
        self.monday = datetime.datetime(2026, 8, 24, 8, 0, tzinfo=datetime.timezone.utc)

    def _make_contact(self, status, i):
        campaign = make_planning_campaign(self.product, self.icp, name=f"CampD-{status}", status=status)
        prospect = make_prospect(name=f"PD-{status}", siret=f"0000000001{i:04d}")
        make_public_email(prospect, email=f"pd-{status}@example.com")
        CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")
        return campaign

    def test_paused_cancelled_completed_excluded_draft_included(self):
        for i, status in enumerate(["paused", "cancelled", "completed", "draft"]):
            self._make_contact(status, i)

        plan = build_week_plan(now=self.monday)
        planned_campaign_ids = {cp.campaign_id for _date, cp, _step in plan}

        draft_campaign = Campaign.objects.get(name="CampD-draft")
        self.assertIn(draft_campaign.pk, planned_campaign_ids)
        for status in ("paused", "cancelled", "completed"):
            camp = Campaign.objects.get(name=f"CampD-{status}")
            self.assertNotIn(camp.pk, planned_campaign_ids)
