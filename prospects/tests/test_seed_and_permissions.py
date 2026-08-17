from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from prospects.models import Competitor, ICPProfile, ProductProfile, SearchPreset


class InitializeAppIdempotencyTests(TestCase):
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
