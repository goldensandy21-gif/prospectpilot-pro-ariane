"""Mission 2 — identité e-mail, conformité, désinscription, campagnes, tracking, API HMAC."""
import json
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from prospects.models import CampaignProspect, ConversionEvent, EmailSend, EngagementEvent, RevenueAttribution, Suppression
from prospects.services.campaign_sending import get_or_create_default_sequence, send_campaign_batch
from prospects.services.email_identity import get_sender_identity
from prospects.services.hmac_api import build_signature_header, verify_signature
from prospects.services.predictneed_email import render_predictneed_email, send_predictneed_campaign_email
from prospects.services.predictneed_webhook import process_predictneed_event
from prospects.services.suppression import is_suppressed
from prospects.services.tracking import build_tracking_url

from .factories import (
    make_campaign,
    make_campaign_prospect,
    make_compliance_profile,
    make_icp,
    make_product,
    make_prospect,
    make_public_email,
)


class SenderIdentityTests(TestCase):
    def test_product_identity_used_when_whitelisted(self):
        product = make_product()
        with override_settings(ALLOWED_SENDER_IDENTITIES=["contact-predict@predictneed-ia.com"]):
            identity = get_sender_identity(product=product)
        self.assertEqual(identity["from_email"], "contact-predict@predictneed-ia.com")
        self.assertEqual(identity["from_name"], "PredictNeed IA")

    def test_non_whitelisted_from_falls_back_to_legacy(self):
        product = make_product(sender_email="someone-else@example.com")
        with override_settings(ALLOWED_SENDER_IDENTITIES=["contact-predict@predictneed-ia.com"], DEFAULT_FROM_EMAIL="legacy@example.com", EMAIL_SENDER_NAME="ProspectPilot"):
            identity = get_sender_identity(product=product)
        self.assertEqual(identity["from_email"], "legacy@example.com")

    def test_no_product_uses_legacy_identity(self):
        with override_settings(DEFAULT_FROM_EMAIL="legacy@example.com", EMAIL_SENDER_NAME="ProspectPilot"):
            identity = get_sender_identity(product=None)
        self.assertEqual(identity["from_email"], "legacy@example.com")


class SuppressionCentralizationTests(TestCase):
    def test_email_suppression_blocks(self):
        Suppression.objects.create(email="blocked@example.com")
        self.assertTrue(is_suppressed("blocked@example.com"))
        self.assertFalse(is_suppressed("ok@example.com"))

    def test_domain_suppression_blocks_whole_domain(self):
        Suppression.objects.create(email="", domain="blocked-domain.com")
        self.assertTrue(is_suppressed("anyone@blocked-domain.com"))

    def test_prospect_do_not_contact_blocks(self):
        prospect = make_prospect(status="do_not_contact")
        self.assertTrue(is_suppressed("someone@example.com", prospect=prospect))

    def test_prospecting_not_allowed_blocks(self):
        prospect = make_prospect(prospecting_allowed=False)
        self.assertTrue(is_suppressed("someone@example.com", prospect=prospect))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PredictNeedRendererTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.campaign = make_campaign(self.product, self.icp)
        self.member = make_campaign_prospect(self.campaign, self.prospect)
        self.sequence = get_or_create_default_sequence(self.product, self.icp)
        self.step = self.sequence.steps.order_by("order").first()
        self.variant = self.step.variants.first()

    def test_no_unsplash_in_new_template(self):
        _, html, text = render_predictneed_email(self.member, self.step, self.variant)
        self.assertNotIn("unsplash", html.lower())
        self.assertNotIn("unsplash", text.lower())

    def test_html_and_text_both_present_and_substantive(self):
        _, html, text = render_predictneed_email(self.member, self.step, self.variant)
        self.assertGreater(len(html), 200)
        self.assertGreater(len(text), 50)
        self.assertIn("Bonjour", text)

    def test_footer_always_present_regardless_of_agent_brief(self):
        _, html, text = render_predictneed_email(self.member, self.step, self.variant)
        self.assertIn("Se désabonner", html)
        self.assertIn("Se désabonner", text)
        self.assertIn(str(self.prospect.unsubscribe_token), html)

    def test_unsubscribe_link_is_unique_per_prospect(self):
        other = make_prospect(name="Autre", siret="00000000000099")
        make_public_email(other, email="autre@example.com")
        member2 = make_campaign_prospect(self.campaign, other)
        _, html1, _ = render_predictneed_email(self.member, self.step, self.variant)
        _, html2, _ = render_predictneed_email(member2, self.step, self.variant)
        self.assertNotIn(str(other.unsubscribe_token), html1)
        self.assertNotIn(str(self.prospect.unsubscribe_token), html2)

    def test_send_uses_predictneed_from_and_reply_to(self):
        with override_settings(ALLOWED_SENDER_IDENTITIES=[]):
            record = send_predictneed_campaign_email(self.member, self.step, self.variant)
        self.assertEqual(record.status, "sent")
        self.assertEqual(record.from_email, "contact-predict@predictneed-ia.com")
        self.assertEqual(record.reply_to_email, "contact-predict@predictneed-ia.com")
        sent = mail.outbox[-1]
        self.assertIn("PredictNeed IA <contact-predict@predictneed-ia.com>", sent.from_email)
        self.assertEqual(sent.extra_headers.get("List-Unsubscribe-Post"), "List-Unsubscribe=One-Click")
        self.assertIn(str(self.prospect.unsubscribe_token), sent.extra_headers.get("List-Unsubscribe", ""))
        self.assertTrue(record.message_id)

    def test_suppressed_prospect_blocks_send_even_if_prepared_earlier(self):
        Suppression.objects.create(email=self.prospect.public_email)
        record = send_predictneed_campaign_email(self.member, self.step, self.variant)
        self.assertEqual(record.status, "suppressed")
        self.assertEqual(len(mail.outbox), 0)

    def test_is_test_does_not_alter_prospect_or_campaign_state(self):
        before_status = self.member.status
        before_stage = self.prospect.predictneed_stage
        record = send_predictneed_campaign_email(self.member, self.step, self.variant, is_test=True, test_recipient="tester@example.com")
        self.assertTrue(record.is_test)
        self.assertEqual(record.status, "sent")
        self.member.refresh_from_db()
        self.prospect.refresh_from_db()
        self.assertEqual(self.member.status, before_status)
        self.assertEqual(self.prospect.predictneed_stage, before_stage)
        self.assertEqual(mail.outbox[-1].to, ["tester@example.com"])


class CampaignSendingLimitsTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_campaign(self.product, self.icp, status="ready", daily_send_limit=1, validated_at="2026-01-01T00:00:00Z")
        get_or_create_default_sequence(self.product, self.icp)
        self.campaign.sequence = self.product.sequences.first()
        self.campaign.save()

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", ALLOWED_SENDER_IDENTITIES=[])
    def test_daily_limit_stops_batch(self):
        for i in range(3):
            prospect = make_prospect(name=f"P{i}", siret=f"1000000000000{i}")
            make_public_email(prospect, email=f"contact{i}@domain{i}.example")
            make_campaign_prospect(self.campaign, prospect)
        summary = send_campaign_batch(self.campaign)
        self.assertEqual(summary["sent"], 1)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", ALLOWED_SENDER_IDENTITIES=[])
    def test_same_domain_throttled_within_run(self):
        self.campaign.daily_send_limit = 10
        self.campaign.save()
        for i in range(2):
            prospect = make_prospect(name=f"D{i}", siret=f"2000000000000{i}")
            make_public_email(prospect, email=f"contact{i}@samedomain.example")
            make_campaign_prospect(self.campaign, prospect)
        summary = send_campaign_batch(self.campaign)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["skipped"], 1)


class TrackingTokenTests(TestCase):
    def test_tracking_url_uses_random_token_not_sequential_id(self):
        product = make_product()
        icp = make_icp(product)
        campaign = make_campaign(product, icp)
        prospect = make_prospect()
        member = make_campaign_prospect(campaign, prospect)
        url = build_tracking_url(member, cta_type="simulator")
        self.assertIn(member.tracking_token, url)
        self.assertNotIn(f"/{member.pk}/", url)
        self.assertGreaterEqual(len(member.tracking_token), 20)

    def test_click_creates_engagement_event_and_redirects(self):
        product = make_product(simulator_url="https://predictneed-ia.example/simulateur")
        icp = make_icp(product)
        campaign = make_campaign(product, icp)
        prospect = make_prospect()
        member = make_campaign_prospect(campaign, prospect)
        response = self.client.get(reverse("campaign_click", kwargs={"token": member.tracking_token}), {"cta": "simulator"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("predictneed-ia.example/simulateur", response["Location"])
        self.assertIn(f"ppt={member.tracking_token}", response["Location"])
        self.assertTrue(EngagementEvent.objects.filter(event_type="link_clicked", campaign_prospect=member).exists())


class HMACWebhookTests(TestCase):
    def setUp(self):
        self.secret = "test-secret"
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.campaign = make_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = make_campaign_prospect(self.campaign, self.prospect)

    def _post(self, payload):
        body = json.dumps(payload)
        sig = build_signature_header(self.secret, body)
        return self.client.post(
            reverse("predictneed_events_webhook"), data=body, content_type="application/json",
            HTTP_X_PREDICTNEED_SIGNATURE=sig,
        )

    @override_settings(PREDICTNEED_SHARED_SECRET="test-secret")
    def test_valid_signature_accepted(self):
        response = self._post({"event_type": "simulator_started", "ppt": self.member.tracking_token, "idempotency_key": "k1"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(EngagementEvent.objects.filter(idempotency_key="k1").exists())

    def test_invalid_signature_rejected(self):
        with override_settings(PREDICTNEED_SHARED_SECRET="real-secret"):
            body = json.dumps({"event_type": "simulator_started"})
            bad_sig = build_signature_header("wrong-secret", body)
            response = self.client.post(
                reverse("predictneed_events_webhook"), data=body, content_type="application/json",
                HTTP_X_PREDICTNEED_SIGNATURE=bad_sig,
            )
        self.assertEqual(response.status_code, 401)

    @override_settings(PREDICTNEED_SHARED_SECRET="test-secret")
    def test_replay_is_idempotent(self):
        payload = {"event_type": "simulator_started", "ppt": self.member.tracking_token, "idempotency_key": "k-replay"}
        self._post(payload)
        self._post(payload)
        self.assertEqual(EngagementEvent.objects.filter(idempotency_key="k-replay").count(), 1)

    @override_settings(PREDICTNEED_SHARED_SECRET="test-secret")
    def test_subscription_activated_creates_revenue_attribution(self):
        response = self._post({
            "event_type": "subscription_activated", "ppt": self.member.tracking_token,
            "idempotency_key": "k-sub", "mrr": 99, "subscription_value": 99, "currency": "EUR",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ConversionEvent.objects.filter(event_type="paying").exists())
        attribution = RevenueAttribution.objects.get()
        self.assertEqual(attribution.mrr, 99)
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, "paying")

    def test_unknown_event_type_rejected(self):
        with override_settings(PREDICTNEED_SHARED_SECRET="test-secret"):
            response = self._post({"event_type": "not_a_real_event"})
        self.assertEqual(response.status_code, 400)

    def test_no_bank_card_fields_accepted_or_stored(self):
        with override_settings(PREDICTNEED_SHARED_SECRET="test-secret"):
            response = self._post({
                "event_type": "subscription_activated", "ppt": self.member.tracking_token,
                "idempotency_key": "k-card", "mrr": 99, "card_number": "4242424242424242",
            })
        self.assertEqual(response.status_code, 200)
        attribution = RevenueAttribution.objects.get(conversion_event__idempotency_key="k-card")
        self.assertNotIn("card_number", str(attribution.__dict__))


class ComplianceReadinessTests(TestCase):
    def test_missing_required_fields_reported(self):
        product = make_product()
        profile = make_compliance_profile(product, ready=False, organization_name="", contact_email="", privacy_policy_url="")
        self.assertFalse(profile.compliance_ready)
        self.assertIn("organization_name", profile.missing_required_fields)
        self.assertIn("Configuration de conformité incomplète", profile.readiness_reason())

    def test_ready_when_required_fields_filled(self):
        product = make_product()
        profile = make_compliance_profile(product, ready=True)
        self.assertTrue(profile.compliance_ready)

    def test_seed_does_not_invent_legal_identity(self):
        from prospects.services.seed_data import seed_predictneed_compliance_profile, seed_predictneed_product
        product = seed_predictneed_product()
        profile = seed_predictneed_compliance_profile(product)
        self.assertEqual(profile.legal_name, "")
        self.assertEqual(profile.postal_address, "")
        self.assertEqual(profile.dpo_email, "")
