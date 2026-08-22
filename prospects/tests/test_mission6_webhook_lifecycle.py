"""Mission 6 (correctif d'audit, round 3) — ordre transactionnel du webhook
PredictNeed et cycle de vie complet activation -> paying -> résiliation."""
from django.test import TestCase
from django.utils import timezone

from prospects.models import CampaignProspect, ConversionEvent, RevenueAttribution
from prospects.services.next_best_action import compute_next_best_action
from prospects.services.predictneed_webhook import process_predictneed_event
from prospects.tests.factories import make_campaign, make_campaign_prospect, make_icp, make_prospect, make_product


class SubscriptionActivatedExclusionOrderingTests(TestCase):
    """Le test exact demandé par l'audit : les 4 valeurs, pas seulement
    Engagement."""

    def test_all_four_values_are_correct_after_subscription_activated(self):
        product = make_product()
        icp = make_icp(product)
        campaign = make_campaign(product, icp=icp)
        prospect = make_prospect()
        cp = make_campaign_prospect(campaign, prospect)

        status_code, response = process_predictneed_event({
            "event_type": "subscription_activated", "ppt": cp.tracking_token,
            "occurred_at": timezone.now().isoformat(), "mrr": "49.00", "subscription_value": "49.00",
        })
        self.assertEqual(status_code, 200)

        prospect.refresh_from_db()
        self.assertEqual(prospect.predictneed_stage, "paying")
        self.assertTrue(prospect.predictneed_excluded)
        self.assertEqual(prospect.predictneed_acquisition_score, 0)
        self.assertEqual(prospect.predictneed_grade, "D")


class SubscriptionCancelledLifecycleTests(TestCase):
    def test_activation_then_cancellation_reaches_a_coherent_final_state(self):
        product = make_product()
        icp = make_icp(product)
        campaign = make_campaign(product, icp=icp)
        prospect = make_prospect()
        cp = make_campaign_prospect(campaign, prospect)
        now = timezone.now()

        process_predictneed_event({
            "event_type": "subscription_activated", "ppt": cp.tracking_token,
            "occurred_at": now.isoformat(), "mrr": "49.00", "subscription_value": "49.00",
        })
        prospect.refresh_from_db()
        self.assertEqual(prospect.predictneed_stage, "paying")
        self.assertTrue(prospect.predictneed_excluded)

        revenue_before = RevenueAttribution.objects.get(prospect=prospect)

        status_code, response = process_predictneed_event({
            "event_type": "subscription_cancelled", "ppt": cp.tracking_token,
            "occurred_at": (now + timezone.timedelta(days=30)).isoformat(),
        })
        self.assertEqual(status_code, 200)

        prospect.refresh_from_db()
        cp.refresh_from_db()

        # Prospect : plus "paying", nouvel état explicite "churned".
        self.assertEqual(prospect.predictneed_stage, "churned")
        # Plus hard-exclu : redevient un candidat légitime (reconquête).
        self.assertFalse(prospect.predictneed_excluded)

        # CampaignProspect : même état explicite.
        self.assertEqual(cp.status, "churned")

        # ConversionEvent "cancelled" enregistré.
        self.assertTrue(ConversionEvent.objects.filter(prospect=prospect, event_type="cancelled").exists())

        # RevenueAttribution historique JAMAIS effacé.
        self.assertTrue(RevenueAttribution.objects.filter(pk=revenue_before.pk).exists())
        revenue_after = RevenueAttribution.objects.get(pk=revenue_before.pk)
        self.assertEqual(revenue_after.mrr, revenue_before.mrr)

        # Next Best Action : reconquête (NURTURE), jamais un STOP définitif
        # ni une prospection standard.
        nba = compute_next_best_action(prospect)
        self.assertEqual(nba["code"], "NURTURE")

    def test_cancelled_prospect_is_not_hard_excluded_from_scoring(self):
        product = make_product()
        icp = make_icp(product)
        campaign = make_campaign(product, icp=icp)
        prospect = make_prospect()
        cp = make_campaign_prospect(campaign, prospect)
        now = timezone.now()

        process_predictneed_event({
            "event_type": "subscription_activated", "ppt": cp.tracking_token,
            "occurred_at": now.isoformat(),
        })
        process_predictneed_event({
            "event_type": "subscription_cancelled", "ppt": cp.tracking_token,
            "occurred_at": (now + timezone.timedelta(days=10)).isoformat(),
        })

        from prospects.services.predictneed_scoring import score_prospect
        prospect.refresh_from_db()
        result = score_prospect(prospect, icp=icp, product=product)
        self.assertFalse(result["predictneed_excluded"])

    def test_active_campaign_sequence_stops_on_churn(self):
        from prospects.services.campaign_sequencing import advance_campaign_prospect
        from prospects.services.linkedin_provider import MockLinkedInProvider
        from prospects.models import ContactPerson, EmailSequence, EmailStep

        product = make_product()
        icp = make_icp(product)
        campaign = make_campaign(product, icp=icp, status="active")
        campaign.validated_at = timezone.now()
        sequence = EmailSequence.objects.create(product=product, name="Séquence churn test")
        EmailStep.objects.create(sequence=sequence, order=1, delay_days=0, channel="linkedin_connect", name="Invitation")
        campaign.sequence = sequence
        campaign.save(update_fields=["validated_at", "sequence"])

        prospect = make_prospect()
        ContactPerson.objects.create(prospect=prospect, full_name="A", profile_url="https://linkedin.com/in/a", is_active=True)
        cp = make_campaign_prospect(campaign, prospect, status="selected")
        now = timezone.now()

        process_predictneed_event({
            "event_type": "subscription_activated", "ppt": cp.tracking_token, "occurred_at": now.isoformat(),
        })
        process_predictneed_event({
            "event_type": "subscription_cancelled", "ppt": cp.tracking_token,
            "occurred_at": (now + timezone.timedelta(days=5)).isoformat(),
        })

        result = advance_campaign_prospect(cp.pk, now=now + timezone.timedelta(days=10), linkedin_provider=MockLinkedInProvider())
        self.assertEqual(result["action"], "stopped")
