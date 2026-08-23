"""Workflow live preview (section 2, correctif éditeur avec contenu actuel)
— backfill contrôlé et vérifié de `editable_body_text` pour les
`PlannedEmailContent` préparés AVANT l'ajout de ce champ.

Ne modifie JAMAIS `html_body`, `text_body`, `content_hash`, `status`,
`approved_by` ni `approved_at` — uniquement `editable_body_text`, et
seulement pour les lignes où l'extraction est VÉRIFIÉE sûre :

Pour chaque ligne jamais modifiée manuellement (`manually_edited_at` vide),
on recalcule le rendu live actuel (services.email_automation.render_live_content,
strictement identique à celui utilisé par prepare_planned_content) et on
compare son empreinte à `content_hash` déjà figé. Seule une correspondance
EXACTE prouve que les données source (AgentBrief, produit, prospect) n'ont
pas changé depuis la préparation — condition nécessaire pour que le texte
éditorial reconstruit (predictneed_email.editable_body_text_for_step, mêmes
phrases mot pour mot que le rendu figé) soit réellement fidèle au contenu
actuel. Toute ligne dont le hash ne correspond plus (donnée source modifiée
depuis, ou déjà modifiée manuellement par un autre chemin) est signalée et
laissée de côté — jamais une extraction devinée.

Usage :
    python manage.py backfill_planned_editable_body_text                # dry-run (par défaut)
    python manage.py backfill_planned_editable_body_text --apply         # applique réellement
"""
from django.core.management.base import BaseCommand

from ...models import PlannedEmailContent
from ...services.email_automation import content_hash_for, render_live_content
from ...services.predictneed_email import editable_body_text_for_step


class Command(BaseCommand):
    help = "Backfille editable_body_text pour les PlannedEmailContent antérieurs à ce champ, sans jamais toucher au contenu figé."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Applique réellement (par défaut : dry-run, aucune écriture).")

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        candidates = (
            PlannedEmailContent.objects.filter(editable_body_text="", manually_edited_at__isnull=True)
            .select_related("campaign_prospect__prospect", "campaign_prospect__campaign", "email_step")
            .order_by("pk")
        )

        verified, skipped_manual, skipped_mismatch = 0, 0, 0

        for planned in candidates:
            if planned.manually_edited_at:
                # Garde-fou défensif (déjà exclu par le filtre ci-dessus) :
                # une ligne modifiée manuellement a sa propre source
                # éditoriale (le texte saisi par l'utilisatrice) — jamais
                # remplacée par une reconstruction automatique.
                skipped_manual += 1
                continue

            subject, html, text = render_live_content(planned.campaign_prospect, planned.email_step)
            live_hash = content_hash_for(subject, html, text)

            if live_hash != planned.content_hash:
                skipped_mismatch += 1
                self.stdout.write(self.style.WARNING(
                    f"[{planned.pk}] {planned.campaign_prospect.prospect.name} / {planned.email_step.name} : "
                    "hash live ne correspond plus au contenu figé (donnée source modifiée depuis la préparation) "
                    "— NON backfillé, à traiter manuellement si besoin."
                ))
                continue

            editable_body_text = editable_body_text_for_step(planned.campaign_prospect, planned.email_step)
            verified += 1
            self.stdout.write(
                f"[{planned.pk}] {planned.campaign_prospect.prospect.name} / {planned.email_step.name} : "
                f"vérifié (hash identique) — {len(editable_body_text.split(chr(10) + chr(10)))} paragraphe(s)."
            )
            if apply_changes:
                planned.editable_body_text = editable_body_text
                planned.save(update_fields=["editable_body_text"])

        self.stdout.write(self.style.SUCCESS(
            f"\n{verified} ligne(s) vérifiée(s){' et backfillée(s)' if apply_changes else ' (dry-run — rien écrit, relancer avec --apply)'}, "
            f"{skipped_mismatch} écartée(s) (hash différent), {skipped_manual} déjà modifiée(s) manuellement (ignorées)."
        ))
