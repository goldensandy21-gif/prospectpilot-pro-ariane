"""Mission 6, section 21 — protection des données existantes : les prospects
et enrichissements historiques sont intouchables. Aucune ligne ne doit être
recréée, dupliquée ou supprimée automatiquement par les ajouts de la
mission 6."""
from importlib import import_module

from django.apps import apps as django_apps
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from prospects.models import (
    ConversionEvent,
    ContactPerson,
    EngagementEvent,
    ProspectSignal,
    PublicEmail,
    PublicPhone,
    RevenueAttribution,
)
from prospects.services.signals import persist_signals, signal_fingerprint
from .factories import make_prospect

migration_0007 = import_module("prospects.migrations.0007_mission6_signal_and_scores")


class _FakeSchemaEditor:
    def __init__(self):
        self.connection = connection


class Migration0007BackfillDataProtectionTests(TestCase):
    def test_backfill_preserves_all_signal_rows_and_pks(self):
        prospect = make_prospect()
        signals = [
            ProspectSignal.objects.create(
                prospect=prospect, signal_type=f"legacy_{i}", category="analytics",
                label=f"Signal historique {i}", evidence=f"preuve {i}", confidence=70,
                score_impact=5, positive=True,
            )
            for i in range(5)
        ]
        pks_before = {s.pk for s in signals}
        total_before = ProspectSignal.objects.count()

        migration_0007.backfill_signal_fields(django_apps, _FakeSchemaEditor())

        self.assertEqual(ProspectSignal.objects.count(), total_before)
        self.assertEqual(set(ProspectSignal.objects.values_list("pk", flat=True)), pks_before)

    def test_backfill_never_touches_the_prospect_row(self):
        prospect = make_prospect(website="https://historique.example", public_email="contact@historique.example")
        ProspectSignal.objects.create(
            prospect=prospect, signal_type="legacy", category="crm",
            label="Signal historique", evidence="preuve", confidence=70,
            score_impact=5, positive=True,
        )
        migration_0007.backfill_signal_fields(django_apps, _FakeSchemaEditor())
        prospect.refresh_from_db()
        self.assertEqual(prospect.website, "https://historique.example")
        self.assertEqual(prospect.public_email, "contact@historique.example")

    def test_backfill_populates_required_fields_without_dropping_evidence(self):
        signal = ProspectSignal.objects.create(
            prospect=make_prospect(), signal_type="legacy", category="growth",
            label="Signal historique", value="valeur", evidence="preuve détaillée",
            confidence=70, score_impact=5, positive=True,
        )
        migration_0007.backfill_signal_fields(django_apps, _FakeSchemaEditor())
        signal.refresh_from_db()
        self.assertEqual(signal.evidence, "preuve détaillée")
        self.assertEqual(signal.signal_group, "intent")
        self.assertEqual(signal.source_kind, "website")
        self.assertIsNotNone(signal.observed_at)
        self.assertEqual(signal.fingerprint, signal_fingerprint("legacy", "valeur", "preuve détaillée"))


class ExistingDataUntouchedByMission6ServicesTests(TestCase):
    """Les services Mission 6 (persist_signals, scoring...) n'agissent que sur
    ProspectSignal/Prospect.intent_score/engagement_score — jamais sur les
    autres tables d'enrichissement historiques."""

    def test_persist_signals_does_not_touch_public_email_or_phone_or_contacts(self):
        prospect = make_prospect()
        email = PublicEmail.objects.create(prospect=prospect, email="marie@exemple.example", is_active=True)
        phone = PublicPhone.objects.create(prospect=prospect, phone="0102030405")
        contact = ContactPerson.objects.create(prospect=prospect, full_name="Marie Dupont", is_active=True)

        from prospects.services.signals import _signal
        persist_signals(prospect, [_signal(prospect, "hiring_growth", "growth", "Recrutement", evidence="preuve")])

        email.refresh_from_db()
        phone.refresh_from_db()
        contact.refresh_from_db()
        self.assertEqual(email.email, "marie@exemple.example")
        self.assertEqual(phone.phone, "0102030405")
        self.assertEqual(contact.full_name, "Marie Dupont")

    def test_score_recompute_never_deletes_engagement_or_conversion_events(self):
        from prospects.services.acquisition_scores import recompute_acquisition_scores

        prospect = make_prospect()
        event = EngagementEvent.objects.create(prospect=prospect, event_type="product_visited", occurred_at=timezone.now())
        conversion = ConversionEvent.objects.create(prospect=prospect, event_type="signup", occurred_at=timezone.now())
        revenue = RevenueAttribution.objects.create(conversion_event=conversion, prospect=prospect, mrr=49)

        recompute_acquisition_scores(prospect)

        self.assertTrue(EngagementEvent.objects.filter(pk=event.pk).exists())
        self.assertTrue(ConversionEvent.objects.filter(pk=conversion.pk).exists())
        self.assertTrue(RevenueAttribution.objects.filter(pk=revenue.pk).exists())
