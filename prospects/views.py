from datetime import datetime
import time
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import CompanySearchForm, ProspectForm, ContactLogForm, EmailComposeForm, RejectCompanyForm, ProspectImportForm
from .models import (
    Prospect, ContactLog, Suppression, SearchConsoleConnection,
    SearchConsoleMetric, Report, SearchDecision, EmailTemplate,
    PublicEmail, PublicPhone, PublicContactForm, PublicSocialLink,
    EmailSend,
)

from .services.company_search import (
    search_companies,
    fetch_all_companies,
    to_csv,
    to_xlsx,
    get_company_by_siren,
)
from .services.messaging import build_message
from .services.emailing import render_email, send_prospect_email
from .services.suppression import suppress
from .services.commercial_timeline import build_prospect_timeline
from .services.reports import prospects_csv, prospects_xlsx, prospect_pdf
from .services.search_console import (
    create_flow, list_properties, fetch_metrics,
)
from .services.site_discovery import discover_official_site
from .services.crawler import crawl_site
from .services.importers import import_prospects_from_upload
from .tasks import (
    audit_site_task,
    discover_site_task,
    commoncrawl_presence_task,
    scan_search_page_contacts_task,
    scan_search_batch_contacts_task,
    enrich_prospect_task,
)

PROSPECT_LIST_FILTERS = [
    ("all", "Tous", lambda qs: qs),
    ("with_email", "Avec e-mail", lambda qs: qs.exclude(public_email="")),
    ("A", "A", lambda qs: qs.filter(predictneed_grade="A")),
    ("B", "B", lambda qs: qs.filter(predictneed_grade="B")),
    ("C", "C", lambda qs: qs.filter(predictneed_grade="C")),
    ("ready", "Prêts à contacter", lambda qs: qs.filter(predictneed_stage="ready_to_contact")),
    ("contacted", "Contactés", lambda qs: qs.filter(predictneed_stage__in=["contacted", "engaged"])),
    ("signed_up", "Inscrits", lambda qs: qs.filter(predictneed_stage__in=["signed_up", "activated"])),
    ("paying", "Clients", lambda qs: qs.filter(predictneed_stage="paying")),
    ("opposition", "Opposition", lambda qs: qs.filter(status="do_not_contact")),
]
PROSPECT_LIST_FILTER_FUNCS = {key: fn for key, _label, fn in PROSPECT_LIST_FILTERS}


def filtered_prospects(request):
    # Mission 5, section 4 : la liste principale ne montre par défaut que les
    # prospects volontairement retenus pour la prospection (historiques inclus,
    # voir migration 0005) — jamais les simples candidats techniques du
    # pipeline d'acquisition non encore sélectionnés.
    qs = Prospect.objects.filter(selected_for_prospecting=True)
    q = request.GET.get("q", "").strip()
    department = request.GET.get("department", "")
    naf = request.GET.get("naf", "")
    quick_filter = request.GET.get("filter", "all")
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(sector__icontains=q)
            | Q(city__icontains=q)
            | Q(website__icontains=q)
        )
    if department:
        qs = qs.filter(department=department)
    if naf:
        qs = qs.filter(naf_code__startswith=naf)

    # Mission 6, section 14 — filtres Signal Intelligence.
    intent_min = request.GET.get("intent_min", "").strip()
    if intent_min.isdigit():
        qs = qs.filter(intent_score__gte=int(intent_min))

    engagement_min = request.GET.get("engagement_min", "").strip()
    if engagement_min.isdigit():
        qs = qs.filter(engagement_score__gte=int(engagement_min))

    signal_max_age = request.GET.get("signal_max_age", "").strip()
    if signal_max_age.isdigit():
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(days=int(signal_max_age))
        qs = qs.filter(signals__observed_at__gte=cutoff).distinct()

    if request.GET.get("has_linkedin"):
        qs = qs.filter(
            Q(contact_people__profile_url__gt="", contact_people__is_active=True)
            | Q(social_links__platform="linkedin", social_links__is_active=True)
        ).distinct()

    if request.GET.get("has_email"):
        qs = qs.exclude(public_email="")

    in_market = request.GET.get("in_market", "").strip()
    if in_market:
        from .services.in_market_status import IN_MARKET_LEVELS
        bounds = next(((low, high) for low, high, code, _label in IN_MARKET_LEVELS if code == in_market), None)
        if bounds:
            qs = qs.filter(intent_score__gte=bounds[0], intent_score__lte=bounds[1])

    qs = PROSPECT_LIST_FILTER_FUNCS.get(quick_filter, PROSPECT_LIST_FILTER_FUNCS["all"])(qs)
    return qs

@login_required
def dashboard(request):
    # Mission 5, section 6 — cockpit commercial unique : le score/stade
    # PredictNeed est le système principal, plus l'ancien priority_score.
    from django.db.models import Sum
    from .models import Campaign, ConversionEvent, EngagementEvent, RevenueAttribution

    selected_qs = Prospect.objects.filter(selected_for_prospecting=True)
    context = {
        "selected_count": selected_qs.count(),
        "ready_count": selected_qs.filter(predictneed_stage="ready_to_contact").count(),
        "contacted_count": selected_qs.filter(predictneed_stage__in=["contacted", "engaged"]).count(),
        "clicks_count": EngagementEvent.objects.filter(event_type="link_clicked").count(),
        "signups_count": ConversionEvent.objects.filter(event_type="signup").count(),
        "clients_count": ConversionEvent.objects.filter(event_type="paying").count(),
        "mrr_total": RevenueAttribution.objects.aggregate(v=Sum("mrr"))["v"] or 0,
        "priority_prospects": selected_qs.filter(predictneed_grade__in=["A", "B"], predictneed_excluded=False)
            .order_by("-predictneed_acquisition_score")[:10],
        "active_campaigns": Campaign.objects.filter(status="active").select_related("product")[:8],
        "recent_contacts": ContactLog.objects.select_related("prospect")[:8],
    }
    return render(request, "prospects/dashboard.html", context)

@login_required
def prospect_list(request):
    from .services.linkedin_orchestration import linkedin_profile_url
    from .services.next_best_action import NBA_CODES, compute_next_best_action
    from .services.signal_freshness import signal_age_days

    base_qs = Prospect.objects.filter(selected_for_prospecting=True)
    filter_options = [
        {"key": key, "label": label, "count": fn(base_qs).count()}
        for key, label, fn in PROSPECT_LIST_FILTERS
    ]

    prospects = list(filtered_prospects(request))
    for p in prospects:
        last_signal = p.signals.order_by("-observed_at", "-detected_at").first()
        p.last_signal = last_signal
        p.last_signal_age_days = signal_age_days(last_signal.observed_at or last_signal.detected_at) if last_signal else None
        p.has_linkedin_contact = bool(linkedin_profile_url(p))
        p.nba = compute_next_best_action(p)

    nba_filter = request.GET.get("nba", "").strip()
    if nba_filter:
        prospects = [p for p in prospects if p.nba["code"] == nba_filter]

    return render(
        request,
        "prospects/list.html",
        {
            "prospects": prospects,
            "statuses": Prospect.STATUS_CHOICES,
            "filter_options": filter_options,
            "active_filter": request.GET.get("filter", "all"),
            "nba_codes": NBA_CODES,
        },
    )


@login_required
def prospect_import(request):
    form = ProspectImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        result = import_prospects_from_upload(form.cleaned_data["file"], owner=request.user)
        if form.cleaned_data.get("enrich_after_import"):
            for prospect in result["prospects"]:
                enrich_prospect_task.delay(prospect.pk, None, request.user.pk)
        messages.success(
            request,
            f"{result['created']} prospect(s) créé(s), {result['updated']} mis à jour.",
        )
        return redirect("prospect_list")
    return render(request, "prospects/import.html", {"form": form})

@login_required
def prospect_create(request):
    form = ProspectForm(request.POST or None)
    if form.is_valid():
        prospect = form.save(commit=False)
        prospect.owner = request.user
        prospect.save()
        messages.success(request, "Prospect ajouté.")
        return redirect("prospect_detail", pk=prospect.pk)
    return render(
        request,
        "prospects/form.html",
        {"form": form, "title": "Nouveau prospect"},
    )

@login_required
def prospect_edit(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    form = ProspectForm(request.POST or None, instance=prospect)
    if form.is_valid():
        form.save()
        messages.success(request, "Prospect mis à jour.")
        return redirect("prospect_detail", pk=pk)
    return render(
        request,
        "prospects/form.html",
        {"form": form, "title": "Modifier le prospect"},
    )

@login_required
def prospect_detail(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    summary = prospect.audit_summaries.first()
    generated = build_message(prospect, summary)
    form = ContactLogForm(
        request.POST or None,
        initial={"message": generated},
    )
    if request.method == "POST" and form.is_valid():
        log = form.save(commit=False)
        log.prospect = prospect
        log.save()
        prospect.last_contacted_at = timezone.now()
        mapping = {
            "replied": "replied",
            "meeting": "meeting",
            "proposal": "proposal",
            "won": "won",
            "lost": "lost",
            "optout": "do_not_contact",
        }
        prospect.status = mapping.get(log.outcome, "contacted")
        if log.outcome == "replied":
            prospect.last_replied_at = timezone.now()
        if log.outcome == "optout":
            prospect.prospecting_allowed = False
            Suppression.objects.get_or_create(
                prospect=prospect,
                defaults={
                    "email": prospect.public_email,
                    "domain": "",
                    "reason": "Opposition enregistrée",
                },
            )
        prospect.next_action_at = log.follow_up_at
        prospect.save()
        messages.success(request, "Interaction enregistrée.")
        return redirect("prospect_detail", pk=pk)

    email_subject, email_html, email_text = render_email(prospect, None, request)
    email_templates = EmailTemplate.objects.filter(active=True).order_by("name")
    email_sends = prospect.email_sends.select_related("template").all()[:8]

    # Mission 6, section 14 — "Pourquoi contacter cette entreprise maintenant ?"
    from .services.in_market_status import in_market_status
    from .services.linkedin_orchestration import linkedin_profile_url
    from .services.next_best_action import compute_next_best_action
    from .services.signal_freshness import signal_age_days

    last_signal = prospect.signals.order_by("-observed_at", "-detected_at").first()

    return render(
        request,
        "prospects/detail.html",
        {
            "prospect": prospect,
            "summary": summary,
            "latest_run": prospect.crawl_runs.first(),
            "pages": (
                summary.crawl_run.pages.all()[:20]
                if summary else []
            ),
            "backlink": prospect.backlink_snapshots.first(),
            "contact_form": form,
            "email_subject": email_subject,
            "email_html": email_html,
            "email_text": email_text,
            "email_templates": email_templates,
            "email_sends": email_sends,
            "contact_people": prospect.contact_people.all()[:20],
            "evidence_items": prospect.evidence_items.select_related("source")[:50],
            "enrichment_runs": prospect.enrichment_runs.all()[:5],
            # ETAPE 23 — Pourquoi prospecter cette entreprise ? (PredictNeed IA)
            "predictneed_signals": prospect.signals.all()[:20],
            "predictneed_technologies": prospect.technologies.filter(is_active=True),
            "predictneed_competitors": prospect.competitor_detections.select_related("competitor"),
            "predictneed_agent_brief": prospect.agent_briefs.order_by("-generated_at").first(),
            "predictneed_campaigns": prospect.campaign_memberships.select_related("campaign"),
            "predictneed_campaign_membership": prospect.campaign_memberships.order_by("-created_at").first(),
            "predictneed_conversions": prospect.conversion_events.all(),
            "predictneed_revenue": prospect.revenue_attributions.all(),
            "predictneed_timeline": build_prospect_timeline(prospect),
            "predictneed_in_market": in_market_status(prospect),
            "predictneed_nba": compute_next_best_action(prospect),
            "predictneed_linkedin_url": linkedin_profile_url(prospect),
            "predictneed_last_signal": last_signal,
            "predictneed_last_signal_age_days": signal_age_days(last_signal.observed_at or last_signal.detected_at) if last_signal else None,
            "predictneed_decision_maker": prospect.contact_people.filter(is_active=True).order_by("-confidence_score").first(),
        },
    )

@login_required
def discover_site(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    task = discover_site_task.delay(prospect.pk)
    messages.info(
        request,
        f"Recherche du site lancée. Tâche {task.id[:8]}.",
    )
    return redirect("prospect_detail", pk=pk)

@login_required
def start_audit(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    if not prospect.website:
        messages.error(
            request,
            "Ajoutez ou détectez d’abord le site officiel.",
        )
    else:
        task = audit_site_task.delay(prospect.pk)
        messages.info(
            request,
            f"Audit multi-pages lancé. Tâche {task.id[:8]}.",
        )
    return redirect("prospect_detail", pk=pk)

@login_required
def start_commoncrawl(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    if prospect.website:
        task = commoncrawl_presence_task.delay(prospect.pk)
        messages.info(
            request,
            f"Analyse Common Crawl lancée. Tâche {task.id[:8]}.",
        )
    return redirect("prospect_detail", pk=pk)


@login_required
def start_enrichment(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    task = enrich_prospect_task.delay(prospect.pk, None, request.user.pk)
    messages.info(
        request,
        f"Enrichissement multi-sources lancé. Tâche {task.id[:8]}.",
    )
    return redirect("prospect_detail", pk=pk)

def _search_values(request):
    source = request.POST if request.method == "POST" else request.GET
    return {
        "query": source.get("query", "").strip(),
        "naf_code": source.get("naf_code", "").strip(),
        "postal_code": source.get("postal_code", "").strip(),
        "department": source.get("department", "").strip(),
        "city": source.get("city", "").strip(),
        "employee_min": source.get("employee_min", "").strip(),
        "page": int(source.get("page", "1") or 1),
    }

@login_required
def company_search(request):
    values = _search_values(request)
    searched = any(
        values[key]
        for key in [
            "query", "naf_code", "postal_code",
            "department", "city", "employee_min",
        ]
    )
    scan_requested = False
    scan_direct_contacts = (
        searched
        and scan_requested
        and not (request.method == "POST" and "import" in request.POST)
    )
    data = None

    if request.method == "POST" and "scan_direct_contacts" in request.POST:
        if not searched:
            messages.error(request, "Choisissez au moins un critère avant de lancer le scan.")
            return redirect("company_search")

        task = scan_search_page_contacts_task.delay(values, request.user.pk)
        messages.info(
            request,
            f"Scan lancé en arrière-plan. Tâche {task.id[:8]}. "
            "Les prospects avec email ou téléphone apparaîtront dans Prospects.",
        )
        return redirect(
            f"{request.path}?{urlencode({**values, 'scan_task': task.id[:8], 'scan_kind': 'page'})}"
        )

    if request.method == "POST" and "import" in request.POST:
        data = search_companies(**values)
        selected = set(request.POST.getlist("selected"))
        count = 0
        skipped = 0

        for item in data["results"]:
            if item["siret"] not in selected:
                continue

            if not item["prospecting_allowed"]:
                skipped += 1
                continue

            prequalification = prequalify_company_website(item)
            if not has_direct_contact(prequalification):
                skipped += 1
                continue

            _import_company_item(
                request,
                item.copy(),
                prequalification=prequalification,
            )
            count += 1

        messages.success(
            request,
            f"{count} entreprise(s) contactable(s) importée(s), {skipped} ignorée(s).",
        )
        return redirect(
            f"{request.path}?{urlencode(values)}"
        )

    if searched:
        try:
            data = search_companies(**values)
            data["scan_mode"] = False
            data["fast_mode"] = True
            if scan_direct_contacts:
                started_at = time.monotonic()
                scan_seconds = getattr(settings, "SEARCH_SCAN_SECONDS", 25)
                scan_limit = getattr(settings, "SEARCH_SCAN_MAX_RESULTS", 25)
                scanned_count = 0
                skipped_count = 0
                qualified_results = []
                time_limited = False

                for item in data["results"]:
                    if scanned_count >= scan_limit or time.monotonic() - started_at >= scan_seconds:
                        time_limited = True
                        break
                    scanned_count += 1
                    if not item["prospecting_allowed"]:
                        skipped_count += 1
                        continue

                    try:
                        prequalification = prequalify_company_website(item)
                    except Exception:
                        skipped_count += 1
                        continue

                    if not has_direct_contact(prequalification):
                        skipped_count += 1
                        continue

                    enriched = {
                        **item,
                        "prequalification": prequalification,
                        "website": prequalification.get("website", ""),
                        "best_email": prequalification.get("best_email", ""),
                        "best_phone": prequalification.get("best_phone", ""),
                        "emails_found": len(prequalification.get("emails", [])),
                        "phones_found": len(prequalification.get("phones", [])),
                        "pages_checked": prequalification.get("pages_checked", 0),
                    }
                    qualified_results.append(enriched)

                data["raw_total_results"] = data["total_results"]
                data["results"] = qualified_results
                data["total_results"] = len(qualified_results)
                data["scan_mode"] = True
                data["scanned_count"] = scanned_count
                data["skipped_count"] = skipped_count
                data["time_limited"] = time_limited
                data["scan_seconds"] = scan_seconds
                data["scan_limit"] = scan_limit
        except Exception as exc:
            messages.error(
                request,
                "La recherche publique est temporairement indisponible ou les critères sont invalides. "
                f"Détail : {exc}",
            )
            data = None

    form = CompanySearchForm(initial=values)
    query_params = {
        key: value
        for key, value in values.items()
        if key != "page" and value not in ("", None)
    }

    if data:
        current = data["page"]
        total = data["total_pages"]
        start = max(1, current - 3)
        end = min(total, current + 3)
        data["page_range"] = range(start, end + 1)
        data["has_previous"] = current > 1
        data["has_next"] = current < total
        data["previous_page"] = current - 1
        data["next_page"] = current + 1

    return render(
        request,
        "prospects/search.html",
        {
            "form": form,
            "data": data,
            "search_query": urlencode(query_params),
            "values": values,
            "scan_task": request.GET.get("scan_task", ""),
            "scan_kind": request.GET.get("scan_kind", ""),
        },
    )

@login_required
def export_search_csv(request):
    values = _search_values(request)
    results = fetch_all_companies(**values)
    response = HttpResponse(
        to_csv(results),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = (
        'attachment; filename="recherche-entreprises.csv"'
    )
    return response

@login_required
def export_search_xlsx(request):
    values = _search_values(request)
    results = fetch_all_companies(**values)
    response = HttpResponse(
        to_xlsx(results),
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = (
        'attachment; filename="recherche-entreprises.xlsx"'
    )
    return response

@login_required
def export_csv(request):
    response = HttpResponse(
        prospects_csv(filtered_prospects(request)),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = (
        'attachment; filename="prospects.csv"'
    )
    return response

@login_required
def export_xlsx(request):
    response = HttpResponse(
        prospects_xlsx(filtered_prospects(request)),
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = (
        'attachment; filename="prospects.xlsx"'
    )
    return response

@login_required
def prospect_pdf_view(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    summary = prospect.audit_summaries.first()
    pages = (
        list(summary.crawl_run.pages.all())
        if summary else []
    )
    content = prospect_pdf(prospect, summary, pages)
    report = Report(prospect=prospect, format="pdf")
    report.file.save(
        f"audit-{prospect.pk}.pdf",
        ContentFile(content),
        save=True,
    )
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="audit-{prospect.pk}.pdf"'
    )
    return response

@login_required
def mark_optout(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    prospect.prospecting_allowed = False
    prospect.status = "do_not_contact"
    prospect.priority_score = 0
    prospect.predictneed_stage = "do_not_contact"
    prospect.save()
    suppress(email=prospect.public_email, prospect=prospect, reason="Opposition manuelle")
    prospect.campaign_memberships.exclude(
        status__in=["paying", "activated", "signed_up"]
    ).update(status="do_not_contact", excluded_reason="Opposition manuelle")
    messages.success(
        request,
        "Prospect ajouté à la liste d’opposition.",
    )
    return redirect("prospect_detail", pk=pk)

@login_required
def response_board(request):
    logs = ContactLog.objects.select_related(
        "prospect"
    ).order_by("-contacted_at")
    outcome = request.GET.get("outcome", "")
    if outcome:
        logs = logs.filter(outcome=outcome)
    return render(
        request,
        "prospects/responses.html",
        {
            "logs": logs,
            "outcomes": ContactLog.OUTCOMES,
        },
    )

@login_required
def suppression_list(request):
    return render(
        request,
        "prospects/suppressions.html",
        {"items": Suppression.objects.filter(active=True)},
    )

@login_required
def search_console_home(request):
    connection = SearchConsoleConnection.objects.filter(
        user=request.user
    ).first()
    properties = []
    if connection:
        try:
            properties = list_properties(connection)
        except Exception as exc:
            messages.warning(
                request,
                f"Lecture Search Console impossible : {exc}",
            )
    metrics = (
        SearchConsoleMetric.objects.filter(
            connection=connection
        ).order_by("-date")[:100]
        if connection else []
    )
    return render(
        request,
        "prospects/search_console.html",
        {
            "connection": connection,
            "properties": properties,
            "metrics": metrics,
        },
    )

@login_required
def search_console_connect(request):
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        messages.error(
            request,
            "Ajoutez les identifiants OAuth Google dans .env.",
        )
        return redirect("search_console_home")
    flow = create_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    request.session["gsc_oauth_state"] = state
    return redirect(url)

@login_required
def search_console_callback(request):
    flow = create_flow(
        state=request.session.get("gsc_oauth_state")
    )
    flow.fetch_token(
        authorization_response=request.build_absolute_uri()
    )
    credentials = flow.credentials
    SearchConsoleConnection.objects.update_or_create(
        user=request.user,
        defaults={
            "token": credentials.token,
            "refresh_token": credentials.refresh_token or "",
            "token_uri": credentials.token_uri,
            "scopes": list(credentials.scopes or []),
            "expiry": credentials.expiry,
        },
    )
    messages.success(request, "Search Console connecté.")
    return redirect("search_console_home")

@login_required
def search_console_sync(request):
    connection = get_object_or_404(
        SearchConsoleConnection,
        user=request.user,
    )
    property_url = request.POST.get("property_url", "")
    if not property_url:
        messages.error(request, "Choisissez une propriété.")
        return redirect("search_console_home")

    data = fetch_metrics(connection, property_url)
    count = 0
    for row in data.get("rows", []):
        keys = row.get("keys", ["", "", ""])
        SearchConsoleMetric.objects.update_or_create(
            connection=connection,
            property_url=property_url,
            date=keys[0],
            query=keys[1],
            page=keys[2],
            defaults={
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "ctr": row.get("ctr", 0),
                "position": row.get("position", 0),
            },
        )
        count += 1

    connection.selected_property = property_url
    connection.save(update_fields=["selected_property"])
    messages.success(
        request,
        f"{count} lignes Search Console synchronisées.",
    )
    return redirect("search_console_home")

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


def is_contactable(prequalification):
    return bool(
        prequalification.get("best_email")
        or prequalification.get("best_phone")
        or first_contact_form_url(prequalification)
    )


def has_direct_contact(prequalification):
    return bool(
        prequalification.get("best_email")
        or prequalification.get("best_phone")
    )


def prequalify_company_website(company):
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

    if not result["direct_contact"]:
        if result["contactable"]:
            result["error"] = "Formulaire trouvé, mais aucun email ou téléphone public détecté."
        else:
            result["error"] = "Site trouvé, mais aucun email, téléphone ou formulaire de contact détecté."

    return result

def _import_company_item(request, item, prequalification=None, website="", public_email="", public_phone=""):
    prequalification = prequalification or {}
    website = website or prequalification.get("website", "")
    public_email = public_email or prequalification.get("best_email", "")
    public_phone = public_phone or prequalification.get("best_phone", "")
    creation = item.pop("creation_date", None)

    if creation:
        try:
            item["creation_date"] = datetime.fromisoformat(creation).date()
        except ValueError:
            item["creation_date"] = None

    defaults = {
        **item,
        "owner": request.user,
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

    email_items = prequalification.get("emails") or []
    if public_email and not email_items:
        email_items = [{
            "email": public_email,
            "email_type": classify_email_type(public_email),
            "source_url": website,
        }]

    for email_item in email_items:
        email = (
            email_item.get("email", "")
            if isinstance(email_item, dict)
            else str(email_item)
        ).strip().lower()
        if not email:
            continue
        PublicEmail.objects.update_or_create(
            prospect=prospect,
            email=email,
            defaults={
                "email_type": email_item.get("email_type", classify_email_type(email)) if isinstance(email_item, dict) else classify_email_type(email),
                "source_url": email_item.get("source_url", website) if isinstance(email_item, dict) else website,
                "source_type": classify_source_type(email_item.get("source_url", website)) if isinstance(email_item, dict) else classify_source_type(website),
                "is_primary": email == public_email.strip().lower(),
                "is_active": True,
                "discovery_method": "prequalification",
            },
        )

    if public_email:
        PublicEmail.objects.filter(prospect=prospect).update(is_primary=False)
        PublicEmail.objects.filter(prospect=prospect, email=public_email.strip().lower()).update(is_primary=True)

    phone_items = prequalification.get("phones") or []
    if public_phone and not phone_items:
        phone_items = [{"phone": public_phone, "source_url": website}]

    for phone_item in phone_items:
        phone = (
            phone_item.get("phone", "")
            if isinstance(phone_item, dict)
            else str(phone_item)
        ).strip()
        if not phone:
            continue
        PublicPhone.objects.update_or_create(
            prospect=prospect,
            phone=phone,
            defaults={
                "source_url": phone_item.get("source_url", website) if isinstance(phone_item, dict) else website,
                "source_type": classify_source_type(phone_item.get("source_url", website)) if isinstance(phone_item, dict) else classify_source_type(website),
                "is_primary": phone == public_phone.strip(),
                "is_active": True,
                "discovery_method": "prequalification",
            },
        )

    if public_phone:
        PublicPhone.objects.filter(prospect=prospect).update(is_primary=False)
        PublicPhone.objects.filter(prospect=prospect, phone=public_phone.strip()).update(is_primary=True)

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
            "decided_by": request.user,
        },
    )

    return prospect

@login_required
def company_preview(request, siren):
    try:
        item = get_company_by_siren(siren)
    except Exception as exc:
        messages.error(request, f"Impossible de charger cette entreprise : {exc}")
        return redirect("company_search")

    existing = Prospect.objects.filter(siren=siren).first()
    rejected = SearchDecision.objects.filter(
        siren=siren,
        decision="rejected",
    ).first()

    prequalification = None

    if request.method == "POST" and "prequalify" in request.POST:
        prequalification = prequalify_company_website(item)

        if prequalification["direct_contact"]:
            messages.success(
                request,
                "Email ou téléphone public trouvé. Cette entreprise peut être importée.",
            )
        else:
            messages.warning(
                request,
                "Aucun email ou téléphone public trouvé pour cette entreprise.",
            )

    if request.method == "POST" and "accept" in request.POST:
        prequalification = prequalify_company_website(item)

        if not prequalification["direct_contact"]:
            messages.error(
                request,
                "Import bloqué : aucun email ou téléphone public n’a été trouvé pour cette entreprise.",
            )
            return redirect("company_preview", siren=siren)

        prospect = _import_company_item(
            request,
            item.copy(),
            prequalification=prequalification,
        )

        messages.success(request, "Entreprise ajoutée à Prospects avec ses coordonnées publiques.")
        return redirect("prospect_detail", pk=prospect.pk)

    return render(
        request,
        "prospects/company_preview.html",
        {
            "company": item,
            "existing": existing,
            "rejected": rejected,
            "reject_form": RejectCompanyForm(),
            "prequalification": prequalification,
        },
    )

@login_required
def reject_company(request,siren):
    item=get_company_by_siren(siren)
    form=RejectCompanyForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        SearchDecision.objects.update_or_create(siren=siren,decision="rejected",defaults={"siret":item["siret"] or "","company_name":item["name"],"reason":form.cleaned_data["reason"],"decided_by":request.user})
        messages.info(request,"Entreprise marquée comme non intéressante.")
    return redirect("company_preview",siren=siren)

@login_required
def import_all_search(request):
    if request.method != "POST":
        return redirect("company_search")

    values = {
        key: request.POST.get(key, "").strip()
        for key in ["query", "naf_code", "postal_code", "department", "city", "employee_min"]
    }
    task = scan_search_batch_contacts_task.delay(values, request.user.pk)
    messages.info(
        request,
        f"Scan de lot lancé en arrière-plan. Tâche {task.id[:8]}. "
        "Les prospects exploitables seront ajoutés automatiquement.",
    )
    return redirect(
        f"{reverse('company_search')}?{urlencode({**values, 'scan_task': task.id[:8], 'scan_kind': 'batch'})}"
    )

def _selected_email_template_from_request(request):
    template_id = request.POST.get("template") or request.GET.get("template")
    if not template_id:
        return None
    return EmailTemplate.objects.filter(pk=template_id, active=True).first()


@login_required
def email_preview(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    tpl = _selected_email_template_from_request(request)
    action = request.POST.get("compose_action", "preview")

    if request.method == "POST" and action == "load_template":
        subject, html, text = render_email(prospect, tpl, request)
        form = EmailComposeForm(initial={
            "template": tpl,
            "subject": subject,
            "message": text,
            "confirm_professional_relevance": request.POST.get("confirm_professional_relevance") == "on",
        })
    elif request.method == "POST":
        subject, html, text = render_email(
            prospect,
            tpl,
            request,
            subject_override=request.POST.get("subject", ""),
            text_override=request.POST.get("message", ""),
        )
        form = EmailComposeForm(initial={
            "template": tpl,
            "subject": subject,
            "message": text,
            "confirm_professional_relevance": request.POST.get("confirm_professional_relevance") == "on",
        })
    else:
        subject, html, text = render_email(prospect, tpl, request)
        form = EmailComposeForm(initial={"template": tpl, "subject": subject, "message": text})

    return render(
        request,
        "prospects/email_preview.html",
        {
            "prospect": prospect,
            "subject": subject,
            "html": html,
            "text": text,
            "form": form,
        },
    )


@login_required
def email_send(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    form = EmailComposeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        record = send_prospect_email(
            prospect,
            form.cleaned_data.get("template"),
            request,
            subject_override=form.cleaned_data.get("subject", ""),
            text_override=form.cleaned_data.get("message", ""),
        )
        if record.status == "sent":
            messages.success(request, "E-mail envoyé et ajouté à l'historique.")
        else:
            messages.error(request, "E-mail non envoyé : " + record.error)
        return redirect("prospect_detail", pk=pk)

    tpl = _selected_email_template_from_request(request)
    subject, html, text = render_email(
        prospect,
        tpl,
        request,
        subject_override=request.POST.get("subject", "") if request.method == "POST" else "",
        text_override=request.POST.get("message", "") if request.method == "POST" else None,
    )
    return render(
        request,
        "prospects/email_preview.html",
        {
            "prospect": prospect,
            "subject": subject,
            "html": html,
            "text": text,
            "form": form,
        },
    )


@login_required
def crm_api_prospect(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    return JsonResponse({
        "id": prospect.pk,
        "name": prospect.name,
        "status": prospect.status,
        "website": prospect.website,
        "public_email": prospect.public_email,
        "public_phone": prospect.public_phone,
        "city": prospect.city,
        "sector": prospect.sector,
        "priority_score": prospect.priority_score,
        "prospecting_allowed": prospect.prospecting_allowed,
        "last_contacted_at": prospect.last_contacted_at,
    })


@login_required
def email_api_preview(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    tpl = _selected_email_template_from_request(request)
    source = request.POST if request.method == "POST" else request.GET
    subject, html, text = render_email(
        prospect,
        tpl,
        request,
        subject_override=source.get("subject", ""),
        text_override=source.get("message") if "message" in source else None,
    )
    return JsonResponse({
        "prospect_id": prospect.pk,
        "template_id": tpl.pk if tpl else None,
        "subject": subject,
        "html": html,
        "text": text,
    })


@login_required
def email_api_send(request, pk):
    if request.method != "POST":
        return JsonResponse({"error": "POST requis."}, status=405)
    prospect = get_object_or_404(Prospect, pk=pk)
    form = EmailComposeForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    record = send_prospect_email(
        prospect,
        form.cleaned_data.get("template"),
        request,
        subject_override=form.cleaned_data.get("subject", ""),
        text_override=form.cleaned_data.get("message", ""),
    )
    status_code = 200 if record.status == "sent" else 400
    return JsonResponse({
        "email_send_id": record.pk,
        "status": record.status,
        "error": record.error,
        "prospect_status": prospect.status,
    }, status=status_code)


@login_required
def sms_api_status(request):
    return JsonResponse({
        "status": "optional_later",
        "configured": False,
        "message": "SMS API prévu plus tard.",
    })




def unsubscribe(request, token):
    prospect = get_object_or_404(Prospect, unsubscribe_token=token)
    prospect.prospecting_allowed = False
    prospect.status = "do_not_contact"
    prospect.priority_score = 0
    prospect.predictneed_stage = "do_not_contact"
    prospect.save()
    suppress(email=prospect.public_email, prospect=prospect, reason="Désinscription par lien e-mail")
    # ETAPE 17 : bloque aussi les campagnes en cours pour ce prospect.
    prospect.campaign_memberships.exclude(
        status__in=["paying", "activated", "signed_up"]
    ).update(status="do_not_contact", excluded_reason="Désinscription du prospect")
    return render(request, "prospects/unsubscribe.html", {"prospect": prospect})
