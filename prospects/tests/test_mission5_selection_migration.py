"""Mission 5, section 3 — backfill selected_for_prospecting : ne doit jamais
perdre de Prospect ni changer leur PK, et doit se baser sur la valeur réelle
du champ `source` (jamais un backfill aveugle)."""
from importlib import import_module

from django.apps import apps as django_apps
from django.db import connection
from django.test import TestCase

from prospects.models import Prospect
from .factories import make_prospect

migration_module = import_module("prospects.migrations.0005_mission5_selection_and_counters")


class _FakeSchemaEditor:
    def __init__(self):
        self.connection = connection


class BackfillSelectedForProspectingTests(TestCase):
    def test_historical_prospects_marked_selected_technical_candidates_not(self):
        historical = make_prospect(name="Historique", source="api_recherche_entreprises", siret="")
        manual = make_prospect(name="Manuel", source="manual_import", siret="")
        technical = make_prospect(name="Technique", source="acquisition_pipeline_predictneed", siret="")

        total_before = Prospect.objects.count()
        pks_before = set(Prospect.objects.values_list("pk", flat=True))

        migration_module.backfill_selected_for_prospecting(django_apps, _FakeSchemaEditor())

        # Aucune perte, aucun nouveau Prospect créé, mêmes PK.
        self.assertEqual(Prospect.objects.count(), total_before)
        self.assertEqual(set(Prospect.objects.values_list("pk", flat=True)), pks_before)

        historical.refresh_from_db()
        manual.refresh_from_db()
        technical.refresh_from_db()

        self.assertTrue(historical.selected_for_prospecting)
        self.assertIsNotNone(historical.selected_at)
        self.assertTrue(manual.selected_for_prospecting)

        self.assertFalse(technical.selected_for_prospecting)
        self.assertIsNone(technical.selected_at)

    def test_technical_candidate_data_is_untouched_only_flag_stays_false(self):
        technical = make_prospect(
            name="Technique", source="acquisition_pipeline_predictneed", siret="",
            website="https://technique.example", public_email="contact@technique.example",
        )
        migration_module.backfill_selected_for_prospecting(django_apps, _FakeSchemaEditor())
        technical.refresh_from_db()
        # Les données d'enrichissement restent intactes, seul le flag de sélection change.
        self.assertEqual(technical.website, "https://technique.example")
        self.assertEqual(technical.public_email, "contact@technique.example")
        self.assertFalse(technical.selected_for_prospecting)
