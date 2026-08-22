"""Mission 6 (correctif d'audit, round 2) — migration explicite et
déterministe des poids ICP existants, jamais une fusion+renormalisation
silencieuse, et jamais un écrasement d'un profil réellement personnalisé."""
from importlib import import_module

from django.apps import apps as django_apps
from django.db import connection
from django.test import TestCase

from prospects.tests.factories import make_icp, make_product

migration_0013 = import_module("prospects.migrations.0013_mission6_audit_round2_icp_weights_backfill")


class _FakeSchemaEditor:
    def __init__(self):
        self.connection = connection


class IcpWeightsBackfillTests(TestCase):
    def test_empty_weights_are_left_untouched(self):
        product = make_product()
        icp = make_icp(product, weights={})
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        self.assertEqual(icp.weights, {})

    def test_exact_legacy_default_weights_are_rescaled_to_75_percent_envelope(self):
        product = make_product()
        icp = make_icp(product, weights={
            "icp_fit": 30, "need": 25, "acquisition_maturity": 20, "contactability": 15, "timing": 10,
        })
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()

        self.assertEqual(icp.weights["intent"], 15)
        self.assertEqual(icp.weights["engagement"], 10)
        legacy_sum = sum(icp.weights[k] for k in ["icp_fit", "need", "acquisition_maturity", "contactability", "timing"])
        self.assertAlmostEqual(legacy_sum, 75, delta=1)
        # Les proportions RELATIVES entre les 5 poids historiques sont conservées :
        # icp_fit (30/100) doit rester le plus élevé, need (25/100) le deuxième, etc.
        self.assertGreater(icp.weights["icp_fit"], icp.weights["need"])
        self.assertGreater(icp.weights["need"], icp.weights["acquisition_maturity"])
        self.assertGreaterEqual(icp.weights["acquisition_maturity"], icp.weights["contactability"])
        self.assertGreater(icp.weights["contactability"], icp.weights["timing"])

    def test_a_genuinely_personalized_profile_keeps_its_emphasis(self):
        """Un profil qui avait mis l'accent sur icp_fit (50%) doit continuer
        à le faire après migration — jamais écrasé par un profil générique."""
        product = make_product()
        icp = make_icp(product, weights={
            "icp_fit": 50, "need": 10, "acquisition_maturity": 20, "contactability": 15, "timing": 5,
        })
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()

        # icp_fit doit rester très largement le premier poste, loin devant need.
        self.assertGreater(icp.weights["icp_fit"], icp.weights["need"] * 3)

    def test_effective_weights_always_sums_to_100_after_migration(self):
        product = make_product()
        icp = make_icp(product, weights={
            "icp_fit": 30, "need": 25, "acquisition_maturity": 20, "contactability": 15, "timing": 10,
        })
        migration_0013.backfill_icp_weights(django_apps, _FakeSchemaEditor())
        icp.refresh_from_db()
        total = sum(icp.effective_weights().values())
        self.assertAlmostEqual(total, 100, delta=1)

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
        legacy_sum = sum(icp.weights[k] for k in ["icp_fit", "need", "acquisition_maturity", "contactability", "timing"])
        self.assertAlmostEqual(legacy_sum, 75, delta=1)
