"""Mission 6, bloc H — parcours métier complet, bout en bout :

Trouver -> enrichir -> signaux -> Fit/Intent -> sélection -> Prospects ->
campagne -> LinkedIn (mock) -> e-mail -> engagement -> PredictNeed ->
conversion -> revenu.

Aucun envoi LinkedIn réel (MockLinkedInProvider, aucun appel réseau).
Aucun batch e-mail réel (backend e-mail de test Django, locmem — jamais de
SMTP réel). Un seul prospect, suivi de bout en bout, pour vérifier que
toute la chaîne construite dans les blocs A à G fonctionne ensemble et
reste cohérente à chaque étape.
"""
from django.core import mail
from django.test import TestCase
from django.utils import timezone

from prospects.models import (
    Alert,
    Campaign,
    CampaignProspect,
    CompanySearchRun,
    ContactLog,
    ContactPerson,
    ConversionEvent,
    EmailSequence,
    EmailStep,
    EmailVariant,
    RevenueAttribution,
    SearchCandidate,
)
from prospects.services.acquisition_scores import recompute_acquisition_scores
from prospects.services.campaign_sequencing import advance_campaign_prospect
from prospects.services.in_market_status import in_market_status
from prospects.services.linkedin_orchestration import record_invitation_accepted
from prospects.services.linkedin_provider import MockLinkedInProvider
from prospects.services.next_best_action import compute_next_best_action
from prospects.services.predictneed_webhook import process_predictneed_event
from prospects.services.signal_collectors import QuickScanSignalCollector, SocialPresenceSignalCollector, run_signal_collectors
from prospects.tests.factories import make_campaign, make_compliance_profile, make_icp, make_prospect, make_product, make_public_email


class FullBusinessJourneyEndToEndTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def test_full_journey_from_signal_to_revenue(self):
        # --- Trouver : un prospect existe (registre officiel, hors réseau ici). ---
        product = make_product(slug="predictneed-ia")
        make_compliance_profile(product)
        icp = make_icp(product)
        prospect = make_prospect(name="Parcours E2E — Agence Complète", predictneed_icp=product.icp_profiles.first())
        prospect.icp_fit_score = 82
        prospect.save(update_fields=["icp_fit_score"])
        make_public_email(prospect, email="contact@agence-complete.example")
        ContactPerson.objects.create(
            prospect=prospect, full_name="Sacha Bernard", job_title="Directrice Growth",
            profile_url="https://linkedin.com/in/sacha-bernard-demo", is_active=True, confidence_score=85,
        )

        # --- Enrichir + signaux : quick scan riche, via le vrai SignalCollector. ---
        search_run = CompanySearchRun.objects.create(mode="manual")
        SearchCandidate.objects.create(
            search_run=search_run, siren="123456789", name=prospect.name, prospect=prospect, status="scanned",
            quick_scan_data={
                "pages_checked": 6, "worth_full_analysis": True, "business_type": "agence",
                "has_contact_form": True, "has_booking": True, "has_signup": True,
                "has_landing_pages": True, "has_lead_magnet": True,
            },
        )
        saved, errors = run_signal_collectors(prospect, collectors=[QuickScanSignalCollector(), SocialPresenceSignalCollector()])
        self.assertEqual(errors, [])
        self.assertGreater(len(saved), 0)

        # --- Fit/Intent : scores recalculés à partir des signaux réellement détectés. ---
        result = recompute_acquisition_scores(prospect, now=self.now)
        self.assertGreater(result["intent_score"], 0)
        status = in_market_status(prospect)
        self.assertIn(status["code"], {"emerging", "probable", "strong"})

        # --- Sélection : le prospect entre dans le pipeline commercial. ---
        prospect.selected_for_prospecting = True
        prospect.selected_at = self.now
        prospect.save(update_fields=["selected_for_prospecting", "selected_at"])

        nba = compute_next_best_action(prospect, now=self.now)
        self.assertIn(nba["code"], {"LINKEDIN_CONNECT", "EMAIL", "WATCH"})

        # --- Campagne multicanal : LinkedIn -> attente -> LinkedIn message -> attente -> e-mail. ---
        sequence = EmailSequence.objects.create(product=product, icp=icp, name="Séquence E2E multicanal")
        connect = EmailStep.objects.create(sequence=sequence, order=1, delay_days=0, channel="linkedin_connect", name="Invitation")
        message_step = EmailStep.objects.create(
            sequence=sequence, order=2, delay_days=2, channel="linkedin_message",
            advance_condition="linkedin_accepted", name="Message",
        )
        email_step = EmailStep.objects.create(sequence=sequence, order=3, delay_days=3, channel="email", name="Relance e-mail")
        EmailVariant.objects.create(step=email_step, name="Relance", subject_template="{{ company_name }} — suite à notre échange")

        campaign = make_campaign(product, icp=icp, status="active")
        campaign.sequence = sequence
        campaign.save(update_fields=["sequence"])
        campaign_prospect = CampaignProspect.objects.create(
            campaign=campaign, prospect=prospect, status="selected",
            acquisition_score_snapshot=result["intent_score"],
        )

        provider = MockLinkedInProvider()

        # Étape 1 — invitation LinkedIn (mock, aucun envoi réel).
        step1 = advance_campaign_prospect(campaign_prospect.pk, now=self.now, linkedin_provider=provider)
        self.assertEqual(step1["action"], "linkedin_invitation")
        invitation_log = ContactLog.objects.get(campaign_prospect=campaign_prospect, email_step=connect)
        self.assertEqual(invitation_log.channel, "linkedin")

        # Acceptation (simulée manuellement — jamais déduite automatiquement).
        record_invitation_accepted(invitation_log)

        # Étape 2 — message LinkedIn (mock), une fois le délai écoulé.
        step2 = advance_campaign_prospect(campaign_prospect.pk, now=self.now + timezone.timedelta(days=3), linkedin_provider=provider)
        self.assertEqual(step2["action"], "linkedin_message")

        # Étape 3 — e-mail de relance (backend e-mail de test Django, jamais de SMTP réel).
        step3 = advance_campaign_prospect(campaign_prospect.pk, now=self.now + timezone.timedelta(days=8), linkedin_provider=provider)
        self.assertEqual(step3["action"], "email")
        self.assertEqual(len(mail.outbox), 1)

        campaign_prospect.refresh_from_db()
        self.assertEqual(campaign_prospect.status, "contacted")

        # --- Engagement PredictNeed réel (webhook), puis conversion et revenu. ---
        status_code, _ = process_predictneed_event({
            "event_type": "product_visited", "ppt": campaign_prospect.tracking_token,
            "occurred_at": (self.now + timezone.timedelta(days=9)).isoformat(),
        })
        self.assertEqual(status_code, 200)
        self.assertTrue(Alert.objects.filter(prospect=prospect, alert_type="new_engagement").exists())

        status_code, response = process_predictneed_event({
            "event_type": "signup_completed", "ppt": campaign_prospect.tracking_token,
            "occurred_at": (self.now + timezone.timedelta(days=10)).isoformat(),
        })
        self.assertEqual(status_code, 200)
        self.assertIn("conversion_event_id", response)

        status_code, response = process_predictneed_event({
            "event_type": "subscription_activated", "ppt": campaign_prospect.tracking_token,
            "occurred_at": (self.now + timezone.timedelta(days=12)).isoformat(),
            "mrr": "49.00", "subscription_value": "49.00",
        })
        self.assertEqual(status_code, 200)
        self.assertIn("revenue_attribution_id", response)

        # --- Vérifications finales : la chaîne complète est cohérente. ---
        campaign_prospect.refresh_from_db()
        prospect.refresh_from_db()
        self.assertEqual(campaign_prospect.status, "paying")
        self.assertEqual(prospect.predictneed_stage, "paying")
        self.assertTrue(ConversionEvent.objects.filter(prospect=prospect, event_type="paying").exists())
        revenue = RevenueAttribution.objects.get(prospect=prospect)
        self.assertEqual(revenue.mrr, 49)

        # La séquence doit s'être arrêtée : un nouvel appel ne doit plus rien
        # exécuter (client déjà payant, condition d'arrêt immédiate).
        final = advance_campaign_prospect(campaign_prospect.pk, now=self.now + timezone.timedelta(days=30), linkedin_provider=provider)
        self.assertEqual(final["action"], "stopped")
