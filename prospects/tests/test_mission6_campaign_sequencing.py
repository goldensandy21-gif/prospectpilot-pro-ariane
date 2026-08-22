from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from prospects.models import (
    Campaign,
    CampaignProspect,
    ContactLog,
    ContactPerson,
    ConversionEvent,
    EmailSend,
    EmailSequence,
    EmailStep,
    EmailVariant,
    Suppression,
)
from prospects.services.campaign_sequencing import advance_campaign_prospect, run_campaign_sequences
from prospects.services.linkedin_orchestration import record_invitation_accepted, record_invitation_declined
from prospects.services.linkedin_provider import MockLinkedInProvider
from prospects.tests.factories import make_campaign, make_campaign_prospect, make_icp, make_prospect, make_product, make_public_email


def _multichannel_sequence(product):
    sequence = EmailSequence.objects.create(product=product, name="Multicanal test")
    connect = EmailStep.objects.create(sequence=sequence, order=1, delay_days=0, channel="linkedin_connect", name="Invitation")
    message = EmailStep.objects.create(
        sequence=sequence, order=2, delay_days=2, channel="linkedin_message",
        advance_condition="linkedin_accepted", name="Message",
    )
    email_step = EmailStep.objects.create(sequence=sequence, order=3, delay_days=4, channel="email", name="Repli e-mail")
    EmailVariant.objects.create(step=email_step, name="Repli", subject_template="{{ company_name }} — suite")
    return sequence, connect, message, email_step


def _validated_campaign(product, icp, **overrides):
    """Campagne active + validée explicitement (is_sendable) — l'état requis
    en production pour qu'une séquence puisse produire une action réelle."""
    defaults = {"status": "active"}
    defaults.update(overrides)
    campaign = make_campaign(product, icp=icp, **defaults)
    campaign.validated_at = timezone.now()
    campaign.save(update_fields=["validated_at"])
    return campaign


def _with_linkedin_contact(prospect):
    ContactPerson.objects.create(
        prospect=prospect, full_name="Alex Dupont", profile_url="https://linkedin.com/in/alex-dupont", is_active=True,
    )


class StopConditionsTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.campaign = _validated_campaign(self.product, self.icp)
        self.sequence, self.connect, self.message, self.email_step = _multichannel_sequence(self.product)
        self.campaign.sequence = self.sequence
        self.campaign.save(update_fields=["sequence"])

    def test_stops_on_do_not_contact_status(self):
        prospect = make_prospect(status="do_not_contact")
        cp = make_campaign_prospect(self.campaign, prospect)
        result = advance_campaign_prospect(cp.pk)
        self.assertEqual(result["action"], "stopped")

    def test_stops_on_active_suppression(self):
        prospect = make_prospect()
        Suppression.objects.create(prospect=prospect, active=True, reason="test")
        cp = make_campaign_prospect(self.campaign, prospect)
        result = advance_campaign_prospect(cp.pk)
        self.assertEqual(result["action"], "stopped")

    def test_stops_on_reply(self):
        prospect = make_prospect()
        cp = make_campaign_prospect(self.campaign, prospect)
        ContactLog.objects.create(prospect=prospect, channel="email", outcome="replied")
        result = advance_campaign_prospect(cp.pk)
        self.assertEqual(result["action"], "stopped")
        self.assertIn("répondu", result["reason"])

    def test_stops_on_conversion(self):
        prospect = make_prospect()
        cp = make_campaign_prospect(self.campaign, prospect)
        ConversionEvent.objects.create(prospect=prospect, event_type="signup")
        result = advance_campaign_prospect(cp.pk)
        self.assertEqual(result["action"], "stopped")
        self.assertIn("Conversion", result["reason"])

    def test_stops_on_optout(self):
        prospect = make_prospect()
        cp = make_campaign_prospect(self.campaign, prospect)
        ContactLog.objects.create(prospect=prospect, channel="email", outcome="optout")
        result = advance_campaign_prospect(cp.pk)
        self.assertEqual(result["action"], "stopped")

    def test_stops_when_already_paying_client(self):
        prospect = make_prospect(predictneed_stage="paying")
        cp = make_campaign_prospect(self.campaign, prospect)
        result = advance_campaign_prospect(cp.pk)
        self.assertEqual(result["action"], "stopped")
        self.assertIn("payant", result["reason"])

    def test_a_reply_recorded_mid_sequence_stops_the_next_action(self):
        """Tentative de contournement : le prospect est déjà en cours de
        séquence (invitation envoyée) puis répond — l'étape suivante ne doit
        jamais s'exécuter."""
        prospect = make_prospect()
        _with_linkedin_contact(prospect)
        cp = make_campaign_prospect(self.campaign, prospect)
        provider = MockLinkedInProvider()
        now = timezone.now()
        advance_campaign_prospect(cp.pk, now=now, linkedin_provider=provider)

        ContactLog.objects.create(prospect=prospect, channel="email", outcome="replied")
        result = advance_campaign_prospect(cp.pk, now=now + timedelta(days=10), linkedin_provider=provider)
        self.assertEqual(result["action"], "stopped")


class CampaignValidationGuardrailTests(TestCase):
    """Correctif d'audit — garde-fous restaurés : une campagne non validée
    ne doit produire AUCUNE action, sur aucun canal, même en tentant de
    contourner via des appels répétés ou un délai avancé."""

    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.sequence, self.connect, self.message, self.email_step = _multichannel_sequence(self.product)
        self.prospect = make_prospect()
        _with_linkedin_contact(self.prospect)
        self.provider = MockLinkedInProvider()

    def test_draft_campaign_produces_no_action(self):
        campaign = make_campaign(self.product, icp=self.icp, status="draft")
        campaign.sequence = self.sequence
        campaign.save(update_fields=["sequence"])
        cp = make_campaign_prospect(campaign, self.prospect)

        result = advance_campaign_prospect(cp.pk, linkedin_provider=self.provider)
        self.assertEqual(result["action"], "not_sendable")
        self.assertEqual(ContactLog.objects.filter(campaign_prospect=cp).count(), 0)

    def test_active_but_unvalidated_campaign_produces_no_action(self):
        """Tentative de contournement : status="active" mais validated_at
        jamais renseigné — is_sendable doit rester False."""
        campaign = make_campaign(self.product, icp=self.icp, status="active")
        campaign.sequence = self.sequence
        campaign.save(update_fields=["sequence"])
        self.assertIsNone(campaign.validated_at)
        cp = make_campaign_prospect(campaign, self.prospect)

        result = advance_campaign_prospect(cp.pk, linkedin_provider=self.provider)
        self.assertEqual(result["action"], "not_sendable")

    def test_repeated_calls_on_unvalidated_campaign_never_produce_an_action(self):
        campaign = make_campaign(self.product, icp=self.icp, status="draft")
        campaign.sequence = self.sequence
        campaign.save(update_fields=["sequence"])
        cp = make_campaign_prospect(campaign, self.prospect)
        now = timezone.now()

        for offset in range(5):
            result = advance_campaign_prospect(cp.pk, now=now + timedelta(days=offset * 10), linkedin_provider=self.provider)
            self.assertEqual(result["action"], "not_sendable")
        self.assertEqual(ContactLog.objects.filter(campaign_prospect=cp).count(), 0)

    def test_validated_active_campaign_does_produce_an_action(self):
        """Contre-épreuve : une fois correctement validée, l'action s'exécute."""
        campaign = _validated_campaign(self.product, self.icp)
        campaign.sequence = self.sequence
        campaign.save(update_fields=["sequence"])
        cp = make_campaign_prospect(campaign, self.prospect)

        result = advance_campaign_prospect(cp.pk, linkedin_provider=self.provider)
        self.assertEqual(result["action"], "linkedin_invitation")


class CampaignLimitGuardrailTests(TestCase):
    """Correctif d'audit — daily_send_limit/total_limit restaurés, tous
    canaux confondus."""

    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.sequence, self.connect, self.message, self.email_step = _multichannel_sequence(self.product)
        self.provider = MockLinkedInProvider()

    def _prospect_with_linkedin(self, i):
        prospect = make_prospect(name=f"Prospect Limite {i}", siret=f"6000000000000{i}")
        _with_linkedin_contact(prospect)
        return prospect

    def test_daily_send_limit_blocks_further_actions_same_day(self):
        campaign = _validated_campaign(self.product, self.icp, daily_send_limit=1, total_limit=200)
        campaign.sequence = self.sequence
        campaign.save(update_fields=["sequence"])

        prospect_a = self._prospect_with_linkedin(1)
        prospect_b = self._prospect_with_linkedin(2)
        cp_a = make_campaign_prospect(campaign, prospect_a)
        cp_b = make_campaign_prospect(campaign, prospect_b)
        now = timezone.now()

        result_a = advance_campaign_prospect(cp_a.pk, now=now, linkedin_provider=self.provider)
        self.assertEqual(result_a["action"], "linkedin_invitation")

        result_b = advance_campaign_prospect(cp_b.pk, now=now, linkedin_provider=self.provider)
        self.assertEqual(result_b["action"], "blocked_daily_limit")
        self.assertEqual(ContactLog.objects.filter(prospect=prospect_b).count(), 0)

    def test_daily_limit_resets_the_next_day(self):
        campaign = _validated_campaign(self.product, self.icp, daily_send_limit=1, total_limit=200)
        campaign.sequence = self.sequence
        campaign.save(update_fields=["sequence"])

        prospect_a = self._prospect_with_linkedin(3)
        prospect_b = self._prospect_with_linkedin(4)
        cp_a = make_campaign_prospect(campaign, prospect_a)
        cp_b = make_campaign_prospect(campaign, prospect_b)
        now = timezone.now()

        advance_campaign_prospect(cp_a.pk, now=now, linkedin_provider=self.provider)
        result_next_day = advance_campaign_prospect(cp_b.pk, now=now + timedelta(days=1), linkedin_provider=self.provider)
        self.assertEqual(result_next_day["action"], "linkedin_invitation")

    def test_total_limit_blocks_regardless_of_day(self):
        campaign = _validated_campaign(self.product, self.icp, daily_send_limit=200, total_limit=1)
        campaign.sequence = self.sequence
        campaign.save(update_fields=["sequence"])

        prospect_a = self._prospect_with_linkedin(5)
        prospect_b = self._prospect_with_linkedin(6)
        cp_a = make_campaign_prospect(campaign, prospect_a)
        cp_b = make_campaign_prospect(campaign, prospect_b)
        now = timezone.now()

        advance_campaign_prospect(cp_a.pk, now=now, linkedin_provider=self.provider)
        result_b = advance_campaign_prospect(cp_b.pk, now=now + timedelta(days=30), linkedin_provider=self.provider)
        self.assertEqual(result_b["action"], "blocked_total_limit")


class EmailDomainPolicyGuardrailTests(TestCase):
    """Correctif d'audit — politique domaine/jour (ETAPE 17) réappliquée à
    la séquence multicanal."""

    def test_same_domain_same_day_email_is_skipped(self):
        product = make_product()
        icp = make_icp(product)
        sequence = EmailSequence.objects.create(product=product, name="Email seul")
        email_step = EmailStep.objects.create(sequence=sequence, order=1, delay_days=0, channel="email", name="Premier contact")
        EmailVariant.objects.create(step=email_step, name="V1", subject_template="{{ company_name }}")

        campaign = _validated_campaign(product, icp)
        campaign.sequence = sequence
        campaign.save(update_fields=["sequence"])

        prospect_a = make_prospect(name="Domaine A", siret="70000000000001")
        make_public_email(prospect_a, email="contact@meme-domaine.example")
        prospect_b = make_prospect(name="Domaine B", siret="70000000000002")
        make_public_email(prospect_b, email="autre@meme-domaine.example")

        cp_a = make_campaign_prospect(campaign, prospect_a)
        cp_b = make_campaign_prospect(campaign, prospect_b)
        now = timezone.now()

        result_a = advance_campaign_prospect(cp_a.pk, now=now)
        self.assertEqual(result_a["action"], "email")

        result_b = advance_campaign_prospect(cp_b.pk, now=now)
        self.assertEqual(result_b["action"], "skipped_domain_already_contacted_today")
        self.assertEqual(EmailSend.objects.filter(prospect=prospect_b).count(), 0)


class MultichannelSequenceWalkTests(TestCase):
    """LINKEDIN_CONNECT -> WAIT -> LINKEDIN_MESSAGE -> WAIT -> EMAIL avec
    provider mock — exactement le flux demandé en bloc C."""

    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.campaign = _validated_campaign(self.product, self.icp)
        self.sequence, self.connect, self.message, self.email_step = _multichannel_sequence(self.product)
        self.campaign.sequence = self.sequence
        self.campaign.save(update_fields=["sequence"])
        self.prospect = make_prospect()
        _with_linkedin_contact(self.prospect)
        self.cp = make_campaign_prospect(self.campaign, self.prospect)
        self.provider = MockLinkedInProvider()
        self.now = timezone.now()

    def test_first_call_sends_linkedin_invitation(self):
        result = advance_campaign_prospect(self.cp.pk, now=self.now, linkedin_provider=self.provider)
        self.assertEqual(result["action"], "linkedin_invitation")
        self.cp.refresh_from_db()
        self.assertEqual(self.cp.current_step_id, self.connect.pk)
        self.assertEqual(self.cp.status, "contacted")

    def test_second_call_before_acceptance_waits(self):
        advance_campaign_prospect(self.cp.pk, now=self.now, linkedin_provider=self.provider)
        result = advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=5), linkedin_provider=self.provider)
        self.assertEqual(result["action"], "waiting")

    def test_after_acceptance_and_delay_sends_linkedin_message(self):
        advance_campaign_prospect(self.cp.pk, now=self.now, linkedin_provider=self.provider)
        invitation_log = ContactLog.objects.get(campaign_prospect=self.cp, email_step=self.connect)
        record_invitation_accepted(invitation_log)

        result = advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=3), linkedin_provider=self.provider)
        self.assertEqual(result["action"], "linkedin_message")
        self.cp.refresh_from_db()
        self.assertEqual(self.cp.current_step_id, self.message.pk)

    def test_after_acceptance_but_before_delay_still_waits(self):
        advance_campaign_prospect(self.cp.pk, now=self.now, linkedin_provider=self.provider)
        invitation_log = ContactLog.objects.get(campaign_prospect=self.cp, email_step=self.connect)
        record_invitation_accepted(invitation_log)

        result = advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(hours=1), linkedin_provider=self.provider)
        self.assertEqual(result["action"], "waiting")

    def test_full_walk_to_email_fallback(self):
        advance_campaign_prospect(self.cp.pk, now=self.now, linkedin_provider=self.provider)
        invitation_log = ContactLog.objects.get(campaign_prospect=self.cp, email_step=self.connect)
        record_invitation_accepted(invitation_log)
        advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=3), linkedin_provider=self.provider)

        make_public_email(self.prospect)

        result = advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=8), linkedin_provider=self.provider)
        self.assertEqual(result["action"], "email")
        self.cp.refresh_from_db()
        self.assertEqual(self.cp.current_step_id, self.email_step.pk)

    def test_declined_invitation_skips_message_and_falls_through_to_email(self):
        advance_campaign_prospect(self.cp.pk, now=self.now, linkedin_provider=self.provider)
        invitation_log = ContactLog.objects.get(campaign_prospect=self.cp, email_step=self.connect)
        record_invitation_declined(invitation_log)

        make_public_email(self.prospect)

        # L'étape "message" est sautée (refusée), mais l'étape "email" qui suit
        # respecte toujours son propre délai (4 jours), compté depuis le
        # passage (skip) — pas de rattrapage instantané, pour rester cohérent
        # avec le comportement d'un délai non sauté.
        immediate = advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=1), linkedin_provider=self.provider)
        self.assertEqual(immediate["action"], "waiting")

        result = advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=6), linkedin_provider=self.provider)
        self.assertEqual(result["action"], "email")
        self.cp.refresh_from_db()
        self.assertEqual(self.cp.current_step_id, self.email_step.pk)

    def test_sequence_complete_after_last_step(self):
        advance_campaign_prospect(self.cp.pk, now=self.now, linkedin_provider=self.provider)
        invitation_log = ContactLog.objects.get(campaign_prospect=self.cp, email_step=self.connect)
        record_invitation_accepted(invitation_log)
        advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=3), linkedin_provider=self.provider)

        make_public_email(self.prospect)
        advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=8), linkedin_provider=self.provider)

        result = advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=20), linkedin_provider=self.provider)
        self.assertEqual(result["action"], "sequence_complete")


class NoDuplicateActionTests(TestCase):
    """Le cœur de la garantie du bloc C : jamais deux actions au même
    moment pour le même prospect."""

    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.campaign = _validated_campaign(self.product, self.icp)
        self.sequence, self.connect, self.message, self.email_step = _multichannel_sequence(self.product)
        self.campaign.sequence = self.sequence
        self.campaign.save(update_fields=["sequence"])
        self.prospect = make_prospect()
        _with_linkedin_contact(self.prospect)
        self.cp = make_campaign_prospect(self.campaign, self.prospect)
        self.provider = MockLinkedInProvider()

    def test_calling_advance_repeatedly_without_time_passing_sends_invitation_only_once(self):
        now = timezone.now()
        for _ in range(5):
            advance_campaign_prospect(self.cp.pk, now=now, linkedin_provider=self.provider)
        self.assertEqual(ContactLog.objects.filter(campaign_prospect=self.cp).count(), 1)

    def test_no_step_is_ever_skipped_over(self):
        """L'étape 3 (email) ne peut jamais être exécutée avant l'étape 2
        (message), même en avançant le temps d'un coup — chaque appel ne
        fait avancer que d'un cran."""
        now = timezone.now()
        advance_campaign_prospect(self.cp.pk, now=now, linkedin_provider=self.provider)
        invitation_log = ContactLog.objects.get(campaign_prospect=self.cp, email_step=self.connect)
        record_invitation_accepted(invitation_log)

        result = advance_campaign_prospect(self.cp.pk, now=now + timedelta(days=30), linkedin_provider=self.provider)
        self.assertEqual(result["action"], "linkedin_message")
        self.assertEqual(ContactLog.objects.filter(campaign_prospect=self.cp).count(), 2)


class RunCampaignSequencesTests(TestCase):
    def test_processes_multiple_campaign_prospects_and_respects_limit(self):
        product = make_product()
        icp = make_icp(product)
        campaign = _validated_campaign(product, icp)
        sequence, connect, message, email_step = _multichannel_sequence(product)
        campaign.sequence = sequence
        campaign.save(update_fields=["sequence"])

        for i in range(3):
            prospect = make_prospect(name=f"Prospect {i}", siret=f"3000000000000{i}")
            _with_linkedin_contact(prospect)
            make_campaign_prospect(campaign, prospect)

        results = run_campaign_sequences(campaign, linkedin_provider=MockLinkedInProvider())
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r["action"] == "linkedin_invitation" for r in results))

    def test_excludes_stopped_statuses_from_batch(self):
        product = make_product()
        icp = make_icp(product)
        campaign = _validated_campaign(product, icp)
        sequence, connect, message, email_step = _multichannel_sequence(product)
        campaign.sequence = sequence
        campaign.save(update_fields=["sequence"])

        prospect = make_prospect()
        cp = make_campaign_prospect(campaign, prospect, status="do_not_contact")

        results = run_campaign_sequences(campaign)
        self.assertEqual(results, [])
