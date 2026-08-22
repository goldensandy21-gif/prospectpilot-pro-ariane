import os
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from prospects.models import Competitor, EmailComplianceProfile, ICPProfile, ProductProfile, SearchPreset


class InitializeAppIdempotencyTests(TestCase):
    def setUp(self):
        env_patcher = mock.patch.dict(os.environ, {"INITIAL_ADMIN_PASSWORD": "test-only-password-not-real"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def test_running_twice_does_not_duplicate_seed_data(self):
        call_command("initialize_app")
        call_command("initialize_app")
        self.assertEqual(ProductProfile.objects.filter(slug="predictneed-ia").count(), 1)
        self.assertEqual(ICPProfile.objects.filter(product__slug="predictneed-ia").count(), 4)
        self.assertEqual(Competitor.objects.count(), 5)
        self.assertEqual(User.objects.filter(username="ariane").count(), 1)

    def test_search_presets_reference_valid_icps(self):
        call_command("initialize_app")
        self.assertTrue(SearchPreset.objects.exists())
        for preset in SearchPreset.objects.all():
            self.assertEqual(preset.icp.product, preset.product)

    def test_redeploy_never_overwrites_admin_edited_values(self):
        """Mission 4.1 — initialize_app tourne à chaque démarrage Fly (fly.toml).
        Un redéploiement ne doit JAMAIS écraser une configuration déjà modifiée
        depuis l'admin (URL produit, prix, poids ICP, angle concurrent,
        information juridique)."""
        call_command("initialize_app")

        product = ProductProfile.objects.get(slug="predictneed-ia")
        product.website_url = "https://admin-edited-url.example"
        product.monthly_price = 149
        product.save()

        icp = ICPProfile.objects.filter(product=product).first()
        icp.weights = {"icp_fit": 50, "need": 20, "acquisition_maturity": 15, "contactability": 10, "timing": 5}
        icp.save()

        competitor = Competitor.objects.first()
        competitor.suggested_angle = "Angle personnalisé saisi par l'utilisateur."
        competitor.save()

        compliance = EmailComplianceProfile.objects.get(product=product)
        compliance.legal_name = "Raison sociale réelle saisie par l'utilisateur"
        compliance.save()

        preset = SearchPreset.objects.first()
        preset.volume_max_candidates = 42
        preset.save()

        # Simule un redéploiement Fly : initialize_app est réexécuté au démarrage.
        call_command("initialize_app")

        product.refresh_from_db()
        icp.refresh_from_db()
        competitor.refresh_from_db()
        compliance.refresh_from_db()
        preset.refresh_from_db()

        self.assertEqual(product.website_url, "https://admin-edited-url.example")
        self.assertEqual(product.monthly_price, 149)
        self.assertEqual(icp.weights["icp_fit"], 50)
        self.assertEqual(competitor.suggested_angle, "Angle personnalisé saisi par l'utilisateur.")
        self.assertEqual(compliance.legal_name, "Raison sociale réelle saisie par l'utilisateur")
        self.assertEqual(preset.volume_max_candidates, 42)


class AcquisitionPagesRequireLoginTests(TestCase):
    def test_acquisition_pages_redirect_anonymous_to_login(self):
        protected_urls = [
            reverse("acquisition_intelligence"),
            reverse("acquisition_search"),
            reverse("campaign_list"),
            reverse("campaign_create"),
            reverse("email_settings"),
        ]
        for url in protected_urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/accounts/login", response["Location"])
