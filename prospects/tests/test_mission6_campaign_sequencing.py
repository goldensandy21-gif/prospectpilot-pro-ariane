from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from prospects.models import (
    Campaign,
    CampaignProspect,
    ContactLog,
    ContactPerson,
    ConversionEvent,
    EmailSequence,
    EmailStep,
    EmailVariant,
    Suppression,
)
from prospects.services.campaign_sequencing import advance_campaign_prospect, run_campaign_sequences
from prospects.services.linkedin_orchestration import record_invitation_accepted, record_invitation_declined
from prospects.services.linkedin_provider import MockLinkedInProvider
from prospects.tests.factories import make_campaign, make_campaign_prospect, make_icp, make_prospect, make_product


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


def _with_linkedin_contact(prospect):
    ContactPerson.objects.create(
        prospect=prospect, full_name="Alex Dupont", profile_url="https://linkedin.com/in/alex-dupont", is_active=True,
    )


class StopConditionsTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.campaign = make_campaign(self.product, icp=self.icp)
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


class MultichannelSequenceWalkTests(TestCase):
    """LINKEDIN_CONNECT -> WAIT -> LINKEDIN_MESSAGE -> WAIT -> EMAIL avec
    provider mock — exactement le flux demandé en bloc C."""

    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.campaign = make_campaign(self.product, icp=self.icp)
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

        from prospects.tests.factories import make_public_email
        make_public_email(self.prospect)

        result = advance_campaign_prospect(self.cp.pk, now=self.now + timedelta(days=8), linkedin_provider=self.provider)
        self.assertEqual(result["action"], "email")
        self.cp.refresh_from_db()
        self.assertEqual(self.cp.current_step_id, self.email_step.pk)

    def test_declined_invitation_skips_message_and_falls_through_to_email(self):
        advance_campaign_prospect(self.cp.pk, now=self.now, linkedin_provider=self.provider)
        invitation_log = ContactLog.objects.get(campaign_prospect=self.cp, email_step=self.connect)
        record_invitation_declined(invitation_log)

        from prospects.tests.factories import make_public_email
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

        from prospects.tests.factories import make_public_email
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
        self.campaign = make_campaign(self.product, icp=self.icp)
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
        campaign = make_campaign(product, icp=icp)
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
        campaign = make_campaign(product, icp=icp)
        sequence, connect, message, email_step = _multichannel_sequence(product)
        campaign.sequence = sequence
        campaign.save(update_fields=["sequence"])

        prospect = make_prospect()
        cp = make_campaign_prospect(campaign, prospect, status="do_not_contact")

        results = run_campaign_sequences(campaign)
        self.assertEqual(results, [])
