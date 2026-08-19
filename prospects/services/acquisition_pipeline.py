"""ETAPE 4/6/7/8/26 — Pipeline d'acquisition PredictNeed IA.

registre -> dedup -> pre-score -> top candidats -> découverte site -> quick scan
-> contact -> score final -> AgentBrief.

Le pipeline traite les candidats un par un et sauvegarde sa progression sur
CompanySearchRun après chaque étape : une erreur sur un candidat n'interrompt pas
le run, et un run interrompu peut reprendre (les candidats déjà traités sont
identifiés par leur `status`, la requête registre par `cursor`).
"""
import logging
import time
from datetime import date

from django.conf import settings
from django.utils import timezone

from ..models import Prospect, PublicEmail, PublicPhone, PublicSocialLink, SearchCandidate
from .agent_brief import generate_agent_brief
from .company_search import fetch_all_companies
from .prescoring import preselect_top_candidates, registry_pre_score
from .predictneed_scoring import score_prospect
from .signal_collectors import SocialPresenceSignalCollector, run_signal_collectors
from .signals import (
    build_competitor_detections,
    build_signals_from_quick_scan,
    build_signals_from_technologies,
    persist_competitor_detections,
    persist_signals,
    persist_technologies,
)
from .site_discovery import discover_official_site
from .quick_scan import quick_scan_site

logger = logging.getLogger(__name__)

GENERIC_EMAIL_PREFIXES = (
    "contact", "info", "bonjour", "commercial", "direction", "accueil", "hello",
    "support", "serviceclient", "service-client", "rh", "recrutement", "secretariat",
)

# Marqueur exact utilisé pour distinguer, parmi les status="not_eligible", ceux
# qui n'ont jamais eu de site (donc jamais réellement analysés) de ceux qui ont
# été scannés puis exclus par le scoring (mission 5, section 10).
NO_SITE_REASON = "Site officiel introuvable."


def classify_email_type(email):
    local = email.split("@", 1)[0].lower().strip()
    if local in GENERIC_EMAIL_PREFIXES:
        return "generic"
    if "." in local or "-" in local or "_" in local:
        return "personal"
    return "unknown"


def _site_cache_cutoff():
    hours = getattr(settings, "ACQUISITION_SITE_CACHE_HOURS", 72)
    return timezone.now() - timezone.timedelta(hours=hours)


def _reusable_recent_site(website_url):
    """ETAPE 26 : ne pas rescanner un site récemment analysé pour un autre prospect."""
    if not website_url:
        return None
    return Prospect.objects.filter(
        website=website_url, updated_at__gte=_site_cache_cutoff(),
    ).exclude(website="").order_by("-updated_at").first()


def _fetch_registry_candidates(search_run):
    criteria = dict(search_run.criteria or {})
    start_page = int((search_run.cursor or {}).get("page", 1))

    def on_page(page_number, rows):
        search_run.cursor = {"page": page_number}
        search_run.registry_count = SearchCandidate.objects.filter(search_run=search_run).count() + len(rows)
        search_run.save(update_fields=["cursor", "registry_count"])

    try:
        rows = fetch_all_companies(
            max_results=search_run.volume_max_candidates,
            start_page=start_page,
            on_page=on_page,
            **criteria,
        )
    except Exception as exc:
        search_run.append_error(f"Échec récupération registre : {exc}")
        rows = []
    return rows


def _create_candidates(search_run, rows):
    created = []
    for row in rows:
        if not row.get("siren"):
            continue
        candidate, _ = SearchCandidate.objects.get_or_create(
            search_run=search_run, siren=row["siren"],
            defaults={
                "siret": row.get("siret") or "", "name": row.get("name", ""), "raw_data": row,
            },
        )
        created.append(candidate)
    return created


def _prescore_candidates(search_run, candidates):
    icp = search_run.icp
    for candidate in candidates:
        if candidate.status != "pending":
            continue
        result = registry_pre_score(candidate.raw_data, icp) if icp else {
            "score": 50, "reasons": ["Aucun ICP : pré-score neutre."], "excluded": False, "exclusion_reason": "",
        }
        candidate.pre_score = result["score"]
        candidate.pre_score_reasons = result["reasons"]
        candidate.status = "rejected_prescore" if result["excluded"] else "preselected"
        if result["excluded"]:
            candidate.outbound_ineligible_reason = result["exclusion_reason"]
        candidate.save(update_fields=["pre_score", "pre_score_reasons", "status", "outbound_ineligible_reason"])


def _build_prospect_defaults(candidate, icp, product):
    row = candidate.raw_data
    defaults = {
        "name": row.get("name", candidate.name),
        "legal_name": row.get("legal_name", ""),
        "sector": row.get("sector", ""),
        "naf_code": row.get("naf_code", ""),
        "department": row.get("department", ""),
        "city": row.get("city", ""),
        "country": row.get("country", "France"),
        "postal_code": row.get("postal_code", ""),
        "address": row.get("address", ""),
        "siren": row.get("siren", ""),
        "employee_band": row.get("employee_band", ""),
        "diffusion_partial": row.get("diffusion_partial", False),
        "prospecting_allowed": row.get("prospecting_allowed", True),
        "source": "acquisition_pipeline_predictneed",
        "source_url": row.get("source_url", ""),
        "source_payload": row.get("source_payload", {}),
        "predictneed_product": product,
        "predictneed_icp": icp,
        "predictneed_stage": "identified",
    }
    creation = row.get("creation_date")
    if creation:
        try:
            defaults["creation_date"] = date.fromisoformat(str(creation)[:10])
        except (TypeError, ValueError):
            pass
    return defaults


def _process_candidate(search_run, candidate, icp, product):
    row = candidate.raw_data
    domain_delay = getattr(settings, "ACQUISITION_DOMAIN_DELAY_SECONDS", 1.0)

    # --- découverte du site (avec cache récent) ------------------------------
    cached = None
    site_result = None
    if candidate.status in ("preselected",):
        discovered = discover_official_site(
            row.get("name", candidate.name), row.get("city", ""),
            siren=candidate.siren, siret=candidate.siret,
            max_candidates=getattr(settings, "SEARCH_SITE_CANDIDATES", 6),
        )
        if discovered.get("url"):
            candidate.site_url = discovered["url"]
            candidate.site_confidence = discovered["confidence"]
            candidate.status = "site_found"
            cached = _reusable_recent_site(discovered["url"])
        else:
            candidate.status = "no_site"
        candidate.save(update_fields=["site_url", "site_confidence", "status"])

    if candidate.status == "no_site":
        candidate.status = "not_eligible"
        candidate.outbound_ineligible_reason = NO_SITE_REASON
        candidate.save(update_fields=["status", "outbound_ineligible_reason"])
        return "no_site"

    # --- quick scan (réutilise un scan récent si un autre prospect a le même site) ---
    if candidate.status == "site_found":
        if cached and cached.technologies.exists():
            quick_data = {"reused_from_prospect_id": cached.pk, "worth_full_analysis": True, "pages_checked": 0}
            technologies = [
                {"technology": t.technology, "category": t.category, "source_url": t.source_url,
                 "evidence": t.evidence, "confidence": t.confidence}
                for t in cached.technologies.filter(is_active=True)
            ]
        else:
            time.sleep(domain_delay)
            quick_data = quick_scan_site(candidate.site_url)
            technologies = quick_data.get("technologies_detailed", [])
        candidate.quick_scan_data = quick_data
        candidate.status = "scanned"
        candidate.save(update_fields=["quick_scan_data", "status"])
    else:
        quick_data = candidate.quick_scan_data or {}
        technologies = quick_data.get("technologies_detailed", [])

    # --- création/mise à jour du Prospect --------------------------------------
    defaults = _build_prospect_defaults(candidate, icp, product)
    lookup = {"siret": row.get("siret")} if row.get("siret") else {"siren": row.get("siren")}
    prospect, _ = Prospect.objects.update_or_create(**lookup, defaults=defaults)
    candidate.prospect = prospect
    candidate.save(update_fields=["prospect"])

    return _finalize_candidate(candidate, quick_data, technologies, prospect, icp, product)


def _finalize_candidate(candidate, quick_data, technologies, prospect, icp, product):
    """Contacts détectés + technologies/signaux + score final + AgentBrief.

    Extrait de _process_candidate pour être rejouable tel quel par un script de
    réparation (candidat déjà passé par site_found/scanned, prospect déjà créé) :
    ne reconvoque jamais le crawl/quick_scan, seulement le reste du pipeline.
    `technologies` = quick_data.get("technologies_detailed", []) dans le cas
    général, sauf réutilisation d'un scan récent (voir _process_candidate).
    """
    # --- contacts détectés lors du quick scan ------------------------------------
    found_emails = quick_data.get("found_emails", [])
    email_sources = quick_data.get("email_sources", {})
    generic = [e for e in found_emails if classify_email_type(e) == "generic"]
    best_email = (generic or found_emails or [""])[0]
    for email in dict.fromkeys(found_emails):
        PublicEmail.objects.update_or_create(
            prospect=prospect, email=email,
            defaults={
                "email_type": classify_email_type(email),
                "source_url": email_sources.get(email, candidate.site_url),
                "source_type": "website", "is_primary": email == best_email,
                "is_active": True, "discovery_method": "quick_scan",
            },
        )
    if best_email:
        PublicEmail.objects.filter(prospect=prospect).exclude(email=best_email).update(is_primary=False)
        prospect.public_email = best_email
        candidate.contact_email = best_email

    for phone in dict.fromkeys(quick_data.get("found_phones", [])):
        PublicPhone.objects.update_or_create(
            prospect=prospect, phone=phone,
            defaults={"source_url": candidate.site_url, "source_type": "website", "is_active": True, "discovery_method": "quick_scan"},
        )
    for link in quick_data.get("found_social_links", []):
        if link.get("url"):
            PublicSocialLink.objects.update_or_create(
                prospect=prospect, url=link["url"],
                defaults={"platform": link.get("platform", "other"), "source_url": link.get("source_url", candidate.site_url), "is_active": True, "discovery_method": "quick_scan"},
            )

    prospect.website = candidate.site_url
    prospect.website_confidence = candidate.site_confidence
    prospect.predictneed_stage = "enriched"
    prospect.save()

    # --- technologies / signaux / concurrents -------------------------------------
    persist_technologies(prospect, technologies)
    signal_objects = (
        build_signals_from_technologies(prospect, technologies, site_url=candidate.site_url)
        + build_signals_from_quick_scan(prospect, quick_data, site_url=candidate.site_url)
    )
    persist_signals(prospect, signal_objects)
    # Mission 6, section 8 : les liens sociaux (PublicSocialLink) viennent d'être
    # écrits ci-dessus (247-252) mais ne produisaient jusqu'ici aucun signal —
    # SocialPresenceSignalCollector comble ce manque sans dupliquer la détection
    # technologies/quick scan déjà faite juste au-dessus.
    run_signal_collectors(prospect, collectors=[SocialPresenceSignalCollector()])
    persist_competitor_detections(prospect, build_competitor_detections(prospect, technologies, site_url=candidate.site_url))

    # --- score final + AgentBrief -------------------------------------------------
    score_result = score_prospect(prospect, icp=icp, product=product)
    generate_agent_brief(prospect, icp=icp, product=product)

    candidate.outbound_eligible = score_result["outbound_eligible"]
    candidate.outbound_ineligible_reason = score_result["outbound_ineligible_reason"]
    candidate.final_score = score_result["predictneed_acquisition_score"]
    candidate.grade = score_result["predictneed_grade"]
    candidate.status = "not_eligible" if score_result["predictneed_excluded"] else "converted"
    candidate.save(update_fields=["contact_email", "outbound_eligible", "outbound_ineligible_reason", "final_score", "grade", "status", "error"])

    if score_result["predictneed_excluded"]:
        prospect.predictneed_stage = "do_not_contact"
    else:
        threshold = icp.minimum_outbound_score if icp else 50
        if score_result["predictneed_acquisition_score"] >= threshold:
            prospect.predictneed_stage = "ready_to_contact" if score_result["outbound_eligible"] else "qualified"
        else:
            prospect.predictneed_stage = "enriched"
    prospect.save(update_fields=["predictneed_stage", "updated_at"])

    return candidate.status


def recompute_search_run_counters(search_run, save=True):
    """Recalcule les compteurs stockés de CompanySearchRun à partir de l'état
    réel des SearchCandidate — règles identiques à la fin de
    run_acquisition_pipeline, réutilisées telles quelles par
    repair_creation_date_candidates pour ne jamais avoir deux formules
    différentes (mission 5, correctif pré-production).
    """
    final = SearchCandidate.objects.filter(search_run=search_run)
    scanned_not_eligible = final.filter(status="not_eligible").exclude(outbound_ineligible_reason=NO_SITE_REASON)

    search_run.with_site_count = final.exclude(site_url="").count()
    # "Réellement scannés" : convertis + exclus après analyse réelle — jamais les no_site.
    search_run.enriched_count = final.filter(status="converted").count() + scanned_not_eligible.count()
    search_run.with_email_count = final.exclude(contact_email="").count()
    search_run.qualified_a_count = final.filter(grade="A").count()
    search_run.qualified_b_count = final.filter(grade="B").count()
    search_run.qualified_c_count = final.filter(grade="C").count()
    # Non éligibles après analyse réelle uniquement (exclut les no_site, jamais analysés).
    search_run.not_eligible_count = scanned_not_eligible.count() + final.filter(status="rejected_prescore").count()
    search_run.error_count = final.filter(status="error").count()

    if save:
        search_run.save(update_fields=[
            "with_site_count", "enriched_count", "with_email_count",
            "qualified_a_count", "qualified_b_count", "qualified_c_count",
            "not_eligible_count", "error_count",
        ])
    return search_run


def run_acquisition_pipeline(search_run):
    search_run.status = "running"
    search_run.started_at = search_run.started_at or timezone.now()
    search_run.current_stage = "registry"
    search_run.save(update_fields=["status", "started_at", "current_stage"])

    icp = search_run.icp
    product = search_run.product

    rows = _fetch_registry_candidates(search_run)
    candidates = _create_candidates(search_run, rows)
    search_run.registry_count = SearchCandidate.objects.filter(search_run=search_run).count()
    search_run.current_stage = "prescoring"
    search_run.save(update_fields=["registry_count", "current_stage"])

    all_candidates = list(SearchCandidate.objects.filter(search_run=search_run))
    _prescore_candidates(search_run, all_candidates)

    scored = [
        (c.raw_data, {"score": c.pre_score, "reasons": c.pre_score_reasons, "excluded": c.status == "rejected_prescore", "exclusion_reason": c.outbound_ineligible_reason})
        for c in SearchCandidate.objects.filter(search_run=search_run)
    ]
    candidates_by_siren = {c.siren: c for c in SearchCandidate.objects.filter(search_run=search_run)}
    chosen_rows = preselect_top_candidates(scored, target_count=search_run.volume_max_enrich, minimum_score=0)
    chosen_sirens = {row["siren"] for row, _ in chosen_rows}

    search_run.preselected_count = len(chosen_sirens)
    search_run.current_stage = "site_discovery"
    search_run.save(update_fields=["preselected_count", "current_stage"])

    to_process = [candidates_by_siren[siren] for siren in chosen_sirens if siren in candidates_by_siren]
    to_process.sort(key=lambda c: c.pre_score, reverse=True)

    for candidate in to_process:
        if candidate.status in ("converted", "not_eligible", "error"):
            continue  # déjà traité lors d'une exécution précédente (reprise)
        try:
            _process_candidate(search_run, candidate, icp, product)
        except Exception as exc:
            logger.exception("Acquisition pipeline: erreur sur le candidat %s", candidate.pk)
            candidate.status = "error"
            candidate.error = str(exc)[:2000]
            candidate.save(update_fields=["status", "error"])
            search_run.append_error(f"Candidat {candidate.name} ({candidate.siren}) : {exc}")
            continue

    recompute_search_run_counters(search_run, save=False)
    search_run.status = "done"
    search_run.current_stage = "done"
    search_run.finished_at = timezone.now()
    search_run.save()
    return search_run
