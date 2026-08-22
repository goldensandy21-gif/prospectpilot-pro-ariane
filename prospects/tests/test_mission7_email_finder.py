"""Mission 7D — Email Finder à 3 niveaux : A (trouvé sur une page publique),
B (motif de domaine déduit, jamais présenté comme trouvé/vérifié), C
(vérification MX, jamais confondue avec "vérifié"). Réutilise
ProspectEvidence/PublicEmail/ContactPerson existants."""
from unittest.mock import Mock, patch

import dns.resolver
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from prospects.models import ContactPerson, EnrichmentSource, ProspectEvidence, PublicEmail
from prospects.services import email_intelligence
from prospects.services.enrichment import EnrichmentEngine
from prospects.tests.factories import make_prospect


class ClassifyPublicSourceEmailTests(TestCase):
    def test_a_valid_professional_email_is_public_source_confirmed(self):
        result = email_intelligence.classify_public_source_email("julie.martin@agence-exemple.example")
        self.assertEqual(result["status"], "public_source_confirmed")

    def test_a_free_domain_email_stays_deliverability_unknown(self):
        result = email_intelligence.classify_public_source_email("julie.martin@gmail.com")
        self.assertEqual(result["status"], "deliverability_unknown")

    def test_an_invalid_email_stays_invalid(self):
        result = email_intelligence.classify_public_source_email("not-an-email")
        self.assertEqual(result["status"], "invalid")


class VerifyMxTests(TestCase):
    def test_domain_with_mx_returns_true(self):
        with patch.object(email_intelligence.dns.resolver, "resolve", return_value=[Mock()]):
            self.assertTrue(email_intelligence.verify_mx("agence-exemple.example"))

    def test_domain_without_mx_returns_false(self):
        with patch.object(email_intelligence.dns.resolver, "resolve", side_effect=dns.resolver.NXDOMAIN()):
            self.assertFalse(email_intelligence.verify_mx("ne-existe-pas.example"))

    def test_network_error_returns_none_not_false(self):
        with patch.object(email_intelligence.dns.resolver, "resolve", side_effect=Exception("timeout")):
            self.assertIsNone(email_intelligence.verify_mx("agence-exemple.example"))

    def test_empty_domain_returns_none(self):
        self.assertIsNone(email_intelligence.verify_mx(""))


class UpgradeEmailVerificationWithMxTests(TestCase):
    def test_format_valid_email_upgrades_to_domain_mx_valid(self):
        prospect = make_prospect()
        PublicEmail.objects.create(prospect=prospect, email="contact@agence-exemple.example", verification_status="format_valid")
        with patch.object(email_intelligence, "verify_mx", return_value=True):
            updated = email_intelligence.upgrade_email_verification_with_mx(prospect)
        self.assertEqual(len(updated), 1)
        self.assertEqual(PublicEmail.objects.get(prospect=prospect).verification_status, "domain_mx_valid")

    def test_no_mx_leaves_status_unchanged(self):
        prospect = make_prospect()
        PublicEmail.objects.create(prospect=prospect, email="contact@agence-exemple.example", verification_status="format_valid")
        with patch.object(email_intelligence, "verify_mx", return_value=False):
            updated = email_intelligence.upgrade_email_verification_with_mx(prospect)
        self.assertEqual(updated, [])
        self.assertEqual(PublicEmail.objects.get(prospect=prospect).verification_status, "format_valid")

    def test_never_downgrades_a_stronger_status(self):
        prospect = make_prospect()
        PublicEmail.objects.create(prospect=prospect, email="contact@agence-exemple.example", verification_status="public_source_confirmed")
        with patch.object(email_intelligence, "verify_mx", return_value=True):
            updated = email_intelligence.upgrade_email_verification_with_mx(prospect)
        self.assertEqual(updated, [])
        self.assertEqual(PublicEmail.objects.get(prospect=prospect).verification_status, "public_source_confirmed")

    def test_mx_check_never_produces_a_verified_status(self):
        prospect = make_prospect()
        PublicEmail.objects.create(prospect=prospect, email="contact@agence-exemple.example", verification_status="format_valid")
        with patch.object(email_intelligence, "verify_mx", return_value=True):
            email_intelligence.upgrade_email_verification_with_mx(prospect)
        self.assertNotEqual(PublicEmail.objects.get(prospect=prospect).verification_status, "verified")


class InferDomainEmailPatternTests(TestCase):
    def test_no_pattern_with_a_single_example(self):
        prospect = make_prospect(website="https://agence-exemple.example")
        ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", email="julie.martin@agence-exemple.example", is_active=True)
        self.assertIsNone(email_intelligence.infer_domain_email_pattern(prospect))

    def test_pattern_detected_with_two_agreeing_examples(self):
        prospect = make_prospect(website="https://agence-exemple.example")
        ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", email="julie.martin@agence-exemple.example", is_active=True)
        ContactPerson.objects.create(prospect=prospect, full_name="Marc Dupuis", email="marc.dupuis@agence-exemple.example", is_active=True)
        result = email_intelligence.infer_domain_email_pattern(prospect)
        self.assertEqual(result["pattern"], "first.last")
        self.assertEqual(result["confirmed_examples"], 2)

    def test_no_pattern_when_examples_disagree(self):
        prospect = make_prospect(website="https://agence-exemple.example")
        ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", email="julie.martin@agence-exemple.example", is_active=True)
        ContactPerson.objects.create(prospect=prospect, full_name="Marc Dupuis", email="m.dupuis@agence-exemple.example", is_active=True)
        self.assertIsNone(email_intelligence.infer_domain_email_pattern(prospect))

    def test_emails_on_a_different_domain_are_ignored(self):
        prospect = make_prospect(website="https://agence-exemple.example")
        ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", email="julie.martin@gmail.com", is_active=True)
        ContactPerson.objects.create(prospect=prospect, full_name="Marc Dupuis", email="marc.dupuis@gmail.com", is_active=True)
        self.assertIsNone(email_intelligence.infer_domain_email_pattern(prospect))


class ProposeInferredEmailTests(TestCase):
    def setUp(self):
        self.prospect = make_prospect(website="https://agence-exemple.example")
        ContactPerson.objects.create(prospect=self.prospect, full_name="Julie Martin", email="julie.martin@agence-exemple.example", is_active=True)
        ContactPerson.objects.create(prospect=self.prospect, full_name="Marc Dupuis", email="marc.dupuis@agence-exemple.example", is_active=True)
        self.target = ContactPerson.objects.create(prospect=self.prospect, full_name="Nina Roy", is_active=True)

    def test_proposes_an_evidence_never_a_public_email_or_contact_email(self):
        evidence = email_intelligence.propose_inferred_email(self.prospect, self.target)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.value, "nina.roy@agence-exemple.example")
        self.assertEqual(evidence.verification_status, "pattern_inferred")
        self.target.refresh_from_db()
        self.assertEqual(self.target.email, "")
        self.assertFalse(PublicEmail.objects.filter(prospect=self.prospect, email="nina.roy@agence-exemple.example").exists())

    def test_contact_with_a_known_email_is_never_touched(self):
        contact = ContactPerson.objects.get(full_name="Julie Martin")
        result = email_intelligence.propose_inferred_email(self.prospect, contact)
        self.assertIsNone(result)

    def test_confidence_is_low_and_never_verified(self):
        evidence = email_intelligence.propose_inferred_email(self.prospect, self.target)
        self.assertLess(evidence.confidence_score, 50)
        self.assertNotIn(evidence.verification_status, ("verified", "public_source_confirmed"))

    def test_propose_for_prospect_covers_every_contact_without_an_email(self):
        second_target = ContactPerson.objects.create(prospect=self.prospect, full_name="Paul Petit", is_active=True)
        created = email_intelligence.propose_inferred_emails_for_prospect(self.prospect)
        self.assertEqual(len(created), 2)
        values = {e.value for e in created}
        self.assertEqual(values, {"nina.roy@agence-exemple.example", "paul.petit@agence-exemple.example"})


class EnrichProspectWiringTests(TestCase):
    """Le niveau B doit être branché automatiquement dans le pipeline
    d'enrichissement réel — jamais une action manuelle supplémentaire."""

    def test_enrich_prospect_proposes_inferred_emails_when_a_pattern_exists(self):
        prospect = make_prospect(website="https://agence-exemple.example")
        ContactPerson.objects.create(prospect=prospect, full_name="Julie Martin", email="julie.martin@agence-exemple.example", is_active=True)
        ContactPerson.objects.create(prospect=prospect, full_name="Marc Dupuis", email="marc.dupuis@agence-exemple.example", is_active=True)
        ContactPerson.objects.create(prospect=prospect, full_name="Nina Roy", is_active=True)

        engine = EnrichmentEngine(source_keys=["public_registry"])
        run = engine.enrich_prospect(prospect)

        self.assertEqual(run.status, "done")
        self.assertTrue(
            ProspectEvidence.objects.filter(
                prospect=prospect, field_name="email_pattern_inferred",
                value="nina.roy@agence-exemple.example",
            ).exists()
        )


class VerifyEmailsMxViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="x")
        self.client.force_login(self.user)

    def test_post_upgrades_eligible_emails(self):
        prospect = make_prospect()
        PublicEmail.objects.create(prospect=prospect, email="contact@agence-exemple.example", verification_status="format_valid")
        with patch("prospects.services.email_intelligence.verify_mx", return_value=True):
            response = self.client.post(reverse("verify_emails_mx", args=[prospect.pk]))
        self.assertRedirects(response, reverse("prospect_detail", args=[prospect.pk]))
        self.assertEqual(PublicEmail.objects.get(prospect=prospect).verification_status, "domain_mx_valid")

    def test_get_never_triggers_a_check(self):
        prospect = make_prospect()
        PublicEmail.objects.create(prospect=prospect, email="contact@agence-exemple.example", verification_status="format_valid")
        with patch("prospects.services.email_intelligence.verify_mx", return_value=True) as mocked:
            self.client.get(reverse("verify_emails_mx", args=[prospect.pk]))
        mocked.assert_not_called()
