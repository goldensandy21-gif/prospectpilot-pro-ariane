"""Mission 8 — automatisation email commerciale planifiée (J0/J4/J8/J14).

Couvre : dates déterministes + report week-end, fenêtre Europe/Paris,
validation obligatoire avant tout envoi automatique, contenu figé après
validation, verrou anti-doublon global (sélection + inscription + pré-SMTP),
idempotence du scheduler, limites new/day et total/day, relances,
conditions d'arrêt (réponse/désabonnement/conversion), tracking d'ouverture
indicatif sans pollution des tests, détection des réponses entrantes (IMAP,
logique pure), template + footer toujours présents dans le contenu figé.
"""
import datetime
from email.message import Message
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import (
    AgentBrief,
    Campaign,
    CampaignProspect,
    ContactLog,
    ConversionEvent,
    EmailAutomationSettings,
    EmailSend,
    EmailSequence,
    EmailStep,
    EmailVariant,
    EngagementEvent,
    PlannedEmailContent,
    Prospect,
    Suppression,
)
from prospects.services.campaign_sequencing import advance_campaign_prospect
from prospects.services.email_automation import (
    compute_scheduled_date,
    content_hash_for,
    filter_out_already_contacted,
    freeze_planned_content,
    has_prior_commercial_first_contact,
    is_content_stale,
    is_within_send_window,
    run_planning_scheduler,
)
from prospects.services.inbound_replies import match_email_send_for_reply, process_inbound_message

from .factories import make_campaign, make_compliance_profile, make_icp, make_product, make_prospect, make_public_email


def make_j0_j4_j8_j14_sequence(product, icp):
    """Séquence à 4 étapes email, delay_days 0/4/4/6 -> cumul J0/J4/J8/J14."""
    sequence = EmailSequence.objects.create(product=product, icp=icp, name=f"J0-J14 {icp.name}")
    specs = [
        (1, 0, "Premier contact", "J0 — {{ company_name }} — une observation sur votre parcours visiteurs"),
        (2, 4, "Rappel court", "J4 — {{ company_name }} — un rappel rapide"),
        (3, 4, "Relance valeur", "J8 — {{ company_name }} — un autre angle"),
        (4, 6, "Dernier message", "J14 — {{ company_name }} — dernier message"),
    ]
    for order, delay, name, subject in specs:
        step = EmailStep.objects.create(sequence=sequence, order=order, delay_days=delay, name=name, channel="email")
        EmailVariant.objects.create(step=step, name=name, subject_template=subject, cta_type="simulator")
    return sequence


def make_planning_campaign(product, icp, **overrides):
    sequence = make_j0_j4_j8_j14_sequence(product, icp)
    defaults = {
        "name": "Planning test", "objective": "Test", "status": "ready",
        "daily_send_limit": 100, "total_limit": 100, "sequence": sequence,
        "planning_managed": True, "validated_at": timezone.now(),
    }
    defaults.update(overrides)
    return Campaign.objects.create(product=product, icp=icp, **defaults)


class DeterministicDatesTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.sequence = make_j0_j4_j8_j14_sequence(self.product, self.icp)
        self.steps = list(self.sequence.steps.order_by("order"))

    def test_cumulative_delays_are_j0_j4_j8_j14(self):
        monday = datetime.date(2026, 8, 24)  # un lundi
        expected_offsets = [0, 4, 8, 14]
        for step, offset in zip(self.steps, expected_offsets):
            scheduled = compute_scheduled_date(monday, self.sequence, step)
            self.assertEqual((scheduled - monday).days, offset if (monday + datetime.timedelta(days=offset)).weekday() < 5 else offset + (7 - (monday + datetime.timedelta(days=offset)).weekday()))

    def test_weekend_date_shifts_to_next_business_day(self):
        # Un lundi + 4 jours = vendredi (jour ouvré) -> pas de décalage
        monday = datetime.date(2026, 8, 24)
        step_j4 = self.steps[1]
        scheduled = compute_scheduled_date(monday, self.sequence, step_j4)
        self.assertEqual(scheduled.weekday(), 4)  # vendredi

        # Un mercredi + 4 jours = dimanche -> décalé au lundi suivant
        wednesday = datetime.date(2026, 8, 26)
        scheduled2 = compute_scheduled_date(wednesday, self.sequence, step_j4)
        self.assertEqual(scheduled2.weekday(), 0)  # lundi
        self.assertGreater(scheduled2, wednesday + datetime.timedelta(days=4))

    def test_never_falls_on_saturday_or_sunday(self):
        for start_offset in range(14):
            start = datetime.date(2026, 8, 24) + datetime.timedelta(days=start_offset)
            for step in self.steps:
                scheduled = compute_scheduled_date(start, self.sequence, step)
                self.assertLess(scheduled.weekday(), 5)


class SendWindowTests(TestCase):
    def test_within_window_on_weekday(self):
        settings_row = EmailAutomationSettings.objects.create()
        # Lundi 2026-08-24, 10:00 Paris -> UTC 08:00 (CEST = UTC+2)
        now = datetime.datetime(2026, 8, 24, 8, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(is_within_send_window(now, settings_row))

    def test_outside_window_before_open(self):
        settings_row = EmailAutomationSettings.objects.create()
        now = datetime.datetime(2026, 8, 24, 6, 0, tzinfo=datetime.timezone.utc)  # 08:00 Paris
        self.assertFalse(is_within_send_window(now, settings_row))

    def test_never_within_window_on_saturday_or_sunday(self):
        settings_row = EmailAutomationSettings.objects.create()
        saturday = datetime.datetime(2026, 8, 22, 8, 0, tzinfo=datetime.timezone.utc)
        sunday = datetime.datetime(2026, 8, 23, 8, 0, tzinfo=datetime.timezone.utc)
        self.assertFalse(is_within_send_window(saturday, settings_row))
        self.assertFalse(is_within_send_window(sunday, settings_row))


class GlobalAntiDuplicateFirstContactTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)

    def _sent_first_contact(self, prospect, campaign):
        sequence = campaign.sequence
        step1 = sequence.steps.get(order=1)
        variant = step1.variants.first()
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")
        from prospects.services.predictneed_email import send_predictneed_campaign_email
        record = send_predictneed_campaign_email(member, email_step=step1, email_variant=variant)
        self.assertEqual(record.status, "sent")
        return member

    def test_no_prior_contact_returns_false(self):
        prospect = make_prospect()
        make_public_email(prospect)
        self.assertFalse(has_prior_commercial_first_contact(prospect))

    def test_same_campaign_prior_first_contact_detected(self):
        prospect = make_prospect()
        make_public_email(prospect)
        campaign = make_planning_campaign(self.product, self.icp)
        self._sent_first_contact(prospect, campaign)
        self.assertTrue(has_prior_commercial_first_contact(prospect))

    def test_other_campaign_prior_first_contact_detected(self):
        prospect = make_prospect()
        make_public_email(prospect)
        campaign_a = make_planning_campaign(self.product, self.icp, name="A")
        self._sent_first_contact(prospect, campaign_a)
        campaign_b = make_planning_campaign(self.product, self.icp, name="B")
        # Un deuxième prospect "identique" (même adresse) inscrit dans une autre campagne.
        self.assertTrue(has_prior_commercial_first_contact(prospect))
        self.assertEqual(campaign_b.campaign_prospects.count(), 0)

    def test_same_email_different_prospect_record_detected(self):
        prospect1 = make_prospect(name="Original")
        make_public_email(prospect1, email="shared@example.com")
        campaign = make_planning_campaign(self.product, self.icp)
        self._sent_first_contact(prospect1, campaign)

        prospect2 = make_prospect(name="Doublon", siret="00000000000088")
        make_public_email(prospect2, email="shared@example.com")
        self.assertTrue(has_prior_commercial_first_contact(prospect2))

    def test_is_test_send_does_not_count_as_first_contact(self):
        prospect = make_prospect()
        make_public_email(prospect)
        campaign = make_planning_campaign(self.product, self.icp)
        step1 = campaign.sequence.steps.get(order=1)
        variant = step1.variants.first()
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="selected")
        from prospects.services.predictneed_email import send_predictneed_campaign_email
        record = send_predictneed_campaign_email(
            member, email_step=step1, email_variant=variant, is_test=True, test_recipient="contact-predict@predictneed-ia.com",
        )
        self.assertEqual(record.status, "sent")
        self.assertFalse(has_prior_commercial_first_contact(prospect))

    def test_legitimate_followup_is_authorized(self):
        """Une relance J4/J8/J14 de LA séquence en cours n'est jamais bloquée
        par le verrou anti-doublon (qui ne vise que step.order == 1)."""
        prospect = make_prospect()
        make_public_email(prospect)
        campaign = make_planning_campaign(self.product, self.icp)
        member = self._sent_first_contact(prospect, campaign)
        # Le prospect a désormais un premier contact -> le verrou global est vrai...
        self.assertTrue(has_prior_commercial_first_contact(prospect))
        # ...mais la relance (step order=2, PAS un "premier contact") doit pouvoir être validée et envoyée normalement.
        step2 = campaign.sequence.steps.get(order=2)
        planned = freeze_planned_content(member, step2, user=None, scheduled_date=timezone.now().date())
        self.assertEqual(planned.status, "validated")

    def test_duplicate_first_contact_blocked_at_pre_smtp_guard(self):
        """Double garde avant SMTP : même si un bug de sélection en amont
        inscrit à tort un prospect déjà contacté dans une nouvelle campagne
        planning_managed, l'envoi du step 1 est bloqué juste avant SMTP."""
        prospect = make_prospect()
        make_public_email(prospect)
        campaign_a = make_planning_campaign(self.product, self.icp, name="A")
        self._sent_first_contact(prospect, campaign_a)

        campaign_b = make_planning_campaign(self.product, self.icp, name="B")
        step1_b = campaign_b.sequence.steps.get(order=1)
        member_b = CampaignProspect.objects.create(campaign=campaign_b, prospect=prospect, status="ready_to_contact")
        # Contenu validé quand même (simulateur d'un bug de préparation) —
        # le verrou pré-SMTP doit bloquer indépendamment de la validation.
        freeze_planned_content(member_b, step1_b, user=None, scheduled_date=timezone.now().date())

        result = advance_campaign_prospect(member_b.pk)
        self.assertEqual(result["action"], "blocked_duplicate_first_contact")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member_b, is_test=False).count(), 0)

    def test_filter_out_already_contacted_selection_guard(self):
        prospect_new = make_prospect(name="Nouveau")
        make_public_email(prospect_new, email="nouveau@example.com")
        prospect_old = make_prospect(name="Deja contacte", siret="00000000000077")
        make_public_email(prospect_old, email="ancien@example.com")
        campaign = make_planning_campaign(self.product, self.icp)
        self._sent_first_contact(prospect_old, campaign)

        result = filter_out_already_contacted([prospect_new, prospect_old])
        self.assertEqual(result, [prospect_new])


class ValidationRequiredAndFrozenContentTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        self.step1 = self.campaign.sequence.steps.get(order=1)

    def test_no_planned_content_blocks_automatic_send(self):
        result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "blocked_awaiting_validation")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member).count(), 0)

    def test_validated_content_is_frozen_and_used_verbatim(self):
        planned = freeze_planned_content(self.member, self.step1, user=None, scheduled_date=timezone.now().date())
        self.assertEqual(planned.status, "validated")
        original_subject = planned.subject

        result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "email")
        record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")
        self.assertEqual(record.subject, original_subject)
        # Section 5 (audit correctif final) : le pixel d'ouverture n'est
        # injecté qu'au moment du vrai envoi — record.html_body est donc
        # planned.html_body + pixel, jamais strictement identique.
        from prospects.services.predictneed_email import inject_open_pixel
        self.assertEqual(record.html_body, inject_open_pixel(planned.html_body, record.open_tracking_token))
        self.assertEqual(record.text_body, planned.text_body)

    def test_content_becomes_stale_when_prospect_changes_after_validation(self):
        planned = freeze_planned_content(self.member, self.step1, user=None, scheduled_date=timezone.now().date())
        self.assertFalse(is_content_stale(planned))

        self.prospect.name = "Nom Modifié Après Validation"
        self.prospect.save(update_fields=["name"])

        self.assertTrue(is_content_stale(planned))
        result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "blocked_awaiting_validation")

    def test_manual_non_planning_campaign_is_never_gated_by_validation(self):
        """Une campagne manuelle (planning_managed=False, comportement
        d'avant cette mission) continue d'envoyer sans PlannedEmailContent —
        rien de cassé pour les campagnes existantes."""
        legacy_campaign = make_campaign(self.product, self.icp)
        legacy_sequence = make_j0_j4_j8_j14_sequence(self.product, self.icp)
        legacy_campaign.sequence = legacy_sequence
        legacy_campaign.status = "ready"
        legacy_campaign.validated_at = timezone.now()
        legacy_campaign.save(update_fields=["sequence", "status", "validated_at"])
        prospect2 = make_prospect(name="Legacy", siret="00000000000066")
        make_public_email(prospect2)
        member2 = CampaignProspect.objects.create(campaign=legacy_campaign, prospect=prospect2, status="ready_to_contact")

        result = advance_campaign_prospect(member2.pk)
        self.assertEqual(result["action"], "email")


class SchedulerIdempotenceAndLimitsTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.settings_row = EmailAutomationSettings.objects.create(new_contacts_per_day=2, daily_total_limit=3)

    def _make_ready_member(self, name, siret):
        campaign = make_planning_campaign(self.product, self.icp, name=f"Camp {name}")
        prospect = make_prospect(name=name, siret=siret)
        make_public_email(prospect, email=f"{name.lower()}@example.com")
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")
        step1 = campaign.sequence.steps.get(order=1)
        freeze_planned_content(member, step1, user=None, scheduled_date=timezone.now().date())
        return member

    def test_scheduler_does_nothing_outside_window(self):
        member = self._make_ready_member("Weekend", "00000000000001")
        saturday = datetime.datetime(2026, 8, 22, 8, 0, tzinfo=datetime.timezone.utc)
        result = run_planning_scheduler(now=saturday)
        self.assertEqual(result["action"], "outside_window")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False).count(), 0)

    def test_scheduler_sends_within_window(self):
        self._make_ready_member("Alpha", "00000000000002")
        monday_10am = datetime.datetime(2026, 8, 24, 8, 0, tzinfo=datetime.timezone.utc)
        result = run_planning_scheduler(now=monday_10am)
        self.assertEqual(result["action"], "ran")
        self.assertEqual(EmailSend.objects.filter(is_test=False, status="sent").count(), 1)

    def test_rerunning_scheduler_never_double_sends(self):
        """Idempotence : rejouer le scheduler (simulateur de redémarrage/
        retry) après un envoi réussi ne doit jamais renvoyer une deuxième
        fois la même étape."""
        self._make_ready_member("Beta", "00000000000003")
        now = datetime.datetime(2026, 8, 24, 8, 0, tzinfo=datetime.timezone.utc)
        run_planning_scheduler(now=now)
        run_planning_scheduler(now=now)
        run_planning_scheduler(now=now)
        self.assertEqual(EmailSend.objects.filter(is_test=False, status="sent").count(), 1)

    def test_new_contacts_per_day_limit_defers_extra_first_contacts(self):
        for i in range(3):
            self._make_ready_member(f"Gamma{i}", f"0000000000001{i}")
        now = datetime.datetime(2026, 8, 24, 8, 0, tzinfo=datetime.timezone.utc)
        run_planning_scheduler(now=now)
        # new_contacts_per_day=2 -> seulement 2 des 3 premiers contacts partent.
        self.assertEqual(EmailSend.objects.filter(is_test=False, status="sent").count(), 2)

    def test_daily_total_limit_defers_beyond_cap_including_followups(self):
        """Les relances déjà dues comptent dans le volume total — jamais
        supprimées, seulement reportées."""
        self.settings_row.new_contacts_per_day = 10
        self.settings_row.daily_total_limit = 1
        self.settings_row.save(update_fields=["new_contacts_per_day", "daily_total_limit"])
        self._make_ready_member("Delta", "00000000000021")
        self._make_ready_member("Epsilon", "00000000000022")
        now = datetime.datetime(2026, 8, 24, 8, 0, tzinfo=datetime.timezone.utc)
        run_planning_scheduler(now=now)
        self.assertEqual(EmailSend.objects.filter(is_test=False, status="sent").count(), 1)
        # Le second reste dû (jamais supprimé) : rejouer le lendemain l'enverrait.
        self.assertEqual(CampaignProspect.objects.exclude(current_step__isnull=True).count(), 1)


class StopConditionsTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        self.step1 = self.campaign.sequence.steps.get(order=1)
        freeze_planned_content(self.member, self.step1, user=None, scheduled_date=timezone.now().date())
        advance_campaign_prospect(self.member.pk)  # envoie J0

    def test_reply_stops_future_relaunches(self):
        ContactLog.objects.create(prospect=self.prospect, channel="email", subject="Re: bonjour", message="...", outcome="replied")
        result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "stopped")
        self.assertIn("répondu", result["reason"].lower())

    def test_unsubscribe_stops_future_relaunches(self):
        Suppression.objects.create(prospect=self.prospect, email=self.prospect.public_email, reason="Désabonnement")
        result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "stopped")

    def test_conversion_stops_future_relaunches(self):
        ConversionEvent.objects.create(prospect=self.prospect, event_type="signup_completed")
        result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "stopped")

    def test_open_alone_does_not_stop_sequence(self):
        record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")
        record.open_tracking_token = "tok123"
        record.save(update_fields=["open_tracking_token"])
        self.client.get(reverse("track_email_open", kwargs={"token": "tok123"}))
        step2 = self.campaign.sequence.steps.get(order=2)
        freeze_planned_content(self.member, step2, user=None, scheduled_date=timezone.now().date())
        self.member.current_step_started_at = timezone.now() - datetime.timedelta(days=5)
        self.member.save(update_fields=["current_step_started_at"])
        later = timezone.now() + datetime.timedelta(days=5)
        result = advance_campaign_prospect(self.member.pk, now=later)
        self.assertEqual(result["action"], "email")  # la relance part normalement

    def test_click_alone_does_not_stop_sequence(self):
        self.client.get(reverse("campaign_click", kwargs={"token": self.member.tracking_token}), {"cta": "simulator"})
        step2 = self.campaign.sequence.steps.get(order=2)
        freeze_planned_content(self.member, step2, user=None, scheduled_date=timezone.now().date())
        self.member.current_step_started_at = timezone.now() - datetime.timedelta(days=5)
        self.member.save(update_fields=["current_step_started_at"])
        later = timezone.now() + datetime.timedelta(days=5)
        result = advance_campaign_prospect(self.member.pk, now=later)
        self.assertEqual(result["action"], "email")


class OpenTrackingTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        self.step1 = self.campaign.sequence.steps.get(order=1)

    def test_real_send_embeds_a_pixel_and_creates_token(self):
        freeze_planned_content(self.member, self.step1, user=None, scheduled_date=timezone.now().date())
        advance_campaign_prospect(self.member.pk)
        record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")
        self.assertTrue(record.open_tracking_token)
        self.assertIn(record.open_tracking_token, record.html_body)

    def test_first_open_creates_engagement_event_and_dedupes(self):
        freeze_planned_content(self.member, self.step1, user=None, scheduled_date=timezone.now().date())
        advance_campaign_prospect(self.member.pk)
        record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")

        self.client.get(reverse("track_email_open", kwargs={"token": record.open_tracking_token}))
        self.client.get(reverse("track_email_open", kwargs={"token": record.open_tracking_token}))
        self.client.get(reverse("track_email_open", kwargs={"token": record.open_tracking_token}))

        record.refresh_from_db()
        self.assertIsNotNone(record.first_opened_at)
        self.assertEqual(record.open_count, 3)
        self.assertEqual(EngagementEvent.objects.filter(event_type="email_opened", campaign_prospect=self.member).count(), 1)

    def test_test_send_never_gets_a_pixel_or_pollutes_commercial_data(self):
        from prospects.services.predictneed_email import send_predictneed_campaign_email
        record = send_predictneed_campaign_email(
            self.member, email_step=self.step1, email_variant=self.step1.variants.first(),
            is_test=True, test_recipient="contact-predict@predictneed-ia.com",
        )
        self.assertEqual(record.open_tracking_token, "")
        self.assertNotIn("width=\"1\" height=\"1\"", record.html_body)
        self.assertEqual(EngagementEvent.objects.filter(event_type="email_opened").count(), 0)

    def test_reopening_test_never_increases_real_open_count(self):
        """Section 5 (audit correctif final) — le test n'embarque plus jamais
        de pixel : le rouvrir avant OU après le vrai envoi commercial ne
        doit jamais faire varier open_count du vrai EmailSend, et le vrai
        pixel n'est jamais le même token que celui (absent) du test."""
        planned = freeze_planned_content(self.member, self.step1, user=None, scheduled_date=timezone.now().date())
        test_record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=True).latest("id")
        self.assertEqual(test_record.open_tracking_token, "")
        self.assertNotIn("width=\"1\" height=\"1\"", test_record.html_body)

        advance_campaign_prospect(self.member.pk)
        real_record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")
        self.assertTrue(real_record.open_tracking_token)
        self.assertNotEqual(real_record.open_tracking_token, test_record.open_tracking_token)
        self.assertEqual(real_record.open_count, 0)

        # Aucun token exploitable côté test -> rien à "rouvrir" qui puisse
        # jamais toucher le vrai envoi, avant ou après coup.
        response = self.client.get(reverse("track_email_open", kwargs={"token": "test-had-no-real-token"}))
        self.assertEqual(response.status_code, 200)
        real_record.refresh_from_db()
        self.assertEqual(real_record.open_count, 0)

        self.client.get(reverse("track_email_open", kwargs={"token": real_record.open_tracking_token}))
        real_record.refresh_from_db()
        self.assertEqual(real_record.open_count, 1)

        response = self.client.get(reverse("track_email_open", kwargs={"token": "test-had-no-real-token"}))
        self.assertEqual(response.status_code, 200)
        real_record.refresh_from_db()
        self.assertEqual(real_record.open_count, 1)  # inchangé après "réouverture" du test

    def test_unknown_token_is_harmless(self):
        response = self.client.get(reverse("track_email_open", kwargs={"token": "does-not-exist"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/gif")


class NewTemplateAndFooterAlwaysPresentInFrozenContentTests(TestCase):
    def test_frozen_content_still_uses_new_template_and_footer(self):
        product = make_product()
        make_compliance_profile(product)
        icp = make_icp(product)
        campaign = make_planning_campaign(product, icp)
        prospect = make_prospect()
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")
        step1 = campaign.sequence.steps.get(order=1)

        planned = freeze_planned_content(member, step1, user=None, scheduled_date=timezone.now().date())

        self.assertIn("PredictNeed IA", planned.html_body)
        self.assertIn("Comportements observés", planned.html_body)
        self.assertIn("Se désabonner", planned.html_body)
        self.assertIn("Se désabonner", planned.text_body)
        # Section 5 (audit correctif final) — AUCUN pixel dans le contenu
        # préparé/testé : il n'est injecté qu'au moment du vrai envoi
        # commercial (voir OpenTrackingTests.test_real_send_embeds_a_pixel_and_creates_token).
        self.assertEqual(planned.html_body.count("<img"), 0)


class InboundReplyMatchingTests(TestCase):
    """Logique pure (aucune connexion IMAP réelle) — testable avec des
    email.message.Message construits à la main."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect, email="prospect@example.com")
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        step1 = self.campaign.sequence.steps.get(order=1)
        freeze_planned_content(self.member, step1, user=None, scheduled_date=timezone.now().date())
        advance_campaign_prospect(self.member.pk)
        self.record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")

    def _msg(self, **headers):
        msg = Message()
        for k, v in headers.items():
            msg[k] = v
        return msg

    def test_matches_via_in_reply_to(self):
        msg = self._msg(**{"In-Reply-To": self.record.message_id, "From": "prospect@example.com", "Subject": "Re: hello"})
        found = match_email_send_for_reply(msg.get("In-Reply-To"), msg.get("References"), "prospect@example.com", msg.get("Subject"))
        self.assertEqual(found.pk, self.record.pk)

    def test_matches_via_references_fallback(self):
        msg = self._msg(**{"References": f"<something-else@x> {self.record.message_id}", "From": "prospect@example.com"})
        found = match_email_send_for_reply("", msg.get("References"), "prospect@example.com", "")
        self.assertEqual(found.pk, self.record.pk)

    def test_unambiguous_sender_and_subject_fallback(self):
        found = match_email_send_for_reply("", "", "prospect@example.com", self.record.subject)
        self.assertEqual(found.pk, self.record.pk)

    def test_ambiguous_sender_alone_does_not_match(self):
        # Une deuxième EmailSend "sent" vers la même adresse, sujet différent -> ambigu sans sujet.
        EmailSend.objects.create(
            prospect=self.prospect, to_email="prospect@example.com", subject="Autre sujet totalement différent",
            html_body="x", text_body="x", status="sent", is_test=False,
        )
        found = match_email_send_for_reply("", "", "prospect@example.com", "")
        self.assertIsNone(found)

    def test_process_inbound_message_stops_sequence_and_logs(self):
        msg = self._msg(**{"In-Reply-To": self.record.message_id, "From": "prospect@example.com", "Subject": "Re: hello"})
        result = process_inbound_message(msg)
        self.assertEqual(result["action"], "matched")

        self.prospect.refresh_from_db()
        self.assertIsNotNone(self.prospect.last_replied_at)
        self.assertEqual(self.prospect.status, "replied")
        self.assertTrue(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").exists())
        self.assertTrue(EngagementEvent.objects.filter(event_type="email_replied", prospect=self.prospect).exists())

        # L'arrêt de séquence existant se déclenche automatiquement (réutilisé, pas dupliqué).
        seq_result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(seq_result["action"], "stopped")

    def test_process_inbound_message_never_sends_a_reply(self):
        from django.core import mail
        msg = self._msg(**{"In-Reply-To": self.record.message_id, "From": "prospect@example.com", "Subject": "Re: hello"})
        outbox_before = len(mail.outbox)
        process_inbound_message(msg)
        self.assertEqual(len(mail.outbox), outbox_before)

    def test_unmatched_message_is_ignored_safely(self):
        msg = self._msg(**{"From": "inconnu@example.com", "Subject": "Sans rapport"})
        result = process_inbound_message(msg)
        self.assertEqual(result["action"], "unmatched")


class DNCAndSuppressionUnaffectedTests(TestCase):
    """Confirme que le comportement DNC/suppression existant reste
    strictement inchangé par cette mission."""

    def test_suppressed_prospect_still_blocks_planning_managed_send(self):
        product = make_product()
        make_compliance_profile(product)
        icp = make_icp(product)
        campaign = make_planning_campaign(product, icp)
        prospect = make_prospect(prospecting_allowed=False)
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")
        step1 = campaign.sequence.steps.get(order=1)
        freeze_planned_content(member, step1, user=None, scheduled_date=timezone.now().date())

        result = advance_campaign_prospect(member.pk)
        # prospecting_allowed=False n'est pas un des motifs vérifiés par
        # _stop_reason() (qui couvre do_not_contact/paying/churned/Suppression/
        # stop_on_*) — il est re-détecté par is_suppressed() juste avant SMTP
        # (correctif d'audit round 3), donc "email_suppressed" ici, pas
        # "stopped". Dans les deux cas : aucun email réellement envoyé.
        self.assertEqual(result["action"], "email_suppressed")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False, status="sent").count(), 0)
