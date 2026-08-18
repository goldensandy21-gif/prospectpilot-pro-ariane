"""Mission 5 — régression du bug 'str' object has no attribute 'isoformat'.

Reproduit exactement le scénario production : un SearchCandidate dont
raw_data['creation_date'] est une chaîne ISO (format API registre), passé au
pipeline d'acquisition puis au scoring PredictNeed sur l'instance encore en
mémoire (jamais rechargée depuis la base).
"""
import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from prospects.models import Campaign, ProductProfile, Prospect, PublicEmail, SearchCandidate
from prospects.services.acquisition_pipeline import _build_prospect_defaults, _finalize_candidate
from prospects.services.predictneed_scoring import _prospect_to_row, score_prospect
from .factories import make_icp, make_prospect, make_product


def make_search_run(product, icp):
    from prospects.models import CompanySearchRun
    return CompanySearchRun.objects.create(mode="acquisition", product=product, icp=icp, status="running")


def make_candidate(search_run, **overrides):
    defaults = {
        "siren": "123456789",
        "name": "Agence Test",
        "raw_data": {
            "name": "Agence Test", "siren": "123456789", "city": "Lyon",
            "creation_date": "2015-06-12T00:00:00",
        },
        "status": "pending",
    }
    defaults.update(overrides)
    return SearchCandidate.objects.create(search_run=search_run, **defaults)


class CreationDateBugRegressionTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.search_run = make_search_run(self.product, self.icp)

    def test_build_prospect_defaults_returns_real_date_object(self):
        """Reproduit le bug : creation_date doit être un datetime.date, jamais un str."""
        candidate = make_candidate(self.search_run)
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        self.assertIn("creation_date", defaults)
        self.assertIsInstance(defaults["creation_date"], datetime.date)
        self.assertNotIsInstance(defaults["creation_date"], str)
        self.assertEqual(defaults["creation_date"], datetime.date(2015, 6, 12))

    def test_build_prospect_defaults_handles_date_only_string(self):
        candidate = make_candidate(self.search_run, raw_data={
            "name": "Agence Test", "siren": "123456789", "creation_date": "2019-01-31",
        })
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        self.assertEqual(defaults["creation_date"], datetime.date(2019, 1, 31))

    def test_build_prospect_defaults_ignores_malformed_creation_date(self):
        candidate = make_candidate(self.search_run, raw_data={
            "name": "Agence Test", "siren": "123456789", "creation_date": "n/a",
        })
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        self.assertNotIn("creation_date", defaults)

    def test_prospect_to_row_does_not_crash_on_created_prospect(self):
        """Reproduit le crash exact : update_or_create(...) puis _prospect_to_row()
        sur l'instance en mémoire (pas rechargée depuis la base)."""
        candidate = make_candidate(self.search_run)
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        prospect, _ = Prospect.objects.update_or_create(siren=candidate.siren, defaults=defaults)
        # Avant le correctif, prospect.creation_date était une str en mémoire ici
        # et cet appel levait : AttributeError: 'str' object has no attribute 'isoformat'
        row = _prospect_to_row(prospect)
        self.assertEqual(row["creation_date"], "2015-06-12")

    def test_score_prospect_does_not_crash_end_to_end(self):
        candidate = make_candidate(self.search_run)
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        prospect, _ = Prospect.objects.update_or_create(siren=candidate.siren, defaults=defaults)
        # Ne doit lever aucune exception (c'est exactement l'appel qui plantait en prod).
        result = score_prospect(prospect, icp=self.icp, product=self.product)
        self.assertIn("predictneed_acquisition_score", result)


class FinalizeCandidateRepairTests(TestCase):
    """_finalize_candidate doit être rejouable tel quel pour réparer un candidat
    déjà passé par site_found/scanned (email/site déjà trouvés), sans perdre ces
    données ni relancer un crawl."""

    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.search_run = make_search_run(self.product, self.icp)

    def test_finalize_candidate_reuses_existing_quick_scan_data(self):
        candidate = make_candidate(
            self.search_run,
            status="error",
            error="'str' object has no attribute 'isoformat'",
            site_url="https://agence-test.example",
            site_confidence=80,
            quick_scan_data={
                "found_emails": ["contact@agence-test.example"],
                "found_phones": [], "found_social_links": [], "technologies_detailed": [],
            },
        )
        defaults = _build_prospect_defaults(candidate, self.icp, self.product)
        prospect, _ = Prospect.objects.update_or_create(siren=candidate.siren, defaults=defaults)
        candidate.prospect = prospect
        candidate.save(update_fields=["prospect"])

        technologies = candidate.quick_scan_data.get("technologies_detailed", [])
        status = _finalize_candidate(
            candidate, candidate.quick_scan_data, technologies, prospect, self.icp, self.product,
        )

        candidate.refresh_from_db()
        prospect.refresh_from_db()
        self.assertIn(status, ("converted", "not_eligible"))
        self.assertNotEqual(candidate.status, "error")
        self.assertEqual(candidate.contact_email, "contact@agence-test.example")
        self.assertEqual(prospect.public_email, "contact@agence-test.example")
        self.assertTrue(
            PublicEmail.objects.filter(prospect=prospect, email="contact@agence-test.example").exists()
        )
        self.assertNotEqual(prospect.predictneed_grade, "")


class RepairCommandTests(TestCase):
    """Commande manage.py repair_creation_date_candidates : dry-run, application
    réelle, portée stricte (ne touche pas les autres erreurs), idempotence."""

    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.search_run = make_search_run(self.product, self.icp)

    def _make_broken_candidate(self, **overrides):
        defaults = {
            "status": "error",
            "error": "'str' object has no attribute 'isoformat'",
            "site_url": "https://agence-test.example",
            "site_confidence": 80,
            "quick_scan_data": {
                "found_emails": ["contact@agence-test.example"],
                "found_phones": [], "found_social_links": [], "technologies_detailed": [],
            },
        }
        defaults.update(overrides)
        candidate = make_candidate(self.search_run, **defaults)
        prospect = make_prospect(name=candidate.name, siret="")
        candidate.prospect = prospect
        candidate.save(update_fields=["prospect"])
        return candidate

    def test_dry_run_does_not_modify_anything(self):
        candidate = self._make_broken_candidate()
        out = StringIO()
        call_command("repair_creation_date_candidates", stdout=out)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, "error")
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertIn("Réparés : 1", out.getvalue())

    def test_apply_repairs_candidate_and_keeps_already_found_email(self):
        candidate = self._make_broken_candidate()
        out = StringIO()
        call_command("repair_creation_date_candidates", "--apply", stdout=out)
        candidate.refresh_from_db()
        self.assertNotEqual(candidate.status, "error")
        self.assertEqual(candidate.contact_email, "contact@agence-test.example")
        self.assertIn("APPLIQUÉ", out.getvalue())

    def test_apply_ignores_unrelated_errors(self):
        other = self._make_broken_candidate(error="Timeout réseau sur le site officiel.")
        out = StringIO()
        call_command("repair_creation_date_candidates", "--apply", stdout=out)
        other.refresh_from_db()
        self.assertEqual(other.status, "error")
        self.assertIn("Réparés : 0", out.getvalue())

    def test_running_twice_is_idempotent(self):
        self._make_broken_candidate()
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        out2 = StringIO()
        call_command("repair_creation_date_candidates", "--apply", stdout=out2)
        self.assertIn("Réparés : 0", out2.getvalue())

    def test_candidate_without_prospect_is_skipped_not_crashed(self):
        candidate = make_candidate(
            self.search_run, status="error",
            error="'str' object has no attribute 'isoformat'",
            prospect=None,
        )
        out = StringIO()
        call_command("repair_creation_date_candidates", "--apply", stdout=out)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, "error")
        self.assertIn("ignorés) : 1", out.getvalue())
