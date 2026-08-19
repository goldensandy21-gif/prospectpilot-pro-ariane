from django.test import TestCase

from prospects.models import ContactLog, ContactPerson, PublicSocialLink
from prospects.services.linkedin_orchestration import (
    linkedin_profile_url,
    record_invitation_accepted,
    record_invitation_declined,
    record_reply,
    send_invitation,
    send_message,
)
from prospects.services.linkedin_provider import ManualLinkedInProvider, MockLinkedInProvider
from prospects.tests.factories import make_prospect


class LinkedinProfileUrlTests(TestCase):
    def test_no_profile_returns_empty_string(self):
        prospect = make_prospect()
        self.assertEqual(linkedin_profile_url(prospect), "")

    def test_prefers_contact_person_over_company_social_link(self):
        prospect = make_prospect()
        PublicSocialLink.objects.create(prospect=prospect, platform="linkedin", url="https://linkedin.com/company/exemple")
        ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont",
            profile_url="https://linkedin.com/in/alex-dupont", is_active=True,
        )
        self.assertEqual(linkedin_profile_url(prospect), "https://linkedin.com/in/alex-dupont")

    def test_falls_back_to_company_social_link(self):
        prospect = make_prospect()
        PublicSocialLink.objects.create(prospect=prospect, platform="linkedin", url="https://linkedin.com/company/exemple")
        self.assertEqual(linkedin_profile_url(prospect), "https://linkedin.com/company/exemple")


class ManualProviderNeverClaimsRealActionTests(TestCase):
    def test_manual_invitation_is_only_prepared_never_sent(self):
        prospect = make_prospect()
        ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont",
            profile_url="https://linkedin.com/in/alex-dupont", is_active=True,
        )
        log = send_invitation(prospect, provider=ManualLinkedInProvider())
        self.assertEqual(log.outcome, "invitation_prepared")
        self.assertEqual(log.metadata["provider"], "manual")

    def test_manual_message_is_only_prepared_never_sent(self):
        prospect = make_prospect()
        ContactPerson.objects.create(
            prospect=prospect, full_name="Alex Dupont",
            profile_url="https://linkedin.com/in/alex-dupont", is_active=True,
        )
        log = send_message(prospect, "Bonjour, ...", provider=ManualLinkedInProvider())
        self.assertEqual(log.outcome, "message_prepared")

    def test_no_profile_url_returns_none_instead_of_crashing(self):
        prospect = make_prospect()
        log = send_invitation(prospect, provider=ManualLinkedInProvider())
        self.assertIsNone(log)


class MockProviderFullCycleTests(TestCase):
    """Mission 6, section 19 — flux mock invitation -> acceptation -> message
    -> réponse -> stop, entièrement déterministe pour les tests."""

    def setUp(self):
        self.prospect = make_prospect()
        ContactPerson.objects.create(
            prospect=self.prospect, full_name="Alex Dupont",
            profile_url="https://linkedin.com/in/alex-dupont", is_active=True,
        )
        self.provider = MockLinkedInProvider()

    def test_full_invitation_to_reply_cycle(self):
        invitation = send_invitation(self.prospect, provider=self.provider)
        self.assertEqual(invitation.outcome, "invitation_sent")

        accepted = record_invitation_accepted(invitation)
        self.assertEqual(accepted.outcome, "invitation_accepted")

        message = send_message(self.prospect, "Ravi d'échanger.", provider=self.provider)
        self.assertEqual(message.outcome, "sent")

        replied = record_reply(message, "Oui, disons jeudi.")
        self.assertEqual(replied.outcome, "replied")
        self.assertEqual(replied.response_text, "Oui, disons jeudi.")

    def test_declined_invitation_recorded_distinctly(self):
        invitation = send_invitation(self.prospect, provider=self.provider)
        declined = record_invitation_declined(invitation)
        self.assertEqual(declined.outcome, "invitation_declined")

    def test_each_orchestration_step_produces_one_contactlog_row_not_duplicated(self):
        send_invitation(self.prospect, provider=self.provider)
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect).count(), 1)
