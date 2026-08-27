"""ETAPE 22/23/30 — Acquisition Intelligence, campagnes, tracking, réglages e-mail.

Séparé de views.py (déjà volumineux) pour ne pas fragiliser les vues historiques.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from datetime import timedelta

from .forms import AcquisitionSearchForm, CampaignCreateForm, EmailComplianceProfileForm, ProductProfileForm
from .services.deliverability import diagnose_domain
from .models import (
    Campaign,
    CampaignProspect,
    CompanySearchRun,
    ConversionEvent,
    EmailAutomationSettings,
    EmailComplianceProfile,
    EmailSend,
    EngagementEvent,
    ICPProfile,
    PlannedEmailContent,
    ProductProfile,
    Prospect,
    RevenueAttribution,
)
from .services.campaign_sending import get_or_create_default_sequence, send_campaign_batch
from .services.campaign_metrics import annotate_campaigns_with_metrics, campaign_performance_summary
from .services.campaign_sequencing import _next_step as _sequence_next_step
from .services.commercial_timeline import build_prospect_timeline
from .services.email_automation import mark_stale_if_changed
from .services.predictneed_email import render_predictneed_email, send_predictneed_campaign_email
from .services.provenance import get_email_provenance
from .services.suppression import is_suppressed
from .services.tracking import resolve_target_url
from .tasks import run_company_search_run_task

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# ETAPE 19 — clic de campagne (redirection avec attribution)
# ---------------------------------------------------------------------------

def campaign_click(request, token):
    campaign_prospect = get_object_or_404(CampaignProspect.objects.select_related("campaign__product", "prospect"), tracking_token=token)
    cta_type = request.GET.get("cta", "product")
    step_id = request.GET.get("step") or None
    variant_id = request.GET.get("variant") or None

    target_url = resolve_target_url(campaign_prospect, cta_type)
    # Section I (automatisation email) : observabilité légère du clic
    # (User-Agent tronqué) — n'affirme jamais distinguer humain vs scanner,
    # simple donnée brute disponible pour une lecture manuelle ultérieure.
    user_agent = request.META.get("HTTP_USER_AGENT", "")[:300]
    EngagementEvent.objects.create(
        campaign_prospect=campaign_prospect,
        prospect=campaign_prospect.prospect,
        campaign=campaign_prospect.campaign,
        email_step_id=step_id, email_variant_id=variant_id,
        event_type="link_clicked", source="prospectpilot",
        metadata={"cta": cta_type, "user_agent": user_agent},
    )
    campaign_prospect.last_engagement_at = timezone.now()
    if campaign_prospect.status == "contacted":
        campaign_prospect.status = "engaged"
    campaign_prospect.save(update_fields=["last_engagement_at", "status"])

    if not target_url:
        return HttpResponse("Lien non configuré pour ce produit.", status=404)
    return HttpResponseRedirect(target_url)


# 1x1 transparent GIF, served with no external dependency.
_TRACKING_PIXEL_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


def track_email_open(request, token):
    """Section H (automatisation email) — pixel d'ouverture indicatif.
    Jamais pour un EmailSend is_test (aucun token n'est alors généré à
    l'envoi, voir services/predictneed_email.py). Ne stocke aucune IP."""
    record = EmailSend.objects.filter(open_tracking_token=token, is_test=False).first()
    if record:
        now = timezone.now()
        first_open = record.first_opened_at is None
        record.last_opened_at = now
        record.open_count = record.open_count + 1
        if first_open:
            record.first_opened_at = now
        record.save(update_fields=["first_opened_at", "last_opened_at", "open_count"])
        if first_open:
            EngagementEvent.objects.create(
                campaign_prospect=record.campaign_prospect,
                prospect=record.prospect,
                campaign=record.campaign_prospect.campaign if record.campaign_prospect else None,
                email_step=record.email_step, email_variant=record.email_variant,
                event_type="email_opened", source="prospectpilot",
                metadata={"email_send_id": record.pk},
            )
    return HttpResponse(_TRACKING_PIXEL_GIF, content_type="image/gif")


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


def test_unsubscribe_preview(request):
    """Section C (Round D, verrous production) — cible du lien « Se
    désabonner » d'un e-mail is_test=True. Aucun token, aucun Prospect
    résolu, AUCUNE mutation (jamais de Suppression, jamais de statut
    do_not_contact) : purement une page d'explication statique."""
    return render(request, "prospects/test_unsubscribe_preview.html")


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


CANDIDATE_FILTERS = [
    ("all", "Tous", lambda qs: qs),
    ("with_email", "Avec e-mail", lambda qs: qs.exclude(contact_email="")),
    ("with_site", "Avec site", lambda qs: qs.exclude(site_url="")),
    ("no_email", "Sans e-mail", lambda qs: qs.filter(contact_email="")),
    ("A", "A", lambda qs: qs.filter(grade="A")),
    ("B", "B", lambda qs: qs.filter(grade="B")),
    ("C", "C", lambda qs: qs.filter(grade="C")),
    ("errors", "Erreurs", lambda qs: qs.filter(status="error")),
    ("not_eligible", "Non éligibles", lambda qs: qs.filter(status="not_eligible")),
]
CANDIDATE_FILTER_FUNCS = {key: fn for key, _label, fn in CANDIDATE_FILTERS}


@login_required
def acquisition_search_run_detail(request, pk):
    search_run = get_object_or_404(CompanySearchRun, pk=pk)

    if request.method == "POST":
        selected_ids = request.POST.getlist("selected")
        candidates_with_prospect = search_run.candidates.filter(
            pk__in=selected_ids, prospect__isnull=False,
        ).select_related("prospect")
        added = 0
        already_selected = 0
        for candidate in candidates_with_prospect:
            if candidate.prospect.selected_for_prospecting:
                already_selected += 1
                continue
            candidate.prospect.selected_for_prospecting = True
            candidate.prospect.selected_at = timezone.now()
            candidate.prospect.save(update_fields=["selected_for_prospecting", "selected_at"])
            added += 1
        without_prospect = len(selected_ids) - candidates_with_prospect.count()
        if added:
            messages.success(request, f"{added} entreprise(s) ajoutée(s) aux Prospects.")
        if already_selected:
            messages.info(request, f"{already_selected} étaient déjà dans les Prospects.")
        if without_prospect:
            messages.warning(request, f"{without_prospect} candidat(s) pas encore assez avancé(s) pour être ajouté(s) (pas encore de site/analyse).")
        if not selected_ids:
            messages.error(request, "Sélectionne au moins une entreprise avant d'ajouter aux Prospects.")
        return redirect("acquisition_search_run_detail", pk=pk)

    active_filter = request.GET.get("filter", "all")
    candidates = search_run.candidates.select_related("prospect").order_by("-final_score", "-pre_score")
    candidates = CANDIDATE_FILTER_FUNCS.get(active_filter, CANDIDATE_FILTER_FUNCS["all"])(candidates)

    all_candidates = search_run.candidates.all()
    filter_options = [
        {"key": key, "label": label, "count": fn(all_candidates).count()}
        for key, label, fn in CANDIDATE_FILTERS
    ]

    return render(request, "prospects/acquisition_search_run_detail.html", {
        "search_run": search_run, "candidates": candidates[:300],
        "active_filter": active_filter, "filter_options": filter_options,
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


GRADE_FILTERS = {
    "A": ["A"],
    "AB": ["A", "B"],
    "ABC": ["A", "B", "C"],
}


@login_required
def campaign_create(request):
    grade = request.GET.get("grade", "A")
    icp_id = request.GET.get("icp")
    min_score = request.GET.get("min_score", "")
    prospect_ids = [pid for pid in request.GET.get("prospects", "").split(",") if pid.isdigit()]

    # Mission 5, section 8 : seuls les prospects volontairement retenus
    # (section 3) sont proposables en campagne — jamais un simple candidat
    # technique du pipeline d'acquisition non encore choisi par l'utilisateur.
    base_qs = Prospect.objects.filter(selected_for_prospecting=True)
    if prospect_ids:
        base_qs = base_qs.filter(pk__in=prospect_ids)
    if icp_id:
        base_qs = base_qs.filter(predictneed_icp_id=icp_id)
    qs = base_qs.filter(predictneed_excluded=False, outbound_eligible=True)
    if grade in GRADE_FILTERS:
        qs = qs.filter(predictneed_grade__in=GRADE_FILTERS[grade])
    if min_score.isdigit():
        qs = qs.filter(predictneed_acquisition_score__gte=int(min_score))

    # Section 2.A (correctif automatisation) — pour une création de campagne
    # destinée au Planning e-mail (?planning=1), un prospect déjà
    # premier-contacté commercialement (n'importe quelle campagne, verrou
    # global has_prior_commercial_first_contact) n'apparaît plus parmi les
    # candidats sélectionnables. La garde-fou réelle et non contournable
    # (POST forgé y compris) reste le contrôle POST ci-dessous — ce filtre
    # n'est qu'une aide d'affichage. Doit rester AVANT le slicing [:200]
    # ci-dessous : Django refuse tout .exclude()/.filter() supplémentaire
    # sur une QuerySet déjà tranchée.
    planning_intent = request.GET.get("planning") == "1"
    if planning_intent:
        from .services.email_automation import contacted_prospect_ids
        qs = qs.exclude(pk__in=contacted_prospect_ids())

    qs = qs.order_by("-predictneed_acquisition_score")[:200]

    empty_state_reasons = None
    if request.method != "POST" and not qs.exists():
        graded_ok = base_qs.filter(predictneed_excluded=False, outbound_eligible=True)
        below_threshold = graded_ok
        if grade in GRADE_FILTERS:
            below_threshold = below_threshold.exclude(predictneed_grade__in=GRADE_FILTERS[grade])
        if min_score.isdigit():
            below_threshold = below_threshold.filter(predictneed_acquisition_score__lt=int(min_score))
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
            # Correctif d'audit (LinkedIn/Hunter.io) : ne remplace la
            # séquence choisie que si le formulaire l'a laissée vide —
            # avant ce correctif, toute sélection était systématiquement
            # écrasée par la séquence e-mail par défaut.
            #
            # Section 3 (audit correctif final) : une campagne Planning sans
            # séquence explicite ne doit JAMAIS tomber silencieusement sur la
            # séquence legacy 3 étapes 0/4/8 (get_or_create_default_sequence)
            # — elle reçoit directement une séquence dédiée, déjà garantie
            # 4 étapes J0/J4/J8/J14. Les campagnes manuelles gardent
            # exactement leur comportement historique.
            if not campaign.sequence_id:
                if campaign.planning_managed:
                    from .services.email_automation import get_or_create_planning_default_sequence
                    campaign.sequence = get_or_create_planning_default_sequence(campaign.product, campaign.icp)
                else:
                    campaign.sequence = get_or_create_default_sequence(campaign.product, campaign.icp)
            campaign.save()

            # Section 2.B (correctif automatisation) — garde-fou réel avant
            # inscription, non contournable par un POST forgé/manipulé :
            # pour une campagne planning_managed, aucun prospect déjà
            # premier-contacté commercialement (verrou global, historique
            # EmailSend réel) n'est jamais inscrit comme nouveau
            # CampaignProspect de premier contact, quelle que soit la liste
            # envoyée dans la requête.
            from .services.email_automation import has_prior_commercial_first_contact
            blocked_prospects = []
            created_count = 0
            for prospect in Prospect.objects.filter(pk__in=selected_ids):
                if campaign.planning_managed and has_prior_commercial_first_contact(prospect):
                    blocked_prospects.append(prospect.name)
                    continue
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
                created_count += 1

            if blocked_prospects:
                names = ", ".join(blocked_prospects[:5]) + ("…" if len(blocked_prospects) > 5 else "")
                messages.warning(
                    request,
                    f"{len(blocked_prospects)} prospect(s) déjà contacté(s) commercialement ont été "
                    f"automatiquement exclus (verrou anti-doublon) : {names}",
                )
            messages.success(request, f"Campagne « {campaign.name} » créée en brouillon avec {created_count} prospect(s).")
            return redirect("campaign_detail", pk=campaign.pk)
        messages.error(request, "Sélectionne au moins un prospect et vérifie le formulaire.")
    else:
        form = CampaignCreateForm(initial={
            "product": ProductProfile.objects.filter(active=True).first(),
            "score_threshold": 65, "daily_send_limit": 30, "total_limit": 200,
        })

    return render(request, "prospects/campaign_create.html", {
        "form": form, "prospects": qs, "grade": grade, "min_score": min_score,
        "prospects_param": request.GET.get("prospects", ""),
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
    # Section B (Round D, verrous production) : une campagne pilotée par le
    # Planning e-mail ne peut JAMAIS être validée par cet ancien flux — sa
    # seule voie de validation est « Valider et programmer » (Planning),
    # qui exige un test réussi sur chaque contenu (voir
    # email_planning_validate_and_schedule/promote_campaign_after_validation).
    if campaign.planning_managed:
        messages.error(request, "Cette campagne est pilotée par le Planning e-mail — validez-la depuis Planning e-mail (Préparer → Tester → Valider).")
        return redirect("email_planning")
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
    # Section B (Round D) : idem campaign_validate — l'envoi par lot legacy
    # ne doit jamais pouvoir contourner Préparer/Tester/Valider pour une
    # campagne Planning. Le garde-fou réel et non contournable reste
    # send_campaign_batch() elle-même (voir campaign_sending.py).
    if campaign.planning_managed:
        messages.error(request, "Cette campagne est pilotée par le Planning e-mail — l'envoi se fait depuis Planning e-mail, jamais par l'envoi par lot legacy.")
        return redirect("email_planning")
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
    # Section B (Round D) : le test legacy (rendu live, step 1 uniquement)
    # ne doit jamais être utilisé pour une campagne Planning — le seul test
    # valable est « Envoyer les tests » (Planning), qui envoie le contenu
    # FIGÉ et alimente le garde-fou de validation (tested_content_hash).
    if campaign.planning_managed:
        messages.error(request, "Cette campagne est pilotée par le Planning e-mail — utilisez « Envoyer les tests » depuis Planning e-mail.")
        return redirect("email_planning")
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


# ---------------------------------------------------------------------------
# Planning e-mail — automatisation J0/J4/J8/J14 (Campagnes > Planning e-mail)
#
# Périmètre assumé : cette page planifie/valide/envoie la séquence pour des
# CampaignProspect DÉJÀ inscrits dans une campagne planning_managed=True —
# elle n'invente pas de nouvel algorithme de sélection de prospects dans
# toute la base (la sélection reste la Recherche intelligente / création de
# campagne existante). Une campagne planning_managed se crée normalement
# (campaign_create), puis se pilote ensuite depuis cette page.
# ---------------------------------------------------------------------------

def _week_dates(reference_date, offset_weeks=0):
    monday = reference_date - timedelta(days=reference_date.weekday()) + timedelta(weeks=offset_weeks)
    return [monday + timedelta(days=i) for i in range(5)]


def _planning_rows(campaign_prospects):
    rows = []
    for cp in campaign_prospects:
        sequence = cp.campaign.sequence
        if not sequence:
            continue
        next_step = _sequence_next_step(cp)

        if cp.status in ("do_not_contact", "lost", "excluded"):
            status_label = "Arrêté" if cp.status == "excluded" else "Bloqué"
            rows.append({"cp": cp, "step": next_step, "status": status_label, "planned": None})
            continue

        if next_step is None:
            rows.append({"cp": cp, "step": None, "status": "Terminé", "planned": None})
            continue

        sent_record = EmailSend.objects.filter(
            campaign_prospect=cp, email_step=next_step, is_test=False, status="sent",
        ).order_by("-sent_at").first()
        if sent_record:
            clicked = cp.events.filter(event_type="link_clicked").exists()
            replied = cp.prospect.contact_logs.filter(outcome="replied").exists()
            rows.append({
                "cp": cp, "step": next_step, "status": "Envoyé", "planned": None,
                "sent_record": sent_record, "clicked": clicked, "replied": replied,
            })
            continue

        planned = PlannedEmailContent.objects.filter(campaign_prospect=cp, email_step=next_step).first()
        if planned:
            mark_stale_if_changed(planned)
            planned.refresh_from_db()
        status_label = {
            None: "Brouillon",
            "to_validate": "À valider",
            "stale": "À valider (contenu modifié)",
            "validated": "Validé",
        }[planned.status if planned else None]

        rows.append({"cp": cp, "step": next_step, "status": status_label, "planned": planned})
    return rows


@login_required
def email_planning(request):
    settings_row = EmailAutomationSettings.current()
    tz = ZoneInfo(settings_row.timezone_name)
    today = timezone.now().astimezone(tz).date()

    campaigns = Campaign.objects.filter(planning_managed=True).select_related("product", "sequence")
    campaign_prospects = (
        CampaignProspect.objects.filter(campaign__in=campaigns)
        .select_related("campaign", "campaign__sequence", "prospect", "current_step")
        .order_by("-acquisition_score_snapshot")
    )
    rows = _planning_rows(campaign_prospects)

    return render(request, "prospects/email_planning.html", {
        "settings": settings_row,
        "this_week": _week_dates(today, 0),
        "next_week": _week_dates(today, 1),
        "rows": rows,
        "test_recipient": settings.EMAIL_HOST_USER or "contact-predict@predictneed-ia.com",
    })


PLANNED_STATUS_DISPLAY = {
    "to_validate": "À relire",
    "modified": "Modifié — à reprogrammer",
    "stale": "Modifié — à reprogrammer",
    "validated": "Programmé",
}

SEND_NOW_REASON_LABELS = {
    "not_programmed": "ce contenu n'est pas (ou plus) Programmé.",
    "deferred_daily_total_limit": "limite globale d'envois du jour déjà atteinte.",
    "deferred_new_contacts_limit": "limite de nouveaux contacts du jour déjà atteinte.",
    "not_sendable": "campagne non validée ou non active.",
    "stopped": "séquence arrêtée pour ce prospect (opposition, perdu, exclu...).",
    "no_sequence": "aucune séquence active pour cette campagne.",
    "sequence_complete": "la séquence est déjà terminée pour ce prospect.",
    "waiting": "ce n'est pas encore l'étape due (délai non écoulé).",
    "blocked_total_limit": "limite totale d'envois de la campagne atteinte.",
    "blocked_daily_limit": "limite quotidienne d'envois de la campagne atteinte.",
    "skipped_domain_already_contacted_today": "un autre contact du même domaine a déjà été contacté aujourd'hui.",
    "blocked_awaiting_validation": "le contenu de cette étape n'est pas (ou plus) validé.",
    "deferred_not_yet_due": "la date programmée n'est pas encore atteinte.",
    "blocked_duplicate_first_contact": "premier contact déjà envoyé pour ce prospect (verrou global anti-doublon).",
    "blocked_permanent_failure": "échec définitif après plusieurs tentatives — nécessite une intervention.",
    "blocked_retry_backoff": "un nouvel essai est déjà programmé après un échec récent — pas encore.",
    "email_suppressed": "opposition détectée juste avant l'envoi — séquence arrêtée.",
    "email_blocked": "adresse email non exploitable.",
    "email_failed": "échec technique de l'envoi (SMTP) — nouvel essai possible plus tard.",
    "unknown_channel": "canal non pris en charge pour cette étape.",
}


def _prepared_content_display_status(planned, was_sent):
    """Workflow final, section 10 — statuts lisibles côté interface.
    « Envoyé »/« Annulé / exclu » priment toujours sur le statut brut de
    PlannedEmailContent : un contenu réellement envoyé ou un prospect arrêté
    ne doit jamais être présenté comme « À relire »/« Programmé »."""
    if was_sent:
        return "Envoyé"
    cp = planned.campaign_prospect
    if cp.status in ("do_not_contact", "excluded", "lost", "churned"):
        return "Annulé / exclu"
    return PLANNED_STATUS_DISPLAY.get(planned.status, planned.status)


@login_required
def email_planning_prepared(request):
    """Section 1 (workflow final) — « Emails préparés » : TOUS les
    PlannedEmailContent de la semaine en cours, une ligne par contenu
    (contrairement à email_planning() qui affiche une ligne par prospect,
    limitée à sa seule PROCHAINE étape). C'est ici que l'utilisatrice
    relit, modifie et programme chaque email — individuellement ou en lot
    (voir email_planning_content_detail / email_planning_programmer_selection)."""
    settings_row = EmailAutomationSettings.current()
    tz = ZoneInfo(settings_row.timezone_name)
    today = timezone.now().astimezone(tz).date()
    week_dates = _week_dates(today, 0)

    campaigns = Campaign.objects.filter(planning_managed=True)
    planned_list = list(
        PlannedEmailContent.objects.filter(
            campaign_prospect__campaign__in=campaigns,
            scheduled_date__gte=week_dates[0], scheduled_date__lte=week_dates[-1],
        )
        .select_related("campaign_prospect__prospect", "campaign_prospect__campaign", "email_step")
        .order_by("scheduled_date", "email_step__order", "-campaign_prospect__acquisition_score_snapshot")
    )
    for planned in planned_list:
        mark_stale_if_changed(planned)  # jamais silencieux : re-vérifié à chaque affichage

    sent_pairs = set(
        EmailSend.objects.filter(
            campaign_prospect__campaign__in=campaigns, is_test=False, status="sent",
        ).values_list("campaign_prospect_id", "email_step_id")
    )

    rows = []
    for planned in planned_list:
        was_sent = (planned.campaign_prospect_id, planned.email_step_id) in sent_pairs
        rows.append({
            "planned": planned,
            "display_status": _prepared_content_display_status(planned, was_sent),
            "tested": bool(planned.tested_content_hash and planned.tested_content_hash == planned.content_hash),
            "programmed": planned.status == "validated",
            "sent": was_sent,
        })

    return render(request, "prospects/email_planning_prepared.html", {
        "rows": rows, "week_dates": week_dates, "settings": settings_row,
        "test_recipient": settings.EMAIL_HOST_USER or "contact-predict@predictneed-ia.com",
    })


@login_required
def email_planning_content_detail(request, planned_id):
    """Section 2/3/5/6 (workflow final) — ouvre UN PlannedEmailContent
    précis : le rendu affiché est TOUJOURS le contenu FIGÉ exact (jamais un
    nouveau rendu généré à la volée). Permet de le modifier (sujet + texte
    rédactionnel uniquement — jamais du HTML brut, voir
    email_automation.apply_manual_edit), d'envoyer un test facultatif, ou
    de le programmer individuellement."""
    planned = get_object_or_404(
        PlannedEmailContent.objects.select_related(
            "campaign_prospect__prospect", "campaign_prospect__campaign", "email_step",
        ),
        pk=planned_id, campaign_prospect__campaign__planning_managed=True,
    )
    mark_stale_if_changed(planned)
    planned.refresh_from_db()

    # Correctif (rattrapage manuel « Envoyer maintenant ») : PlannedEmailContent.
    # status reste "validated" pour toujours après un envoi réel — seul un
    # EmailSend(is_test=False, status="sent") existant fait foi qu'il est
    # RÉELLEMENT parti (même logique que email_planning_prepared()). Sans ce
    # contrôle, le statut affiché ET le bouton « Envoyer maintenant »
    # resteraient trompeurs sur un email déjà envoyé par le scheduler.
    was_sent = EmailSend.objects.filter(
        campaign_prospect_id=planned.campaign_prospect_id, email_step_id=planned.email_step_id,
        is_test=False, status="sent",
    ).exists()

    if request.method == "POST":
        action = request.POST.get("action")
        from .services.email_automation import (
            apply_manual_edit,
            promote_campaign_after_validation,
            send_planned_content_now,
            send_test_email,
            validate_planned_content,
        )
        from .services.predictneed_email import render_custom_planned_content

        if action == "preview":
            # Section 3/4 (correctif éditeur live preview) — reconstruit
            # EXACTEMENT la même enveloppe que celle qui serait produite par
            # apply_manual_edit() (même fonction de rendu), sans jamais
            # écrire en base : ni PlannedEmailContent, ni content_hash, ni
            # status, ni approved_at/approved_by. Aucun SMTP, aucune
            # programmation. Une frappe clavier ou un aperçu ne modifie
            # jamais rien — seul « Enregistrer la modification » (action=
            # edit) écrit.
            subject_preview = (request.POST.get("subject", "") or "").strip()
            body_preview = (request.POST.get("body_text", "") or "").strip()
            html_preview, text_preview = render_custom_planned_content(
                planned.campaign_prospect, planned.email_step, body_preview, request=request,
            )
            return JsonResponse({"subject": subject_preview, "html": html_preview, "text": text_preview})

        if action == "edit":
            subject = request.POST.get("subject", "")
            body_text = request.POST.get("body_text", "")
            if not subject.strip() or not body_text.strip():
                messages.error(request, "Le sujet et le texte ne peuvent pas être vides.")
            else:
                apply_manual_edit(planned, subject, body_text, request=request)
                messages.success(request, "Modification enregistrée pour ce prospect et cette étape uniquement. Toute programmation existante a été invalidée.")
        elif action == "test":
            test_recipient = settings.EMAIL_HOST_USER or "contact-predict@predictneed-ia.com"
            record = send_test_email(planned.campaign_prospect, planned, test_recipient, request=request)
            if record.status == "sent":
                messages.success(request, f"Test envoyé à {test_recipient} — aucun email envoyé au prospect.")
            else:
                messages.error(request, f"Échec de l'envoi de test : {record.error}")
        elif action == "programmer":
            ok, reason = validate_planned_content(planned, request.user)
            if ok:
                promote_campaign_after_validation(planned.campaign_prospect.campaign, request.user)
                messages.success(request, "Programmé. Aucun email envoyé immédiatement — le scheduler l'enverra à la date prévue.")
            else:
                REASON_LABELS = {
                    "stale": "contenu devenu obsolète depuis la préparation — relance « Préparer » ou modifie le texte.",
                    "no_email": "ce prospect n'a plus d'adresse exploitable.",
                    "prospect_not_eligible": "ce prospect n'est plus éligible (exclu, en opposition, ou séquence arrêtée).",
                }
                messages.error(request, f"Programmation refusée : {REASON_LABELS.get(reason, reason)}")
        elif action == "send_now":
            # Correctif UX (rattrapage manuel) — envoie RÉELLEMENT l'email
            # maintenant, sans attendre le prochain créneau automatique
            # (09:30-11:00, jours ouvrés). Réservé au contenu déjà
            # Programmé — voir email_automation.send_planned_content_now.
            if was_sent:
                messages.error(request, "Cet email a déjà été envoyé — aucune action possible.")
            else:
                result = send_planned_content_now(planned, request.user)
                if result.get("action") == "email":
                    messages.success(request, "Email envoyé maintenant au prospect.")
                else:
                    reason = result.get("action")
                    messages.error(request, f"Envoi refusé : {SEND_NOW_REASON_LABELS.get(reason, reason)}")
        return redirect("email_planning_content_detail", planned_id=planned.pk)

    return render(request, "prospects/email_planning_content_detail.html", {
        "planned": planned,
        "tested": bool(planned.tested_content_hash and planned.tested_content_hash == planned.content_hash),
        "display_status": _prepared_content_display_status(planned, was_sent),
        "can_send_now": planned.status == "validated" and not was_sent,
        "test_recipient": settings.EMAIL_HOST_USER or "contact-predict@predictneed-ia.com",
    })


@login_required
def email_planning_programmer_selection(request):
    """Section 7/8 (workflow final) — programme UNIQUEMENT les
    PlannedEmailContent explicitement cochés sur la page « Emails préparés »
    (jamais « tout » implicitement). N'APPELLE JAMAIS SMTP : autorise
    seulement le scheduler à envoyer plus tard, aux dates prévues — voir le
    test dédié `test_programmer_selection_never_sends_immediately`."""
    if request.method != "POST":
        return redirect("email_planning_prepared")

    from .services.email_automation import promote_campaign_after_validation, validate_planned_content

    planned_ids = request.POST.getlist("planned_ids")
    if not planned_ids:
        messages.error(request, "Aucun email sélectionné.")
        return redirect("email_planning_prepared")

    validated_count = 0
    refused_count = 0
    validated_campaign_ids = set()
    candidates = (
        PlannedEmailContent.objects.filter(pk__in=planned_ids, campaign_prospect__campaign__planning_managed=True)
        .exclude(campaign_prospect__campaign__status__in=["paused", "cancelled", "completed"])
        .select_related("campaign_prospect__campaign")
    )
    for planned in candidates:
        ok, reason = validate_planned_content(planned, request.user)
        if ok:
            validated_count += 1
            validated_campaign_ids.add(planned.campaign_prospect.campaign_id)
        else:
            refused_count += 1

    for campaign in Campaign.objects.filter(pk__in=validated_campaign_ids):
        promote_campaign_after_validation(campaign, request.user)

    messages.success(request, f"{validated_count} email(s) programmé(s). Aucun email envoyé immédiatement — ils partiront aux dates prévues.")
    if refused_count:
        messages.warning(request, f"{refused_count} email(s) écarté(s) (contenu périmé ou prospect non éligible).")
    return redirect("email_planning_prepared")


@login_required
def email_planning_send_selection_now(request):
    """Correctif UX (rattrapage manuel) — envoie RÉELLEMENT, tout de suite,
    UNIQUEMENT les PlannedEmailContent explicitement cochés qui sont déjà
    Programmé (status="validated") — jamais « tout » implicitement, et
    jamais un contenu non encore Programmé (voir send_planned_content_now).
    Contrairement à email_planning_programmer_selection, cette vue APPELLE
    RÉELLEMENT SMTP pour chaque ligne éligible — bouton distinct, réservé au
    rattrapage d'un créneau automatique manqué."""
    if request.method != "POST":
        return redirect("email_planning_prepared")

    from .services.email_automation import send_planned_content_now

    planned_ids = request.POST.getlist("planned_ids")
    if not planned_ids:
        messages.error(request, "Aucun email sélectionné.")
        return redirect("email_planning_prepared")

    sent_count = 0
    refused_count = 0
    candidates = (
        PlannedEmailContent.objects.filter(pk__in=planned_ids, campaign_prospect__campaign__planning_managed=True)
        .exclude(campaign_prospect__campaign__status__in=["paused", "cancelled", "completed"])
        .select_related("campaign_prospect__campaign")
    )
    for planned in candidates:
        result = send_planned_content_now(planned, request.user)
        if result.get("action") == "email":
            sent_count += 1
        else:
            refused_count += 1

    messages.success(request, f"{sent_count} email(s) envoyé(s) maintenant.")
    if refused_count:
        messages.warning(request, f"{refused_count} email(s) non envoyé(s) (pas Programmé, limite atteinte, ou plus éligible).")
    return redirect("email_planning_prepared")


@login_required
def email_planning_preview(request, cp_id):
    """Section G (Round D, verrous production) — l'aperçu affiche le
    CONTENU FIGÉ (PlannedEmailContent.subject/html_body/text_body) tel
    quel, JAMAIS un nouveau rendu live du renderer : c'est exactement ce
    que le test a reçu / ce que le scheduler enverra une fois validé.
    Liste les 4 étapes J0/J4/J8/J14 pour ce prospect, chacune avec son
    statut réel (préparé/testé/validé)."""
    cp = get_object_or_404(
        CampaignProspect.objects.select_related("campaign__sequence", "campaign__product", "prospect"),
        pk=cp_id, campaign__planning_managed=True,
    )
    steps = list(cp.campaign.sequence.steps.filter(active=True).order_by("order")) if cp.campaign.sequence else []
    planned_by_step = {
        p.email_step_id: p
        for p in PlannedEmailContent.objects.filter(campaign_prospect=cp, email_step__in=steps)
    }
    rows = []
    for step in steps:
        planned = planned_by_step.get(step.pk)
        if planned:
            mark_stale_if_changed(planned)
            planned.refresh_from_db()
        tested = bool(planned and planned.tested_content_hash and planned.tested_content_hash == planned.content_hash)
        rows.append({
            "step": step,
            "planned": planned,
            "tested": tested,
            "validated": bool(planned and planned.status == "validated"),
        })

    requested_step_id = request.GET.get("step")
    selected = None
    if requested_step_id:
        selected = next((r for r in rows if str(r["step"].pk) == requested_step_id), None)
    if selected is None:
        selected = next((r for r in rows if r["planned"]), rows[0] if rows else None)

    return render(request, "prospects/email_planning_preview.html", {
        "cp": cp, "rows": rows, "selected": selected,
    })


@login_required
def email_planning_prepare_week(request):
    """« Préparer » — correctif audit, sections 3 et 4.

    Une seule version candidate est rendue par étape (prepare_planned_content,
    jamais régénérée ensuite par le test ou la validation). Les dates sont
    de vrais créneaux lundi->vendredi : les relances dues sont placées en
    priorité, les nouveaux premiers contacts remplissent la capacité
    restante de chaque jour (new_contacts_per_day et daily_total_limit
    réellement respectés), tout dépassement est reporté au jour ouvré
    suivant. N'envoie rien."""
    if request.method != "POST":
        return redirect("email_planning")

    from .services.email_automation import build_week_plan, prepare_planned_content

    plan = build_week_plan(now=timezone.now())
    count = 0
    for scheduled_date, cp, step in plan:
        existing = PlannedEmailContent.objects.filter(campaign_prospect=cp, email_step=step).first()
        if existing and existing.status == "validated":
            continue  # déjà validé — ne pas repasser à "à valider" silencieusement
        prepare_planned_content(cp, step, scheduled_date)
        count += 1

    messages.success(request, f"{count} étape(s) préparée(s) et programmée(s) — à valider avant tout envoi.")
    return redirect("email_planning")


@login_required
def email_planning_send_tests(request):
    """Envoie chaque contenu non encore programmé en mode test UNIQUEMENT
    vers l'adresse de test contrôlée — EXACTEMENT le contenu déjà préparé
    (correctif audit, section 3 : jamais un nouveau rendu ; JAMAIS de pixel
    d'ouverture, section 5). Ne compte jamais comme premier contact
    commercial, ne crée aucun engagement commercial.

    Workflow final (section 5/9) : le test est désormais FACULTATIF — il ne
    conditionne plus « Programmer », c'est un contrôle supplémentaire pour
    qui souhaite voir le rendu dans sa vraie boîte mail. send_test_email()
    marque tout de même `tested_content_hash`/`test_sent_at` (purement
    informatif, badge « Testé »)."""
    if request.method != "POST":
        return redirect("email_planning")

    from .services.email_automation import send_test_email

    test_recipient = settings.EMAIL_HOST_USER or "contact-predict@predictneed-ia.com"
    # Round E, point 2 : une campagne paused/cancelled/completed ne doit
    # jamais recevoir de test via l'action globale « Envoyer les tests »,
    # exactement comme build_week_plan() ne la prépare plus.
    campaigns = Campaign.objects.filter(planning_managed=True).exclude(status__in=["paused", "cancelled", "completed"])
    sent = 0
    for planned in PlannedEmailContent.objects.filter(
        campaign_prospect__campaign__in=campaigns, status__in=["to_validate", "stale", "modified"],
    ).select_related("campaign_prospect", "email_step"):
        record = send_test_email(planned.campaign_prospect, planned, test_recipient, request=request)
        if record.status == "sent":
            sent += 1

    messages.success(request, f"{sent} e-mail(s) de test envoyé(s) à {test_recipient} — contenu identique au contenu préparé. Aucun envoi commercial.")
    return redirect("email_planning")


@login_required
def email_planning_validate_and_schedule(request):
    """« Programmer tout » — trace d'approbation explicite (qui/quand/quel
    contenu) sur EXACTEMENT le contenu déjà préparé, jamais un nouveau
    rendu (correctif audit, section 3). Si les données source du prospect
    ont changé depuis « Préparer », le contenu passe `stale` et n'est pas
    programmé — il faut relancer « Préparer » ou le modifier à la main.

    Workflow final (section 6/9) : le test n'est plus une condition —
    `validate_planned_content()` n'exige plus `tested_content_hash ==
    content_hash`. Cette action N'ENVOIE JAMAIS RIEN elle-même : elle
    autorise seulement le scheduler à envoyer plus tard, aux dates prévues.

    Section A (Round D, verrous production) : au-delà du seul
    PlannedEmailContent, cette action doit rendre la CAMPAGNE réellement
    envoyable — sans quoi une campagne adoptée (remise à draft/
    validated_at=None) reste bloquée pour toujours, alors même que son
    contenu est validé. Chaque campagne ayant reçu au moins une validation
    réussie dans CE lot est promue par promote_campaign_after_validation()
    (nouveau validated_at/validated_by, jamais l'ancien). Une campagne sans
    aucun contenu validé dans ce lot reste inchangée, donc non-sendable."""
    if request.method != "POST":
        return redirect("email_planning")

    from .services.email_automation import promote_campaign_after_validation, validate_planned_content

    # Round E, point 2 : une campagne paused/cancelled/completed ne doit
    # jamais être (re)validée/reprogrammée par l'action globale — une
    # campagne en pause ne redevient jamais "ready" par validation
    # implicite, et cancelled/completed ne sont jamais réactivées par ce
    # workflow.
    campaigns = Campaign.objects.filter(planning_managed=True).exclude(status__in=["paused", "cancelled", "completed"])
    validated_count = 0
    refused_count = 0
    validated_campaign_ids = set()
    for planned in PlannedEmailContent.objects.filter(
        campaign_prospect__campaign__in=campaigns, status__in=["to_validate", "stale", "modified"],
    ).select_related("campaign_prospect__campaign", "email_step"):
        ok, reason = validate_planned_content(planned, request.user)
        if ok:
            validated_count += 1
            validated_campaign_ids.add(planned.campaign_prospect.campaign_id)
        else:
            refused_count += 1

    for campaign in Campaign.objects.filter(pk__in=validated_campaign_ids):
        promote_campaign_after_validation(campaign, request.user)

    messages.success(request, f"{validated_count} étape(s) programmée(s) par {request.user.username}. Aucun email envoyé immédiatement — le scheduler enverra aux dates prévues.")
    if validated_campaign_ids:
        messages.success(request, f"{len(validated_campaign_ids)} campagne(s) désormais envoyable(s) par le scheduler.")
    if refused_count:
        messages.warning(request, f"{refused_count} étape(s) écartée(s) (contenu périmé ou prospect non éligible) — relance « Préparer » ou vérifie le prospect.")
    return redirect("email_planning")


@login_required
def email_planning_pause(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk, planning_managed=True)
    if request.method == "POST":
        campaign.status = "paused"
        campaign.save(update_fields=["status"])
        messages.success(request, f"Campagne « {campaign.name} » mise en pause.")
    return redirect("email_planning")


@login_required
def email_planning_stop(request, cp_id):
    cp = get_object_or_404(CampaignProspect, pk=cp_id, campaign__planning_managed=True)
    if request.method == "POST":
        cp.status = "excluded"
        cp.excluded_reason = f"Arrêt manuel par {request.user.username} depuis le Planning e-mail."
        cp.save(update_fields=["status", "excluded_reason"])
        messages.success(request, f"Séquence arrêtée pour {cp.prospect.name}.")
    return redirect("email_planning")
