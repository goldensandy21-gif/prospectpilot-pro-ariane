"""Mission 5 — réparation idempotente des SearchCandidate cassés par le bug
'str' object has no attribute 'isoformat' (creation_date str vs date).

Ne relance AUCUN crawl/quick_scan : reprend chaque candidat à partir de ses
données déjà enregistrées (site_url, quick_scan_data, Prospect déjà créé) et
rejoue uniquement la finalisation (contacts déjà persistés inchangés, score,
grade, éligibilité, stade, AgentBrief).

Portée strictement limitée aux candidats status="error" dont le message
d'erreur correspond exactement à ce bug — ne touche à aucune autre erreur.

Une fois qu'un candidat est réparé, son status quitte "error" : relancer la
commande est donc sans effet sur les candidats déjà réparés (idempotent).

Après réparation :
- les compteurs de chaque CompanySearchRun concerné (with_site_count,
  enriched_count, with_email_count, qualified_a/b/c_count,
  not_eligible_count, error_count) sont recalculés avec exactement les mêmes
  règles que run_acquisition_pipeline (voir recompute_search_run_counters) ;
- si TOUS les candidats "isoformat" d'un run ont été réparés, les entrées
  correspondantes sont retirées de CompanySearchRun.errors — les erreurs
  réseau ou sans rapport avec ce bug ne sont jamais touchées.

Usage :
    python manage.py repair_creation_date_candidates            # dry-run (par défaut)
    python manage.py repair_creation_date_candidates --apply     # applique réellement
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from ...models import Prospect, SearchCandidate
from ...services.acquisition_pipeline import _finalize_candidate, recompute_search_run_counters

ERROR_SIGNATURE = "isoformat"


class Command(BaseCommand):
    help = "Répare les SearchCandidate en status=error causés par le bug creation_date (str vs date)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Applique réellement la réparation (sans cette option : dry-run, aucune écriture).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        broken = SearchCandidate.objects.filter(
            status="error", error__icontains=ERROR_SIGNATURE,
        ).select_related("search_run")

        total = broken.count()
        self.stdout.write(f"{total} candidat(s) en status=error correspondant au bug creation_date.")

        repaired, skipped_no_prospect, failed = 0, 0, []
        affected_runs = {}

        for candidate in broken:
            affected_runs[candidate.search_run_id] = candidate.search_run

            if not candidate.prospect_id:
                skipped_no_prospect += 1
                continue

            try:
                prospect = Prospect.objects.get(pk=candidate.prospect_id)
            except Prospect.DoesNotExist:
                skipped_no_prospect += 1
                continue

            quick_data = candidate.quick_scan_data or {}
            technologies = quick_data.get("technologies_detailed", [])
            icp = candidate.search_run.icp
            product = candidate.search_run.product

            if not apply_changes:
                repaired += 1
                continue

            try:
                with transaction.atomic():
                    candidate.error = ""
                    _finalize_candidate(candidate, quick_data, technologies, prospect, icp, product)
                repaired += 1
            except Exception as exc:
                failed.append((candidate.pk, str(exc)[:300]))

        if apply_changes:
            for search_run in affected_runs.values():
                recompute_search_run_counters(search_run)
                self._clean_resolved_errors(search_run)

        mode = "APPLIQUÉ" if apply_changes else "DRY-RUN (aucune écriture)"
        self.stdout.write(self.style.SUCCESS(
            f"[{mode}] Réparés : {repaired} | Sans prospect associé (ignorés) : {skipped_no_prospect} | "
            f"Échecs : {len(failed)} | Recherches recalculées : {len(affected_runs) if apply_changes else 0}"
        ))
        for pk, err in failed:
            self.stdout.write(self.style.ERROR(f"  Candidat {pk} : {err}"))

    def _clean_resolved_errors(self, search_run):
        """Retire de CompanySearchRun.errors les entrées du bug isoformat
        uniquement si plus aucun candidat de ce run n'y correspond encore —
        ne touche jamais les erreurs réseau ou sans rapport."""
        still_broken = SearchCandidate.objects.filter(
            search_run=search_run, status="error", error__icontains=ERROR_SIGNATURE,
        ).exists()
        if still_broken:
            return

        original_errors = search_run.errors or []
        cleaned_errors = [
            entry for entry in original_errors
            if ERROR_SIGNATURE not in str(entry.get("message", ""))
        ]
        if len(cleaned_errors) != len(original_errors):
            search_run.errors = cleaned_errors
            search_run.save(update_fields=["errors"])
