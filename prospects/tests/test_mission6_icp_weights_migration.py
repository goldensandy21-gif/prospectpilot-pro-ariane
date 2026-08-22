"""Mission 6 (correctif d'audit, rounds 2 et 3) — migration explicite et
déterministe des poids ICP existants, jamais une fusion+renormalisation
silencieuse, et jamais un écrasement d'un profil réellement personnalisé.

Trois cas distincts (round 3) : Cas A (anciens défauts exacts -> nouveaux
défauts exacts, pas un rescale générique), Cas B (profil personnalisé ->
proportions relatives conservées dans 75% pile, somme exacte via la méthode
du plus grand reste), Cas C (vide -> laissé vide)."""
from importlib import import_module

from django.apps import apps as django_apps
from django.db import connection
from django.test import TestCase

from prospects.tests.factories import make_icp, make_product

migration_0013 = import_module("prospects.migrations.0013_mission6_audit_round2_icp_weights_backfill")


class _FakeSchemaEditor:
    def __init__(self):
        self.connection = connection


class IcpWeightsBackfillCaseATests(TestCase):
    """Cas A : weights == anciens défauts exacts (30/25/20/15/10)."""

    def test_maps_exactly_to_the_new_default_weights(self):
        product = make_product()
        icp = make_icp(product, weights={
            "icp_fit": 30, "need": 25, "acquisition_maturity": 20, "contactability": 15, "timing": 10,
        })
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()

        self.assertEqual(icp.weights, {
            "icp_fit": 25, "need": 15, "acquisition_maturity": 15,
            "contactability": 15, "timing": 5, "intent": 15, "engagement": 10,
        })
        self.assertEqual(sum(icp.weights.values()), 100)


class IcpWeightsBackfillCaseBTests(TestCase):
    """Cas B : profil réellement personnalisé (différent des anciens défauts)."""

    def test_a_genuinely_personalized_profile_keeps_its_emphasis(self):
        """Un profil qui avait mis l'accent sur icp_fit (50%) doit continuer
        à le faire après migration — jamais écrasé par un profil générique."""
        product = make_product()
        icp = make_icp(product, weights={
            "icp_fit": 50, "need": 10, "acquisition_maturity": 20, "contactability": 15, "timing": 5,
        })
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        self.assertGreater(icp.weights["icp_fit"], icp.weights["need"] * 3)

    def test_writes_a_total_of_exactly_100_not_approximately(self):
        product = make_product()
        icp = make_icp(product, weights={
            "icp_fit": 50, "need": 10, "acquisition_maturity": 20, "contactability": 15, "timing": 5,
        })
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        self.assertEqual(sum(icp.weights.values()), 100)  # exact, pas +-1

    def test_various_odd_legacy_splits_always_sum_to_exactly_100(self):
        """La méthode du plus grand reste doit garantir une somme exacte
        quelle que soit la répartition, y compris des cas qui génèrent des
        restes de division difficiles (33/33/34/... etc.)."""
        product = make_product()
        odd_splits = [
            {"icp_fit": 33, "need": 33, "acquisition_maturity": 34, "contactability": 0, "timing": 0},
            {"icp_fit": 1, "need": 1, "acquisition_maturity": 1, "contactability": 1, "timing": 96},
            {"icp_fit": 20, "need": 20, "acquisition_maturity": 20, "contactability": 20, "timing": 20},
        ]
        for i, weights in enumerate(odd_splits):
            icp = make_icp(product, name=f"ICP odd {i}", weights=dict(weights))
            migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
            icp.refresh_from_db()
            self.assertEqual(sum(icp.weights.values()), 100, f"split={weights}")
            legacy_sum = sum(icp.weights[k] for k in ["icp_fit", "need", "acquisition_maturity", "contactability", "timing"])
            self.assertEqual(legacy_sum, 75, f"split={weights}")

    def test_partial_legacy_weights_fill_missing_keys_from_old_defaults_not_new_ones(self):
        """Un profil qui n'avait personnalisé QUE icp_fit doit voir les
        autres clés manquantes complétées par les anciens défauts legacy,
        pas par les nouveaux (pour ne jamais changer silencieusement le sens
        d'une valeur que l'utilisateur n'a jamais touchée)."""
        product = make_product()
        icp = make_icp(product, weights={"icp_fit": 40})
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        self.assertIn("intent", icp.weights)
        self.assertIn("engagement", icp.weights)
        self.assertEqual(sum(icp.weights.values()), 100)


class IcpWeightsBackfillCaseCTests(TestCase):
    """Cas C : weights vide -> laissé vide, DEFAULT_ICP_WEIGHTS s'applique."""

    def test_empty_weights_are_left_untouched(self):
        product = make_product()
        icp = make_icp(product, weights={})
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        self.assertEqual(icp.weights, {})

    def test_effective_weights_sums_to_100_for_an_untouched_empty_profile(self):
        product = make_product()
        icp = make_icp(product, weights={})
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        self.assertEqual(sum(icp.effective_weights().values()), 100)


class IcpWeightsBackfillIdempotencyTests(TestCase):
    def test_already_migrated_profile_is_left_untouched(self):
        product = make_product()
        icp = make_icp(product, weights={
            "icp_fit": 20, "need": 12, "acquisition_maturity": 12, "contactability": 12,
            "timing": 4, "intent": 25, "engagement": 15,
        })
        original = dict(icp.weights)
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        self.assertEqual(icp.weights, original)

    def test_running_the_migration_twice_is_a_no_op_the_second_time(self):
        product = make_product()
        icp = make_icp(product, weights={
            "icp_fit": 30, "need": 25, "acquisition_maturity": 20, "contactability": 15, "timing": 10,
        })
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        first_pass = dict(icp.weights)

        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        self.assertEqual(icp.weights, first_pass)
