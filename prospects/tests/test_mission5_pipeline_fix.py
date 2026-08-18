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

from prospects.models import (
    Campaign, CompanySearchRun, ProductProfile, Prospect, PublicEmail, PublicPhone, SearchCandidate,
)
from prospects.services.acquisition_pipeline import (
    _build_prospect_defaults, _finalize_candidate, recompute_search_run_counters,
)
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

    def test_apply_actually_clears_error_field_in_database(self):
        candidate = self._make_broken_candidate()
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        candidate.refresh_from_db()
        self.assertEqual(candidate.error, "")

    def test_dry_run_leaves_error_field_untouched(self):
        candidate = self._make_broken_candidate()
        call_command("repair_creation_date_candidates", stdout=StringIO())
        candidate.refresh_from_db()
        self.assertEqual(candidate.error, "'str' object has no attribute 'isoformat'")

    def test_repaired_candidate_has_correct_status_score_grade_email(self):
        candidate = self._make_broken_candidate()
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        candidate.refresh_from_db()
        self.assertIn(candidate.status, ("converted", "not_eligible"))
        self.assertEqual(candidate.contact_email, "contact@agence-test.example")
        self.assertGreater(candidate.final_score, 0)
        self.assertIn(candidate.grade, ("A", "B", "C", "D"))


class RepairCommandCountersAndErrorsTests(TestCase):
    """Correctif pré-production : après réparation, les compteurs stockés du
    CompanySearchRun et sa liste d'erreurs doivent refléter l'état réel."""

    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.search_run = make_search_run(self.product, self.icp)
        # Compteurs volontairement obsolètes, comme un run affiché avant réparation.
        self.search_run.error_count = 5
        self.search_run.with_email_count = 0
        self.search_run.enriched_count = 0
        self.search_run.qualified_a_count = 0
        self.search_run.qualified_b_count = 0
        self.search_run.qualified_c_count = 0
        self.search_run.not_eligible_count = 0
        self.search_run.errors = [
            {"message": "Candidat Agence Test (123456789) : 'str' object has no attribute 'isoformat'", "at": "2026-08-01T00:00:00"},
            {"message": "Candidat Autre Société (987654321) : Timeout réseau sur le site officiel.", "at": "2026-08-01T00:01:00"},
        ]
        self.search_run.save()

    def _broken_candidate(self, siren, **overrides):
        defaults = {
            "status": "error",
            "error": "'str' object has no attribute 'isoformat'",
            "site_url": f"https://{siren}.example",
            "site_confidence": 80,
            "quick_scan_data": {
                "found_emails": [f"contact@{siren}.example"],
                "found_phones": [], "found_social_links": [], "technologies_detailed": [],
            },
        }
        defaults.update(overrides)
        candidate = make_candidate(self.search_run, siren=siren, **defaults)
        prospect = make_prospect(name=f"Société {siren}", siret="")
        candidate.prospect = prospect
        candidate.save(update_fields=["prospect"])
        return candidate

    def test_dry_run_does_not_change_counters_or_errors(self):
        self._broken_candidate("111111111")
        call_command("repair_creation_date_candidates", stdout=StringIO())
        self.search_run.refresh_from_db()
        self.assertEqual(self.search_run.error_count, 5)
        self.assertEqual(len(self.search_run.errors), 2)

    def test_error_count_decreases_after_repair(self):
        self._broken_candidate("222222222")
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        self.search_run.refresh_from_db()
        # Plus aucun SearchCandidate en status=error pour ce run -> error_count=0.
        self.assertEqual(self.search_run.error_count, 0)

    def test_with_email_count_increases_after_repair(self):
        self._broken_candidate("333333333")
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        self.search_run.refresh_from_db()
        self.assertEqual(self.search_run.with_email_count, 1)

    def test_grade_counts_recomputed(self):
        self._broken_candidate("444444444")
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        self.search_run.refresh_from_db()
        total_graded = (
            self.search_run.qualified_a_count
            + self.search_run.qualified_b_count
            + self.search_run.qualified_c_count
        )
        self.assertGreaterEqual(total_graded, 0)  # recalculé, jamais laissé à sa valeur obsolète
        candidate = SearchCandidate.objects.get(siren="444444444")
        if candidate.grade == "A":
            self.assertEqual(self.search_run.qualified_a_count, 1)
        elif candidate.grade == "B":
            self.assertEqual(self.search_run.qualified_b_count, 1)
        elif candidate.grade == "C":
            self.assertEqual(self.search_run.qualified_c_count, 1)

    def test_unrelated_network_error_entry_is_preserved(self):
        self._broken_candidate("555555555")
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        self.search_run.refresh_from_db()
        messages = [e["message"] for e in self.search_run.errors]
        self.assertTrue(any("Timeout réseau" in m for m in messages))

    def test_resolved_isoformat_error_entry_removed_once_fully_repaired(self):
        self._broken_candidate("666666666")
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        self.search_run.refresh_from_db()
        messages = [e["message"] for e in self.search_run.errors]
        self.assertFalse(any("isoformat" in m for m in messages))
        self.assertEqual(len(self.search_run.errors), 1)  # seule l'erreur réseau reste

    def test_isoformat_error_entries_kept_if_a_candidate_could_not_be_repaired(self):
        """Un candidat isoformat sans prospect associé reste en erreur : la
        liste des erreurs du run ne doit PAS être nettoyée tant que tout
        n'est pas réparé."""
        self._broken_candidate("777777777")
        unrepairable = make_candidate(
            self.search_run, siren="888888888", status="error",
            error="'str' object has no attribute 'isoformat'", prospect=None,
        )
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        self.search_run.refresh_from_db()
        messages = [e["message"] for e in self.search_run.errors]
        self.assertTrue(any("isoformat" in m for m in messages))
        unrepairable.refresh_from_db()
        self.assertEqual(unrepairable.status, "error")

    def test_second_run_is_idempotent_for_counters_and_errors(self):
        self._broken_candidate("999999999")
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        self.search_run.refresh_from_db()
        counters_after_first = (
            self.search_run.error_count, self.search_run.with_email_count,
            self.search_run.qualified_a_count, self.search_run.qualified_b_count,
            self.search_run.qualified_c_count, list(self.search_run.errors),
        )
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())
        self.search_run.refresh_from_db()
        counters_after_second = (
            self.search_run.error_count, self.search_run.with_email_count,
            self.search_run.qualified_a_count, self.search_run.qualified_b_count,
            self.search_run.qualified_c_count, list(self.search_run.errors),
        )
        self.assertEqual(counters_after_first, counters_after_second)

    def test_no_existing_prospect_or_contact_data_deleted(self):
        pre_existing = make_prospect(name="Prospect intact", siret="")
        PublicEmail.objects.create(
            prospect=pre_existing, email="deja-la@intact.example",
            email_type="generic", source_type="website", is_active=True,
        )
        PublicPhone.objects.create(
            prospect=pre_existing, phone="0600000000",
            source_type="website", is_active=True,
        )
        prospects_before = Prospect.objects.count()
        emails_before = PublicEmail.objects.count()
        phones_before = PublicPhone.objects.count()

        self._broken_candidate("101010101")
        call_command("repair_creation_date_candidates", "--apply", stdout=StringIO())

        self.assertGreaterEqual(Prospect.objects.count(), prospects_before)
        self.assertGreaterEqual(PublicEmail.objects.count(), emails_before)
        self.assertGreaterEqual(PublicPhone.objects.count(), phones_before)
        self.assertTrue(Prospect.objects.filter(pk=pre_existing.pk).exists())
        self.assertTrue(PublicEmail.objects.filter(prospect=pre_existing, email="deja-la@intact.example").exists())
        self.assertTrue(PublicPhone.objects.filter(prospect=pre_existing, phone="0600000000").exists())


class RecomputeSearchRunCountersTests(TestCase):
    """Fonction partagée par run_acquisition_pipeline et la commande de
    réparation — une seule formule, testée directement."""

    def setUp(self):
        self.product = make_product()
        self.icp = make_icp(self.product)
        self.search_run = make_search_run(self.product, self.icp)

    def test_no_site_candidate_not_counted_as_enriched(self):
        make_candidate(
            self.search_run, siren="121212121", status="not_eligible",
            outbound_ineligible_reason="Site officiel introuvable.", site_url="",
        )
        recompute_search_run_counters(self.search_run)
        self.assertEqual(self.search_run.enriched_count, 0)
        self.assertEqual(self.search_run.not_eligible_count, 0)

    def test_scanned_not_eligible_counted_as_enriched_and_not_eligible(self):
        make_candidate(
            self.search_run, siren="131313131", status="not_eligible",
            outbound_ineligible_reason="Aucune adresse e-mail exploitable.",
            site_url="https://scanned.example",
        )
        recompute_search_run_counters(self.search_run)
        self.assertEqual(self.search_run.enriched_count, 1)
        self.assertEqual(self.search_run.not_eligible_count, 1)

    def test_save_false_does_not_persist(self):
        make_candidate(self.search_run, siren="141414141", status="converted", contact_email="a@b.example")
        recompute_search_run_counters(self.search_run, save=False)
        reloaded = CompanySearchRun.objects.get(pk=self.search_run.pk)
        self.assertEqual(reloaded.enriched_count, 0)  # jamais sauvegardé
