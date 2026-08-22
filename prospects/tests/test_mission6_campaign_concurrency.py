"""Mission 6 (correctif d'audit, round 2) — concurrence campagne.

Ce test ne prouve quelque chose que sur un vrai moteur avec verrouillage de
lignes transactionnel (PostgreSQL) : SQLite sérialise déjà tout au niveau du
fichier, ce qui ne démontrerait rien sur la correction du verrou explicite.
Il est donc explicitement ignoré hors PostgreSQL (voir skipUnless) — à
exécuter avec `DATABASE_URL` pointé sur un Postgres réel.
"""
import threading

from django.db import connection
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from prospects.models import CampaignProspect, ContactLog, ContactPerson, EmailSequence, EmailStep
from prospects.services.campaign_sequencing import advance_campaign_prospect
from prospects.services.linkedin_provider import MockLinkedInProvider
from prospects.tests.factories import make_campaign, make_icp, make_prospect, make_product


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentCampaignLimitTests(TransactionTestCase):
    """Deux CampaignProspect distincts de la MÊME campagne, avancés en
    parallèle par deux threads (donc deux connexions/transactions
    distinctes), avec daily_send_limit=1 : une seule action doit réussir."""

    def test_only_one_action_allowed_under_concurrent_load(self):
        if connection.vendor != "postgresql":
            self.skipTest("Nécessite PostgreSQL réel (verrouillage de lignes) — voir docstring du module.")

        product = make_product()
        icp = make_icp(product)
        campaign = make_campaign(product, icp=icp, status="active", daily_send_limit=1, total_limit=200)
        campaign.validated_at = timezone.now()
        campaign.save(update_fields=["validated_at"])

        sequence = EmailSequence.objects.create(product=product, name="Concurrence test")
        connect = EmailStep.objects.create(sequence=sequence, order=1, delay_days=0, channel="linkedin_connect", name="Invitation")
        campaign.sequence = sequence
        campaign.save(update_fields=["sequence"])

        prospect_a = make_prospect(name="Concurrent A", siret="95000000000001")
        ContactPerson.objects.create(prospect=prospect_a, full_name="A", profile_url="https://linkedin.com/in/a", is_active=True)
        prospect_b = make_prospect(name="Concurrent B", siret="95000000000002")
        ContactPerson.objects.create(prospect=prospect_b, full_name="B", profile_url="https://linkedin.com/in/b", is_active=True)

        cp_a = CampaignProspect.objects.create(campaign=campaign, prospect=prospect_a, status="selected")
        cp_b = CampaignProspect.objects.create(campaign=campaign, prospect=prospect_b, status="selected")

        results = {}
        barrier = threading.Barrier(2, timeout=10)

        def worker(key, cp_id):
            connection.close()  # force une connexion neuve et dédiée à ce thread
            try:
                barrier.wait()
                results[key] = advance_campaign_prospect(cp_id, linkedin_provider=MockLinkedInProvider())
            finally:
                connection.close()

        t1 = threading.Thread(target=worker, args=("a", cp_a.pk))
        t2 = threading.Thread(target=worker, args=("b", cp_b.pk))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        actions = {results["a"]["action"], results["b"]["action"]}
        executed = [k for k, r in results.items() if r["action"] == "linkedin_invitation"]
        blocked = [k for k, r in results.items() if r["action"] == "blocked_daily_limit"]

        self.assertEqual(len(executed), 1, f"résultats obtenus : {results}")
        self.assertEqual(len(blocked), 1, f"résultats obtenus : {results}")
        self.assertEqual(ContactLog.objects.filter(campaign_prospect__campaign=campaign).count(), 1)
