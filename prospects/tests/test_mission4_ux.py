"""Mission 4 — pages du cockpit commercial, branding e-mail PredictNeed,
destination des CTA, et non-régression des parcours historiques."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from prospects.models import Campaign, CampaignProspect, EngagementEvent
from prospects.services.campaign_sending import get_or_create_default_sequence
from prospects.services.predictneed_email import render_predictneed_email

from .factories import (
    make_campaign,
    make_campaign_prospect,
    make_compliance_profile,
    make_icp,
    make_product,
    make_prospect,
    make_public_email,
)


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="x", is_staff=True)
        self.client.force_login(self.user)


class CockpitPagesRenderTests(LoggedInTestCase):
    """ETAPE 42 — chaque page du cockpit répond 200 avec des données réelles."""

    def setUp(self):
        super().setUp()
        # email_settings() cherche spécifiquement slug="predictneed-ia" (produit de production).
        self.product = make_product(slug="predictneed-ia")
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.prospect = make_prospect(
            predictneed_product=self.product, predictneed_icp=self.icp,
            predictneed_acquisition_score=87, predictneed_grade="A",
            icp_fit_score=27, need_score=21, acquisition_maturity_score=17,
            contactability_score=14, timing_score=8, outbound_eligible=True,
        )
        make_public_email(self.prospect)
        self.campaign = make_campaign(self.product, self.icp, status="active")
        self.campaign.sequence = get_or_create_default_sequence(self.product, self.icp)
        self.campaign.save()
        self.member = make_campaign_prospect(self.campaign, self.prospect, grade="A", acquisition_score_snapshot=87)

    def test_dashboard_200(self):
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_acquisition_intelligence_200(self):
        self.assertEqual(self.client.get(reverse("acquisition_intelligence")).status_code, 200)

    def test_acquisition_search_200(self):
        self.assertEqual(self.client.get(reverse("acquisition_search")).status_code, 200)

    def test_prospect_detail_200(self):
        self.assertEqual(self.client.get(reverse("prospect_detail", args=[self.prospect.pk])).status_code, 200)

    def test_campaign_list_200(self):
        self.assertEqual(self.client.get(reverse("campaign_list")).status_code, 200)

    def test_campaign_detail_200(self):
        self.assertEqual(self.client.get(reverse("campaign_detail", args=[self.campaign.pk])).status_code, 200)

    def test_campaign_preview_200(self):
        self.assertEqual(self.client.get(reverse("campaign_preview", args=[self.campaign.pk])).status_code, 200)

    def test_campaign_preview_with_selected_member_200(self):
        response = self.client.get(reverse("campaign_preview", args=[self.campaign.pk]) + f"?cp={self.member.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.prospect.name)

    def test_email_settings_200(self):
        self.assertEqual(self.client.get(reverse("email_settings")).status_code, 200)

    def test_conversions_revenue_200(self):
        self.assertEqual(self.client.get(reverse("conversions_revenue")).status_code, 200)

    def test_prospect_detail_shows_predictneed_score_not_legacy_priority(self):
        response = self.client.get(reverse("prospect_detail", args=[self.prospect.pk]))
        self.assertContains(response, "SCORE PREDICTNEED")
        self.assertContains(response, "87")

    def test_prospect_detail_offers_predictneed_email_cta(self):
        response = self.client.get(reverse("prospect_detail", args=[self.prospect.pk]))
        expected_url = reverse("campaign_preview", args=[self.campaign.pk]) + f"?cp={self.member.pk}"
        self.assertContains(response, expected_url)
        self.assertContains(response, "Préparer un e-mail PredictNeed</a>")


class EmptyStateTests(LoggedInTestCase):
    def test_acquisition_intelligence_empty_state_is_human(self):
        response = self.client.get(reverse("acquisition_intelligence"))
        self.assertContains(response, "Lancez une recherche Acquisition PredictNeed")

    def test_campaign_list_empty_state_is_human(self):
        response = self.client.get(reverse("campaign_list"))
        self.assertContains(response, "Créez une campagne à partir de vos prospects qualifiés")

    def test_conversions_revenue_empty_state_is_human(self):
        response = self.client.get(reverse("conversions_revenue"))
        self.assertContains(response, "apparaîtront ici")


class EmailBrandingTests(LoggedInTestCase):
    """ETAPE 43 — l'e-mail est brandé PredictNeed IA, jamais ProspectPilot,
    et ne contient jamais d'image Unsplash."""

    def setUp(self):
        super().setUp()
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.campaign = make_campaign(self.product, self.icp)
        self.member = make_campaign_prospect(self.campaign, self.prospect)
        sequence = get_or_create_default_sequence(self.product, self.icp)
        self.step = sequence.steps.order_by("order").first()
        self.variant = self.step.variants.first()

    def test_html_contains_predictneed_branding(self):
        _, html, _ = render_predictneed_email(self.member, self.step, self.variant)
        self.assertIn("PredictNeed IA", html)

    def test_html_contains_official_address(self):
        _, html, _ = render_predictneed_email(self.member, self.step, self.variant)
        self.assertIn("contact-predict@predictneed-ia.com", html)

    def test_html_never_shows_prospectpilot_as_brand(self):
        _, html, text = render_predictneed_email(self.member, self.step, self.variant)
        self.assertNotIn("ProspectPilot Pro", html)
        self.assertNotIn("ProspectPilot Pro", text)

    def test_no_unsplash_in_predictneed_email(self):
        _, html, text = render_predictneed_email(self.member, self.step, self.variant)
        self.assertNotIn("unsplash", html.lower())
        self.assertNotIn("unsplash", text.lower())

    def test_html_and_text_both_present(self):
        _, html, text = render_predictneed_email(self.member, self.step, self.variant)
        self.assertGreater(len(html), 100)
        self.assertGreater(len(text), 30)

    def test_footer_present_in_both_versions(self):
        _, html, text = render_predictneed_email(self.member, self.step, self.variant)
        self.assertIn("Se désabonner", html)
        self.assertIn("Se désabonner", text)

    def test_unsubscribe_link_present(self):
        _, html, _ = render_predictneed_email(self.member, self.step, self.variant)
        self.assertIn(f"/unsubscribe/{self.prospect.unsubscribe_token}/", html)

    def test_privacy_link_present_when_configured(self):
        _, html, _ = render_predictneed_email(self.member, self.step, self.variant)
        self.assertIn(f"/privacy/prospect/{self.prospect.unsubscribe_token}/", html)


class CTADestinationTests(LoggedInTestCase):
    """ETAPE 21/44 — le CTA passe par le tracking ProspectPilot puis redirige
    vers PredictNeed (jamais vers une interface ProspectPilot)."""

    def setUp(self):
        super().setUp()
        self.product = make_product(
            website_url="https://predictneed-ia.example",
            simulator_url="https://predictneed-ia.example/simulateur",
            signup_url="https://predictneed-ia.example/inscription",
        )
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.campaign = make_campaign(self.product, self.icp)
        self.member = make_campaign_prospect(self.campaign, self.prospect)

    def test_email_cta_url_points_to_prospectpilot_tracking_endpoint(self):
        sequence = get_or_create_default_sequence(self.product, self.icp)
        step = sequence.steps.order_by("order").first()
        variant = step.variants.first()
        _, html, _ = render_predictneed_email(self.member, step, variant)
        self.assertIn(f"/t/{self.member.tracking_token}/", html)

    def test_click_redirects_to_predictneed_with_utm_and_ppt(self):
        response = self.client.get(
            reverse("campaign_click", kwargs={"token": self.member.tracking_token}), {"cta": "simulator"}
        )
        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        self.assertTrue(location.startswith("https://predictneed-ia.example/simulateur"))
        self.assertIn("utm_source=prospectpilot", location)
        self.assertIn(f"ppt={self.member.tracking_token}", location)

    def test_click_never_redirects_to_prospectpilot_interface(self):
        response = self.client.get(
            reverse("campaign_click", kwargs={"token": self.member.tracking_token}), {"cta": "simulator"}
        )
        location = response["Location"]
        for forbidden in ["/dashboard", "/prospects/", "/acquisition/", "/admin/", "127.0.0.1", "testserver"]:
            self.assertNotIn(forbidden, location)

    def test_click_creates_engagement_event_and_updates_status(self):
        self.member.status = "contacted"
        self.member.save(update_fields=["status"])
        self.client.get(reverse("campaign_click", kwargs={"token": self.member.tracking_token}), {"cta": "simulator"})
        self.assertTrue(EngagementEvent.objects.filter(campaign_prospect=self.member, event_type="link_clicked").exists())
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, "engaged")

    def test_all_three_cta_types_resolve_to_configured_product_urls(self):
        sequence = get_or_create_default_sequence(self.product, self.icp)
        step = sequence.steps.order_by("order").first()
        for cta_type, expected_prefix in [
            ("product", "https://predictneed-ia.example"),
            ("simulator", "https://predictneed-ia.example/simulateur"),
            ("signup", "https://predictneed-ia.example/inscription"),
        ]:
            response = self.client.get(
                reverse("campaign_click", kwargs={"token": self.member.tracking_token}), {"cta": cta_type}
            )
            self.assertTrue(response["Location"].startswith(expected_prefix), f"{cta_type} -> {response['Location']}")


class NoRegressionTests(LoggedInTestCase):
    """ETAPE 39/42 — les parcours historiques ProspectPilot restent intacts."""

    def test_legacy_dashboard_and_prospect_list_still_work(self):
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("prospect_list")).status_code, 200)

    def test_legacy_manual_search_page_still_works(self):
        self.assertEqual(self.client.get(reverse("company_search")).status_code, 200)

    def test_legacy_prospect_email_preview_still_works(self):
        prospect = make_prospect()
        response = self.client.get(reverse("email_preview", args=[prospect.pk]))
        self.assertEqual(response.status_code, 200)

    def test_suppression_list_still_works(self):
        self.assertEqual(self.client.get(reverse("suppression_list")).status_code, 200)
