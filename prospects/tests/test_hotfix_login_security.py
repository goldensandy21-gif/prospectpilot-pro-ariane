import os
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase


class LoginPageNoCredentialsExposedTests(TestCase):
    def test_login_page_does_not_expose_any_credential_hint(self):
        response = self.client.get("/accounts/login/")
        content = response.content.decode()
        self.assertNotIn("ChangeMe123", content)
        self.assertNotIn("Après installation", content)
        self.assertIn("Connexion", content)


class InitializeAppAdminPasswordSecurityTests(TestCase):
    def setUp(self):
        User.objects.filter(username="ariane").delete()

    def test_existing_admin_password_is_never_changed(self):
        user = User.objects.create(username="ariane", is_staff=True, is_superuser=True)
        user.set_password("original-password-do-not-touch")
        user.save()

        with mock.patch.dict(os.environ, {"INITIAL_ADMIN_PASSWORD": "a-completely-different-password"}):
            call_command("initialize_app", stdout=StringIO())

        user.refresh_from_db()
        self.assertTrue(user.check_password("original-password-do-not-touch"))
        self.assertFalse(user.check_password("a-completely-different-password"))

    def test_missing_admin_without_password_env_raises_and_creates_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INITIAL_ADMIN_PASSWORD", None)
            with self.assertRaises(CommandError):
                call_command("initialize_app", stdout=StringIO())

        self.assertFalse(User.objects.filter(username="ariane").exists())

    def test_missing_admin_with_password_env_creates_account(self):
        with mock.patch.dict(os.environ, {"INITIAL_ADMIN_PASSWORD": "a-strong-test-password"}):
            call_command("initialize_app", stdout=StringIO())

        user = User.objects.get(username="ariane")
        self.assertTrue(user.check_password("a-strong-test-password"))
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_no_secret_value_is_printed_to_stdout(self):
        out = StringIO()
        secret = "super-secret-password-value-xyz"
        with mock.patch.dict(os.environ, {"INITIAL_ADMIN_PASSWORD": secret}):
            call_command("initialize_app", stdout=out)

        self.assertNotIn(secret, out.getvalue())
