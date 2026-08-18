"""ETAPE 22/23/30 — Acquisition Intelligence, campagnes, tracking, réglages e-mail.

Séparé de views.py (déjà volumineux) pour ne pas fragiliser les vues historiques.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import AcquisitionSearchForm, CampaignCreateForm, EmailComplianceProfileForm, ProductProfileForm
from .services.deliverability import diagnose_domain
from .models import (
    Campaign,
    CampaignProspect,
    CompanySearchRun,
    ConversionEvent,
    EmailComplianceProfile,
    EmailSend,
    EngagementEvent,
    ICPProfile,
    ProductProfile,
    Prospect,
    RevenueAttribution,
)
from .services.campaign_sending import get_or_create_default_sequence, send_campaign_batch
from .services.campaign_metrics import annotate_campaigns_with_metrics, campaign_performance_summary
from .services.commercial_timeline import build_prospect_timeline
from .services.predictneed_email import render_predictneed_email, send_predictneed_campaign_email
from .services.provenance import get_email_provenance
from .services.suppression import is_suppressed
from .services.tracking import resolve_target_url
from .tasks import run_company_search_run_task


# ---------------------------------------------------------------------------
# ETAPE 19 — clic de campagne (redirection avec attribution)
# ---------------------------------------------------------------------------

def campaign_click(request, token):
    campaign_prospect = get_object_or_404(CampaignProspect.objects.select_related("campaign__product", "prospect"), tracking_token=token)
    cta_type = request.GET.get("cta", "product")
    step_id = request.GET.get("step") or None
    variant_id = request.GET.get("variant") or None

    target_url = resolve_target_url(campaign_prospect, cta_type)
    EngagementEvent.objects.create(
        campaign_prospect=campaign_prospect,
        prospect=campaign_prospect.prospect,
        campaign=campaign_prospect.campaign,
        email_step_id=step_id, email_variant_id=variant_id,
        event_type="link_clicked", source="prospectpilot",
        metadata={"cta": cta_type},
    )
    campaign_prospect.last_engagement_at = timezone.now()
    if campaign_prospect.status == "contacted":
        campaign_prospect.status = "engaged"
    campaign_prospect.save(update_fields=["last_engagement_at", "status"])

    if not target_url:
        return HttpResponse("Lien non configuré pour ce produit.", status=404)
    return HttpResponseRedirect(target_url)


# ---------------------------------------------------------------------------
# ETAPE 16 — page de transparence individuelle (rien d'interne exposé)
# ---------------------------------------------------------------------------

def prospect_privacy(request, token):
    prospect = get_object_or_404(Prospect, unsubscribe_token=token)
    email = prospect.public_email or ""
    provenance = get_email_provenance(prospect, email) if email else None
    context = {
        "company_name": prospect.name,
        "email": email,
        "provenance": provenance,
        "unsubscribe_url": reverse("unsubscribe", kwargs={"token": prospect.unsubscribe_token}),
    }
    return render(request, "prospects/prospect_privacy.html", context)


# ---------------------------------------------------------------------------
# ETAPE 4/30 — recherche « Acquisition PredictNeed IA »
# ---------------------------------------------------------------------------

@login_required
def acquisition_search(request):
    form = AcquisitionSearchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        criteria = {}
        if data["department"]:
            criteria["department"] = data["department"]
        if data["region"]:
            criteria["region"] = data["region"]
        if data["icp"].naf_codes:
            criteria["naf_codes"] = data["icp"].naf_codes
        search_run = CompanySearchRun.objects.create(
            user=request.user, mode="acquisition", product=data["product"], icp=data["icp"],
            campaign_name=data["campaign_name"], criteria=criteria,
            volume_max_candidates=data["volume_max_candidates"],
            volume_max_enrich=data["volume_max_enrich"],
            score_threshold=data["score_threshold"],
            status="queued",
        )
        run_company_search_run_task.delay(search_run.pk)
        messages.success(request, "Recherche PredictNeed IA lancée en arrière-plan.")
        return redirect("acquisition_search_run_detail", pk=search_run.pk)

    recent_runs = CompanySearchRun.objects.filter(mode="acquisition").order_by("-created_at")[:10]
    return render(request, "prospects/acquisition_search.html", {"form": form, "recent_runs": recent_runs})


@login_required
def acquisition_search_run_detail(request, pk):
    search_run = get_object_or_404(CompanySearchRun, pk=pk)
    candidates = search_run.candidates.select_related("prospect").order_by("-final_score", "-pre_score")
    return render(request, "prospects/acquisition_search_run_detail.html", {
        "search_run": search_run, "candidates": candidates[:300],
    })


# ---------------------------------------------------------------------------
# ETAPE 22 — Acquisition Intelligence
# ---------------------------------------------------------------------------

FUNNEL_STAGES = [
    ("identified", "Identifiés"),
    ("enriched", "Enrichis"),
    ("qualified", "Qualifiés"),
    ("ready_to_contact", "Prêts"),
    ("contacted", "Contactés"),
    ("engaged", "Engagés"),
    ("signed_up", "Inscrits"),
    ("paying", "Clients"),
]


@login_required
def acquisition_intelligence(request):
    prospects_a = Prospect.objects.filter(predictneed_grade="A", predictneed_excluded=False)
    prospects_b = Prospect.objects.filter(predictneed_grade="B", predictneed_excluded=False)
    ready_to_contact = Prospect.objects.filter(predictneed_stage="ready_to_contact")
    active_campaigns = Campaign.objects.filter(status="active")
    blocked_prospects = Prospect.objects.filter(predictneed_excluded=True)

    emails_sent = EmailSend.objects.filter(status="sent", is_test=False).count()
    clicks = EngagementEvent.objects.filter(event_type="link_clicked").count()
    signups = ConversionEvent.objects.filter(event_type="signup").count()
    activations = ConversionEvent.objects.filter(event_type="activation").count()
    subscriptions = ConversionEvent.objects.filter(event_type="paying").count()
    mrr_total = RevenueAttribution.objects.aggregate(total=Sum("mrr"))["total"] or 0

    # ETAPE 15 — funnel réel (stades réellement observés sur predictneed_stage,
    # jamais un pourcentage fictif).
    stage_counts = dict(
        Prospect.objects.exclude(predictneed_stage="")
        .values_list("predictneed_stage")
        .annotate(n=Count("id"))
    )
    funnel = [{"key": key, "label": label, "count": stage_counts.get(key, 0)} for key, label in FUNNEL_STAGES]

    priority_prospects = (
        Prospect.objects.filter(predictneed_grade__in=["A", "B"], predictneed_excluded=False)
        .select_related("predictneed_icp")
        .prefetch_related("technologies", "competitor_detections__competitor", "agent_briefs")
        .order_by("-predictneed_acquisition_score")[:100]
    )
    for prospect in priority_prospects:
        prospect.top_technologies = list({t.technology for t in prospect.technologies.all() if t.is_active})[:4]
        prospect.top_competitor = next((d.competitor.name for d in prospect.competitor_detections.all()), "")
        latest_brief = max(prospect.agent_briefs.all(), key=lambda b: b.generated_at, default=None)
        prospect.recommended_angle = latest_brief.recommended_angle if latest_brief else ""
        prospect.next_best_action = latest_brief.next_best_action if latest_brief else ""

    active_campaigns_with_metrics = annotate_campaigns_with_metrics(
        Campaign.objects.filter(status="active").select_related("product", "icp")
    )

    context = {
        "count_a": prospects_a.count(),
        "count_b": prospects_b.count(),
        "count_ready": ready_to_contact.count(),
        "count_blocked": blocked_prospects.count(),
        "active_campaigns": active_campaigns.count(),
        "active_campaigns_list": active_campaigns_with_metrics,
        "emails_sent": emails_sent,
        "clicks": clicks,
        "signups": signups,
        "activations": activations,
        "subscriptions": subscriptions,
        "mrr_total": mrr_total,
        "priority_prospects": priority_prospects,
        "funnel": funnel,
    }
    return render(request, "prospects/acquisition_intelligence.html", context)


# ---------------------------------------------------------------------------
# ETAPE 15/17 — campagnes : brouillon -> aperçu -> validation -> envoi
# ---------------------------------------------------------------------------

@login_required
def campaign_list(request):
    campaigns = annotate_campaigns_with_metrics(
        Campaign.objects.select_related("product", "icp").order_by("-created_at")
    )
    return render(request, "prospects/campaign_list.html", {"campaigns": campaigns})


@login_required
def campaign_create(request):
    grade = request.GET.get("grade", "A")
    icp_id = request.GET.get("icp")
    # Mission 5, section 8 : seuls les prospects volontairement retenus
    # (section 3) sont proposables en campagne — jamais un simple candidat
    # technique du pipeline d'acquisition non encore choisi par l'utilisateur.
    base_qs = Prospect.objects.filter(selected_for_prospecting=True)
    if icp_id:
        base_qs = base_qs.filter(predictneed_icp_id=icp_id)
    qs = base_qs.filter(predictneed_excluded=False, outbound_eligible=True)
    if grade == "A":
        qs = qs.filter(predictneed_grade="A")
    elif grade == "AB":
        qs = qs.filter(predictneed_grade__in=["A", "B"])
    qs = qs.order_by("-predictneed_acquisition_score")[:200]

    empty_state_reasons = None
    if request.method != "POST" and not qs.exists():
        graded_ok = base_qs.filter(predictneed_excluded=False, outbound_eligible=True)
        below_threshold = graded_ok
        if grade == "A":
            below_threshold = below_threshold.exclude(predictneed_grade="A")
        elif grade == "AB":
            below_threshold = below_threshold.exclude(predictneed_grade__in=["A", "B"])
        empty_state_reasons = {
            "total_selected": base_qs.count(),
            "without_email": base_qs.filter(public_email="").count(),
            "excluded": base_qs.filter(predictneed_excluded=True).count(),
            "not_outbound_eligible": base_qs.filter(predictneed_excluded=False, outbound_eligible=False).count(),
            "below_threshold": below_threshold.count(),
        }

    if request.method == "POST":
        form = CampaignCreateForm(request.POST)
        selected_ids = request.POST.getlist("selected")
        if form.is_valid() and selected_ids:
            campaign = form.save(commit=False)
            campaign.created_by = request.user
            campaign.status = "draft"
            campaign.sequence = get_or_create_default_sequence(campaign.product, campaign.icp)
            campaign.save()
            for prospect in Prospect.objects.filter(pk__in=selected_ids):
                brief = prospect.agent_briefs.order_by("-generated_at").first()
                CampaignProspect.objects.get_or_create(
                    campaign=campaign, prospect=prospect,
                    defaults={
                        "acquisition_score_snapshot": prospect.predictneed_acquisition_score,
                        "grade": prospect.predictneed_grade,
                        "agent_brief": brief,
                        "status": "selected",
                        "selected_at": timezone.now(),
                    },
                )
            messages.success(request, f"Campagne « {campaign.name} » créée en brouillon avec {len(selected_ids)} prospect(s).")
            return redirect("campaign_detail", pk=campaign.pk)
        messages.error(request, "Sélectionne au moins un prospect et vérifie le formulaire.")
    else:
        form = CampaignCreateForm(initial={
            "product": ProductProfile.objects.filter(active=True).first(),
            "score_threshold": 65, "daily_send_limit": 30, "total_limit": 200,
        })

    return render(request, "prospects/campaign_create.html", {
        "form": form, "prospects": qs, "grade": grade,
        "empty_state_reasons": empty_state_reasons,
    })


@login_required
def campaign_detail(request, pk):
    campaign = get_object_or_404(Campaign.objects.select_related("product", "icp", "sequence"), pk=pk)
    members = list(campaign.campaign_prospects.select_related("prospect").order_by("-acquisition_score_snapshot"))
    mrr_by_prospect = dict(
        RevenueAttribution.objects.filter(campaign=campaign).values_list("prospect_id", "mrr")
    )
    for member in members:
        member.mrr = mrr_by_prospect.get(member.prospect_id)
    performance = campaign_performance_summary(campaign)
    return render(request, "prospects/campaign_detail.html", {
        "campaign": campaign, "members": members, "performance": performance,
    })


@login_required
def campaign_preview(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    members = campaign.campaign_prospects.select_related("prospect").order_by("-acquisition_score_snapshot")
    cp_id = request.GET.get("cp")
    member = members.filter(pk=cp_id).first() if cp_id else members.first()
    if not member:
        messages.error(request, "Aucun prospect dans cette campagne.")
        return redirect("campaign_detail", pk=pk)

    sequence = campaign.sequence or get_or_create_default_sequence(campaign.product, campaign.icp)
    step = sequence.steps.order_by("order").first()
    variant = step.variants.filter(active=True).first() if step else None
    if not step or not variant:
        messages.error(request, "Aucune séquence e-mail configurée pour cette campagne.")
        return redirect("campaign_detail", pk=pk)

    subject, html, text = render_predictneed_email(member, step, variant, request)
    email = member.prospect.public_email or ""
    provenance = get_email_provenance(member.prospect, email) if email else None
    blocked = is_suppressed(email, prospect=member.prospect)
    destination_url = resolve_target_url(member, variant.cta_type)

    return render(request, "prospects/campaign_preview.html", {
        "campaign": campaign, "member": member, "members": members, "subject": subject, "html": html, "text": text,
        "provenance": provenance, "blocked": blocked, "compliance_profile": getattr(campaign.product, "compliance_profile", None),
        "destination_url": destination_url,
    })


@login_required
def campaign_validate(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    if request.method == "POST":
        compliance = getattr(campaign.product, "compliance_profile", None)
        if not compliance or not compliance.compliance_ready:
            messages.error(request, compliance.readiness_reason() if compliance else "Aucun profil de conformité configuré pour ce produit.")
            return redirect("campaign_detail", pk=pk)
        campaign.status = "ready"
        campaign.validated_at = timezone.now()
        campaign.validated_by = request.user
        campaign.save(update_fields=["status", "validated_at", "validated_by"])
        campaign.campaign_prospects.filter(status="selected").update(status="ready_to_contact")
        messages.success(request, "Campagne validée. Elle peut maintenant être envoyée par lots.")
    return redirect("campaign_detail", pk=pk)


@login_required
def campaign_send_batch(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    if request.method == "POST":
        if campaign.status == "ready":
            campaign.status = "active"
            campaign.save(update_fields=["status"])
        summary = send_campaign_batch(campaign)
        messages.success(request, f"Envoi : {summary['sent']} envoyé(s), {summary['blocked']} bloqué(s), {summary['skipped']} reporté(s).")
        for error in summary["errors"][:5]:
            messages.error(request, error)
    return redirect("campaign_detail", pk=pk)


@login_required
def campaign_send_test(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    test_email = request.POST.get("test_email", "").strip()
    member = campaign.campaign_prospects.select_related("prospect").first()
    if request.method == "POST" and test_email and member:
        sequence = campaign.sequence or get_or_create_default_sequence(campaign.product, campaign.icp)
        step = sequence.steps.order_by("order").first()
        variant = step.variants.filter(active=True).first() if step else None
        record = send_predictneed_campaign_email(member, step, variant, request, is_test=True, test_recipient=test_email)
        if record.status == "sent":
            messages.success(request, f"E-mail de test envoyé à {test_email} (aucun statut de campagne modifié).")
        else:
            messages.error(request, f"Échec de l'envoi de test : {record.error}")
    return redirect("campaign_detail", pk=pk)


# ---------------------------------------------------------------------------
# ETAPE 34 — réglages Email PredictNeed IA (jamais les secrets SMTP)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ETAPE 29 (mission 4) — Conversions & Revenus
# ---------------------------------------------------------------------------

@login_required
def conversions_revenue(request):
    conversions = (
        ConversionEvent.objects.select_related("prospect", "campaign")
        .order_by("-occurred_at")[:50]
    )
    revenue_events = (
        RevenueAttribution.objects.select_related("prospect", "campaign", "icp")
        .order_by("-attributed_at")[:50]
    )

    mrr_total = RevenueAttribution.objects.aggregate(total=Sum("mrr"))["total"] or 0
    clients_total = ConversionEvent.objects.filter(event_type="paying").count()

    by_icp = list(
        RevenueAttribution.objects.exclude(icp__isnull=True)
        .values("icp__name")
        .annotate(clients=Count("id", distinct=True), mrr=Sum("mrr"))
        .order_by("-mrr")
    )
    by_campaign = annotate_campaigns_with_metrics(
        Campaign.objects.filter(campaign_prospects__isnull=False).distinct().select_related("product", "icp")
    )
    by_grade = list(
        CampaignProspect.objects.exclude(grade="")
        .values("grade")
        .annotate(
            total=Count("id", distinct=True),
            contacted=Count("id", filter=Q(contacted_at__isnull=False), distinct=True),
            clients=Count("id", filter=Q(status="paying"), distinct=True),
        )
        .order_by("grade")
    )

    context = {
        "conversions": conversions,
        "revenue_events": revenue_events,
        "mrr_total": mrr_total,
        "clients_total": clients_total,
        "by_icp": by_icp,
        "by_campaign": [c for c in by_campaign if c.metrics["clients"] or c.metrics["mrr"] or c.metrics["sent"]],
        "by_grade": by_grade,
    }
    return render(request, "prospects/conversions_revenue.html", context)


@login_required
def email_settings(request):
    product = ProductProfile.objects.filter(slug="predictneed-ia").first()
    if not product:
        messages.error(request, "Produit PredictNeed IA introuvable — lance `initialize_app`.")
        return redirect("dashboard")
    compliance, _ = EmailComplianceProfile.objects.get_or_create(product=product)

    if request.method == "POST":
        product_form = ProductProfileForm(request.POST, instance=product)
        compliance_form = EmailComplianceProfileForm(request.POST, instance=compliance)
        if product_form.is_valid() and compliance_form.is_valid():
            product_form.save()
            compliance_form.save()
            messages.success(request, "Réglages e-mail PredictNeed IA mis à jour.")
            return redirect("email_settings")
    else:
        product_form = ProductProfileForm(instance=product)
        compliance_form = EmailComplianceProfileForm(instance=compliance)

    sender_domain = (product.sender_email or "").split("@", 1)[-1]
    deliverability = diagnose_domain(sender_domain, dkim_selector=settings.EMAIL_DKIM_SELECTOR)
    smtp_configured = bool(settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD)

    return render(request, "prospects/email_settings.html", {
        "product_form": product_form, "compliance_form": compliance_form,
        "compliance_ready": compliance.compliance_ready, "missing_fields": compliance.missing_required_fields,
        "deliverability": deliverability, "smtp_configured": smtp_configured,
        "sender_email": product.sender_email, "reply_to_email": product.reply_to_email,
    })
