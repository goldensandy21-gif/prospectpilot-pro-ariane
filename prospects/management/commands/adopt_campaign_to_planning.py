"""Section 1 (correctif automatisation) — adoption sûre d'une campagne
existante (créée avant `planning_managed`) dans le Planning e-mail.

N'envoie jamais rien. N'invente aucune bascule aveugle de booléen : refuse
l'adoption si un premier email commercial a déjà été réellement envoyé pour
un des prospects de la campagne, invalide l'ancienne validation (le
scheduler ne peut rien envoyer tant qu'une nouvelle validation humaine
n'existe pas), et complète la séquence à 4 étapes sans jamais toucher au J0
personnalisé existant. Voir services/email_automation.py::adopt_campaign_into_planning.

Usage :
    python manage.py adopt_campaign_to_planning --campaign-id 2 3 4 5 6            # dry-run (par défaut)
    python manage.py adopt_campaign_to_planning --campaign-id 2 3 4 5 6 --apply    # applique réellement
"""
from django.core.management.base import BaseCommand, CommandError

from ...models import Campaign
from ...services.email_automation import adopt_campaign_into_planning


class Command(BaseCommand):
    help = "Adopte une ou plusieurs campagnes existantes dans le Planning e-mail, sans envoi."

    def add_arguments(self, parser):
        parser.add_argument("--campaign-id", type=int, nargs="+", required=True, help="ID(s) de campagne à adopter.")
        parser.add_argument("--apply", action="store_true", help="Applique réellement (par défaut : dry-run, aucune écriture).")

    def handle(self, *args, **options):
        campaign_ids = options["campaign_id"]
        apply_changes = options["apply"]

        for campaign_id in campaign_ids:
            try:
                campaign = Campaign.objects.select_related("sequence").get(pk=campaign_id)
            except Campaign.DoesNotExist:
                raise CommandError(f"Campagne {campaign_id} introuvable.")

            if campaign.planning_managed:
                self.stdout.write(self.style.WARNING(f"[{campaign_id}] « {campaign.name} » est déjà planning_managed=True — ignorée."))
                continue

            if not apply_changes:
                members = campaign.campaign_prospects.count()
                self.stdout.write(
                    f"[DRY-RUN] {campaign_id} — « {campaign.name} » : {members} prospect(s), "
                    f"séquence={'oui' if campaign.sequence else 'ABSENTE'}, total_limit actuel={campaign.total_limit}. "
                    "Relancer avec --apply pour adopter réellement."
                )
                continue

            try:
                adopt_campaign_into_planning(campaign)
            except ValueError as exc:
                self.stdout.write(self.style.ERROR(f"[{campaign_id}] refusé : {exc}"))
                continue

            self.stdout.write(self.style.SUCCESS(
                f"[{campaign_id}] « {campaign.name} » adoptée dans le Planning e-mail "
                f"(status=draft, validated_at=None — nouvelle validation humaine requise avant tout envoi)."
            ))
