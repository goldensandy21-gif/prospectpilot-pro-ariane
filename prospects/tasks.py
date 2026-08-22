from datetime import datetime
from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from .models import (
    Prospect,
    CrawlRun,
    PageAudit,
    SiteAuditSummary,
    BacklinkSnapshot,
    PublicEmail,
    PublicPhone,
    PublicContactForm,
    PublicSocialLink,
    SearchDecision,
)
from .services.crawler import crawl_site, summarize_pages
from .services.company_search import search_companies, fetch_all_companies
from .services.pagespeed import run_pagespeed
from .services.scoring import calculate_scores
from .services.site_discovery import discover_official_site
from .services.commoncrawl import open_web_presence
from .services.enrichment import EnrichmentEngine, create_external_source_records
from .services.acquisition_pipeline import run_acquisition_pipeline

GENERIC_EMAIL_PREFIXES = (
    "contact",
    "info",
    "bonjour",
    "commercial",
    "direction",
    "accueil",
    "hello",
    "support",
    "serviceclient",
    "service-client",
    "rh",
    "recrutement",
    "secretariat",
    "secrétariat",
    "admin",
)


def classify_email_type(email):
    local_part = email.split("@", 1)[0].lower().strip()

    if local_part in GENERIC_EMAIL_PREFIXES:
        return "generic"

    if "." in local_part or "-" in local_part or "_" in local_part:
        return "personal"

    return "unknown"


def classify_source_type(url):
    value = (url or "").lower()

    if "contact" in value:
        return "contact_page"

    if "mentions" in value or "legal" in value or "legales" in value:
        return "legal_notice"

    return "website"



def first_contact_form_url(prequalification):
    forms = prequalification.get("contact_forms", [])
    return forms[0].get("page_url", "") if forms else ""


def has_direct_contact(prequalification):
    return bool(
        prequalification.get("best_email")
        or prequalification.get("best_phone")
    )


def is_contactable(prequalification):
    return bool(
        has_direct_contact(prequalification)
        or first_contact_form_url(prequalification)
    )


def prequalify_company_contact(company):
    result = {
        "website": "",
        "confidence": 0,
        "emails": [],
        "phones": [],
        "contact_forms": [],
        "social_links": [],
        "best_email": "",
        "best_phone": "",
        "best_contact_form": "",
        "contactable": False,
        "direct_contact": False,
        "pages_checked": 0,
        "error": "",
    }

    site = discover_official_site(
        company["name"],
        company.get("city", ""),
        max_candidates=getattr(settings, "SEARCH_SITE_CANDIDATES", 6),
    )
    result["website"] = site.get("url", "")
    result["confidence"] = site.get("confidence", 0)

    if not result["website"]:
        result["error"] = "Aucun site officiel trouvé."
        return result

    try:
        crawl_data = crawl_site(
            result["website"],
            max_pages=getattr(settings, "SEARCH_SCAN_CRAWL_PAGES", 3),
            check_broken_links=False,
        )
    except Exception as exc:
        result["error"] = f"Site trouvé, mais analyse impossible : {exc}"
        return result

    email_sources = {}
    phone_sources = {}
    found_emails = []
    found_phones = []
    found_forms = []
    found_social_links = []

    for page in crawl_data.get("pages", []):
        result["pages_checked"] += 1
        page_url = page.get("url", result["website"])

        for email in page.get("found_emails", []):
            email_clean = email.strip().lower()
            if email_clean:
                found_emails.append(email_clean)
                email_sources.setdefault(email_clean, page_url)

        for phone in page.get("found_phones", []):
            phone_clean = phone.strip()
            if phone_clean:
                found_phones.append(phone_clean)
                phone_sources.setdefault(phone_clean, page_url)

        found_forms.extend(page.get("found_contact_forms", []))
        found_social_links.extend(page.get("found_social_links", []))

    unique_emails = list(dict.fromkeys(found_emails))
    unique_phones = list(dict.fromkeys(found_phones))
    generic_emails = [
        email for email in unique_emails
        if classify_email_type(email) == "generic"
    ]

    result["emails"] = [
        {
            "email": email,
            "email_type": classify_email_type(email),
            "source_url": email_sources.get(email, result["website"]),
        }
        for email in unique_emails
    ]
    result["phones"] = [
        {
            "phone": phone,
            "source_url": phone_sources.get(phone, result["website"]),
        }
        for phone in unique_phones
    ]
    result["contact_forms"] = list({
        (form.get("page_url", ""), form.get("form_action", "")): form
        for form in found_forms
        if form.get("page_url")
    }.values())
    result["social_links"] = list({
        link.get("url", ""): link
        for link in found_social_links
        if link.get("url")
    }.values())
    result["best_email"] = (
        generic_emails or unique_emails
    )[0] if unique_emails else ""
    result["best_phone"] = unique_phones[0] if unique_phones else ""
    result["best_contact_form"] = first_contact_form_url(result)
    result["contactable"] = is_contactable(result)
    result["direct_contact"] = has_direct_contact(result)
    return result


def import_contactable_company(item, prequalification, user_id=None):
    website = prequalification.get("website", "")
    public_email = prequalification.get("best_email", "")
    public_phone = prequalification.get("best_phone", "")
    creation = item.pop("creation_date", None)
    owner = get_user_model().objects.filter(pk=user_id).first() if user_id else None

    if creation:
        try:
            item["creation_date"] = datetime.fromisoformat(creation).date()
        except ValueError:
            item["creation_date"] = None

    defaults = {
        **item,
        "owner": owner,
        "rejected": False,
    }

    if website:
        defaults["website"] = website

    if public_email:
        defaults["public_email"] = public_email.strip().lower()

    if public_phone:
        defaults["public_phone"] = public_phone.strip()

    prospect, _ = Prospect.objects.update_or_create(
        siret=item["siret"],
        defaults=defaults,
    )

    for email_item in prequalification.get("emails") or []:
        email = email_item.get("email", "").strip().lower()
        if not email:
            continue
        source_url = email_item.get("source_url", website)
        PublicEmail.objects.update_or_create(
            prospect=prospect,
            email=email,
            defaults={
                "email_type": email_item.get("email_type", classify_email_type(email)),
                "source_url": source_url,
                "source_type": classify_source_type(source_url),
                "is_primary": email == public_email.strip().lower(),
                "is_active": True,
                "discovery_method": "prequalification",
            },
        )

    if public_email:
        PublicEmail.objects.filter(prospect=prospect).update(is_primary=False)
        PublicEmail.objects.filter(
            prospect=prospect,
            email=public_email.strip().lower(),
        ).update(is_primary=True)

    for phone_item in prequalification.get("phones") or []:
        phone = phone_item.get("phone", "").strip()
        if not phone:
            continue
        source_url = phone_item.get("source_url", website)
        PublicPhone.objects.update_or_create(
            prospect=prospect,
            phone=phone,
            defaults={
                "source_url": source_url,
                "source_type": classify_source_type(source_url),
                "is_primary": phone == public_phone.strip(),
                "is_active": True,
                "discovery_method": "prequalification",
            },
        )

    if public_phone:
        PublicPhone.objects.filter(prospect=prospect).update(is_primary=False)
        PublicPhone.objects.filter(
            prospect=prospect,
            phone=public_phone.strip(),
        ).update(is_primary=True)

    contact_forms = prequalification.get("contact_forms") or []
    if contact_forms:
        PublicContactForm.objects.filter(prospect=prospect).update(is_primary=False)
    for index, form in enumerate(contact_forms):
        page_url = form.get("page_url", "")
        if not page_url:
            continue
        PublicContactForm.objects.update_or_create(
            prospect=prospect,
            page_url=page_url,
            form_action=form.get("form_action", ""),
            defaults={
                "form_method": form.get("form_method", ""),
                "has_email_field": bool(form.get("has_email_field")),
                "has_phone_field": bool(form.get("has_phone_field")),
                "is_primary": index == 0,
                "is_active": True,
                "discovery_method": "prequalification",
            },
        )

    for link in prequalification.get("social_links") or []:
        url = link.get("url", "")
        if not url:
            continue
        PublicSocialLink.objects.update_or_create(
            prospect=prospect,
            url=url,
            defaults={
                "platform": link.get("platform", "other"),
                "source_url": link.get("source_url", website),
                "is_active": True,
                "discovery_method": "prequalification",
            },
        )

    SearchDecision.objects.update_or_create(
        siren=item["siren"],
        decision="accepted",
        defaults={
            "siret": item["siret"] or "",
            "company_name": item["name"],
            "decided_by": owner,
        },
    )

    return prospect


def scan_and_import_contactable_rows(rows, user_id=None):
    imported = 0
    skipped = 0
    rejected = set(
        SearchDecision.objects
        .filter(decision="rejected")
        .values_list("siren", flat=True)
    )

    for item in rows:
        if not item.get("prospecting_allowed") or item.get("siren") in rejected:
            skipped += 1
            continue

        try:
            prequalification = prequalify_company_contact(item)
        except Exception:
            skipped += 1
            continue

        if not has_direct_contact(prequalification):
            skipped += 1
            continue

        import_contactable_company(item.copy(), prequalification, user_id=user_id)
        imported += 1

    return {"imported": imported, "skipped": skipped}


@shared_task(bind=True)
def scan_search_page_contacts_task(self, values, user_id=None):
    data = search_companies(**values)
    result = scan_and_import_contactable_rows(data["results"], user_id=user_id)
    result["page"] = values.get("page", 1)
    result["total_results"] = data.get("total_results", 0)
    return result


@shared_task(bind=True)
def scan_search_batch_contacts_task(self, values, user_id=None):
    rows = fetch_all_companies(
        **values,
        max_results=getattr(settings, "SEARCH_IMPORT_MAX_RESULTS", 75),
    )
    result = scan_and_import_contactable_rows(rows, user_id=user_id)
    result["total_scanned"] = len(rows)
    return result


@shared_task(bind=True)
def enrich_prospect_task(self, prospect_id, source_keys=None, user_id=None, force_refresh=False):
    prospect = Prospect.objects.get(pk=prospect_id)
    owner = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    create_external_source_records()
    run = EnrichmentEngine(source_keys=source_keys, force_refresh=force_refresh).enrich_prospect(prospect, user=owner)
    return {"run_id": run.pk, "status": run.status, "totals": run.totals}

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def discover_site_task(self, prospect_id):
    prospect = Prospect.objects.get(pk=prospect_id)
    result = discover_official_site(prospect.name, prospect.city)
    if result["url"]:
        prospect.website = result["url"]
        prospect.website_confidence = result["confidence"]
        prospect.save(update_fields=["website","website_confidence","updated_at"])
    return result

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def audit_site_task(self, prospect_id, max_pages=None):
    prospect = Prospect.objects.get(pk=prospect_id)
    if not prospect.website:
        raise ValueError("Le prospect ne possède pas de site.")
    run = CrawlRun.objects.create(
        prospect=prospect, start_url=prospect.website,
        pages_requested=max_pages or 12, status="running", started_at=timezone.now()
    )
    try:
        data = crawl_site(prospect.website, max_pages=max_pages)
        run.robots_allowed = data["robots_allowed"]
        run.robots_url = data["robots_url"]
        page_objects = []
        found_emails = []
        found_phones = []
        found_forms = []
        found_social_links = []
        email_sources = {}
        phone_sources = {}

        for page in data["pages"]:
            page_url = page.get("url", prospect.website)
            page_emails = page.pop("found_emails", [])
            page_phones = page.pop("found_phones", [])
            page_forms = page.pop("found_contact_forms", [])
            page_social_links = page.pop("found_social_links", [])
            # Audit correctif round 2, §1 — analyze_html() (crawler.py) renvoie
            # aussi found_people/found_temporal_events (mission 7C/7E) depuis
            # cette même page ; PageAudit n'a pas ces champs et
            # PageAudit.objects.create(**page) lèverait un TypeError sinon.
            # L'audit technique multi-pages (SEO/perf) reste volontairement
            # distinct du pipeline d'enrichissement/personnes : ces données ne
            # sont pas persistées depuis ce chemin (elles le sont déjà via
            # CompanyWebsiteSource/enrich_prospect — voir docs/WEB_DATA_INTELLIGENCE.md).
            page.pop("found_people", None)
            page.pop("found_temporal_events", None)

            found_emails.extend(page_emails)
            found_phones.extend(page_phones)
            found_forms.extend(page_forms)
            found_social_links.extend(page_social_links)

            for email in page_emails:
                email_clean = email.strip().lower()
                if email_clean and email_clean not in email_sources:
                    email_sources[email_clean] = page_url

            for phone in page_phones:
                phone_clean = phone.strip()
                if phone_clean and phone_clean not in phone_sources:
                    phone_sources[phone_clean] = page_url

            page_objects.append(PageAudit.objects.create(
                crawl_run=run, prospect=prospect, **page
            ))

        unique_emails = list(dict.fromkeys(email.strip().lower() for email in found_emails if email.strip()))
        generic = [
            email for email in unique_emails
            if classify_email_type(email) == "generic"
        ]

        best_email = (generic or unique_emails)[0] if (generic or unique_emails) else ""
        unique_phones = list(dict.fromkeys(phone.strip() for phone in found_phones if phone.strip()))
        best_phone = unique_phones[0] if unique_phones else ""

        for email in unique_emails:
            source_url = email_sources.get(email, prospect.website)
            obj, created = PublicEmail.objects.get_or_create(
                prospect=prospect,
                email=email,
                defaults={
                    "email_type": classify_email_type(email),
                    "source_url": source_url,
                    "source_type": classify_source_type(source_url),
                    "is_primary": email == best_email,
                    "is_active": True,
                    "discovery_method": "site_audit",
                },
            )

            if not created:
                obj.email_type = classify_email_type(email)
                obj.source_url = obj.source_url or source_url
                obj.source_type = classify_source_type(obj.source_url)
                obj.is_active = True
                obj.save(update_fields=[
                    "email_type",
                    "source_url",
                    "source_type",
                    "is_active",
                ])

        if best_email:
            PublicEmail.objects.filter(prospect=prospect).update(is_primary=False)
            PublicEmail.objects.filter(prospect=prospect, email=best_email).update(is_primary=True)

        for phone in unique_phones:
            source_url = phone_sources.get(phone, prospect.website)
            obj, created = PublicPhone.objects.get_or_create(
                prospect=prospect,
                phone=phone,
                defaults={
                    "source_url": source_url,
                    "source_type": classify_source_type(source_url),
                    "is_primary": phone == best_phone,
                    "is_active": True,
                    "discovery_method": "site_audit",
                },
            )
            if not created:
                obj.source_url = obj.source_url or source_url
                obj.source_type = classify_source_type(obj.source_url)
                obj.is_active = True
                obj.save(update_fields=["source_url", "source_type", "is_active"])

        if best_phone:
            PublicPhone.objects.filter(prospect=prospect).update(is_primary=False)
            PublicPhone.objects.filter(prospect=prospect, phone=best_phone).update(is_primary=True)

        if found_forms:
            PublicContactForm.objects.filter(prospect=prospect).update(is_primary=False)
        for index, form in enumerate(found_forms):
            page_url = form.get("page_url") or prospect.website
            PublicContactForm.objects.update_or_create(
                prospect=prospect,
                page_url=page_url,
                form_action=form.get("form_action", ""),
                defaults={
                    "form_method": form.get("form_method", ""),
                    "has_email_field": bool(form.get("has_email_field")),
                    "has_phone_field": bool(form.get("has_phone_field")),
                    "is_primary": index == 0,
                    "is_active": True,
                    "discovery_method": "site_audit",
                },
            )

        for link in found_social_links:
            url = link.get("url", "")
            if not url:
                continue
            PublicSocialLink.objects.update_or_create(
                prospect=prospect,
                url=url,
                defaults={
                    "platform": link.get("platform", "other"),
                    "source_url": link.get("source_url", prospect.website),
                    "is_active": True,
                    "discovery_method": "site_audit",
                },
            )

        if not prospect.public_email and best_email:
            prospect.public_email = best_email

        if not prospect.public_phone and best_phone:
            prospect.public_phone = best_phone
        summary_data = summarize_pages(data["pages"])
        try:
            speed = run_pagespeed(prospect.website)
        except Exception:
            speed = {"performance_score":None,"seo_score":None,"accessibility_score":None}
        summary = SiteAuditSummary.objects.create(
            prospect=prospect, crawl_run=run, **summary_data, **speed
        )

        tech, commercial, fit, priority, reasons = calculate_scores(prospect, summary, page_objects)
        prospect.technical_score = tech
        prospect.commercial_score = commercial
        prospect.fit_score = fit
        prospect.priority_score = priority
        prospect.score_reasons = reasons
        if priority >= 60 and prospect.status == "new":
            prospect.status = "qualified"
        prospect.save()
        run.status = "done"
        run.pages_crawled = len(page_objects)
        run.finished_at = timezone.now()
        run.save()

        # Correctif d'audit (round 2 puis 3) : chaque nouvel audit RÉUSSI
        # devient un instantané comparable au précédent — appelé seulement
        # maintenant, une fois run.status="done" et run.pages_crawled
        # définitivement posés (SiteChangeSignalCollector filtre sur
        # crawl_run__status="done" et compare la couverture pages_crawled —
        # les deux seraient faux/à zéro s'il était appelé plus tôt). Isolé
        # (jamais bloquant) : un échec ici ne doit jamais faire échouer l'audit.
        try:
            from .services.signal_collectors import SiteChangeSignalCollector, run_signal_collectors
            run_signal_collectors(prospect, collectors=[SiteChangeSignalCollector()])
        except Exception:
            pass

        return {"run_id":run.pk,"pages":len(page_objects),"priority":priority}
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = timezone.now()
        run.save()
        raise

@shared_task
def commoncrawl_presence_task(prospect_id):
    prospect = Prospect.objects.get(pk=prospect_id)
    if not prospect.website:
        raise ValueError("Site absent")
    data = open_web_presence(prospect.website)
    snapshot = BacklinkSnapshot.objects.create(prospect=prospect,source="common_crawl",**data)
    return snapshot.pk

@shared_task
def scheduled_refresh_top_prospects():
    # Correctif d'audit (round 3) : dernier tri legacy encore en production —
    # remplacé par le score canonique Mission 6. Exclut aussi les prospects
    # déjà exclus commercialement (predictneed_excluded) : ré-auditer un
    # prospect déjà écarté (opposition, déjà client...) ne sert à rien.
    ids = list(
        Prospect.objects.filter(prospecting_allowed=True, predictneed_excluded=False)
        .exclude(website="")
        .order_by("-predictneed_acquisition_score")
        .values_list("id", flat=True)[:50]
    )
    for prospect_id in ids:
        audit_site_task.delay(prospect_id)
    return len(ids)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=1)
def run_company_search_run_task(self, search_run_id):
    """ETAPE 4/26 — Pipeline complet d'acquisition PredictNeed IA pour un CompanySearchRun.

    Ré-invocable sans effet de bord : les candidats déjà traités (status
    converted/not_eligible/error) sont ignorés, ce qui permet une reprise après
    échec partiel sans dupliquer le travail déjà fait.
    """
    from .models import CompanySearchRun

    search_run = CompanySearchRun.objects.get(pk=search_run_id)
    try:
        run_acquisition_pipeline(search_run)
    except Exception as exc:
        search_run.status = "failed"
        search_run.append_error(f"Échec du pipeline : {exc}", save=False)
        search_run.finished_at = timezone.now()
        search_run.save(update_fields=["status", "errors", "finished_at"])
        raise
    return {
        "search_run_id": search_run.pk,
        "status": search_run.status,
        "registry_count": search_run.registry_count,
        "preselected_count": search_run.preselected_count,
        "qualified_a": search_run.qualified_a_count,
        "qualified_b": search_run.qualified_b_count,
        "qualified_c": search_run.qualified_c_count,
    }


@shared_task
def run_scheduled_search_preset_task(preset_id):
    """ETAPE 25 — SearchPreset planifié via Celery Beat.

    Ne déclenche jamais d'envoi d'e-mail : crée uniquement un CompanySearchRun.
    L'envoi reste soumis à une campagne distincte, explicitement validée."""
    from .models import CompanySearchRun, SearchPreset

    preset = SearchPreset.objects.get(pk=preset_id, active=True)
    search_run = CompanySearchRun.objects.create(
        mode="acquisition", product=preset.product, icp=preset.icp, preset=preset,
        campaign_name=f"Planifié — {preset.name}",
        criteria=preset.filters, volume_max_candidates=preset.volume_max_candidates,
        volume_max_enrich=preset.volume_max_enrich, score_threshold=preset.score_threshold,
    )
    preset.last_run_at = timezone.now()
    preset.save(update_fields=["last_run_at"])
    run_company_search_run_task.delay(search_run.pk)
    return search_run.pk
