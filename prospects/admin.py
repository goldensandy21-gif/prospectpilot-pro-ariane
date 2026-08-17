from django.contrib import admin
from .models import (
    Prospect,
    PublicEmail,
    PublicPhone,
    PublicContactForm,
    PublicSocialLink,
    CrawlRun,
    PageAudit,
    SiteAuditSummary,
    BacklinkSnapshot,
    ContactLog,
    Suppression,
    SearchConsoleConnection,
    SearchConsoleMetric,
    Report,
    EmailTemplate,
    EmailSend,
    SearchDecision,
    EnrichmentSource,
    EnrichmentRun,
    ProspectEvidence,
    ContactPerson,
    ProductProfile,
    ICPProfile,
    CompanySearchRun,
    SearchCandidate,
    SearchPreset,
    ProspectTechnology,
    ProspectSignal,
    Competitor,
    CompetitorDetection,
    AgentBrief,
    Campaign,
    CampaignProspect,
    EmailSequence,
    EmailStep,
    EmailVariant,
    EngagementEvent,
    ConversionEvent,
    RevenueAttribution,
    EmailComplianceProfile,
)


class PublicEmailInline(admin.TabularInline):
    model = PublicEmail
    extra = 0
    fields = (
        "email",
        "email_type",
        "source_type",
        "source_url",
        "is_primary",
        "is_active",
        "found_at",
    )
    readonly_fields = ("found_at",)


class PublicPhoneInline(admin.TabularInline):
    model = PublicPhone
    extra = 0
    fields = (
        "phone",
        "source_type",
        "source_url",
        "is_primary",
        "is_active",
        "found_at",
    )
    readonly_fields = ("found_at",)


class PublicContactFormInline(admin.TabularInline):
    model = PublicContactForm
    extra = 0
    fields = (
        "page_url",
        "form_action",
        "form_method",
        "has_email_field",
        "has_phone_field",
        "is_primary",
        "is_active",
        "found_at",
    )
    readonly_fields = ("found_at",)


class PublicSocialLinkInline(admin.TabularInline):
    model = PublicSocialLink
    extra = 0
    fields = (
        "platform",
        "url",
        "source_url",
        "is_active",
        "found_at",
    )
    readonly_fields = ("found_at",)


class ContactPersonInline(admin.TabularInline):
    model = ContactPerson
    extra = 0
    fields = ("full_name", "job_title", "email", "phone", "profile_url", "confidence_score", "verification_status", "source_url")


class ProspectEvidenceInline(admin.TabularInline):
    model = ProspectEvidence
    extra = 0
    fields = ("field_name", "value", "source", "confidence_score", "verification_status", "source_url", "last_checked_at")
    readonly_fields = ("last_checked_at",)


@admin.register(Prospect)
class ProspectAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "naf_code",
        "city",
        "public_email",
        "priority_score",
        "status",
        "prospecting_allowed",
        "updated_at",
    )
    list_filter = (
        "status",
        "naf_code",
        "department",
        "prospecting_allowed",
        "diffusion_partial",
    )
    search_fields = (
        "name",
        "legal_name",
        "siren",
        "siret",
        "website",
        "public_email",
        "city",
    )
    inlines = [
        PublicEmailInline,
        PublicPhoneInline,
        PublicContactFormInline,
        PublicSocialLinkInline,
        ContactPersonInline,
        ProspectEvidenceInline,
    ]


@admin.register(PublicEmail)
class PublicEmailAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "prospect",
        "email_type",
        "source_type",
        "is_primary",
        "is_active",
        "found_at",
    )
    list_filter = (
        "email_type",
        "source_type",
        "is_primary",
        "is_active",
    )
    search_fields = (
        "email",
        "prospect__name",
        "prospect__website",
        "source_url",
    )
    readonly_fields = ("found_at",)


@admin.register(PublicPhone)
class PublicPhoneAdmin(admin.ModelAdmin):
    list_display = (
        "phone",
        "prospect",
        "source_type",
        "is_primary",
        "is_active",
        "found_at",
    )
    list_filter = ("source_type", "is_primary", "is_active")
    search_fields = ("phone", "prospect__name", "prospect__website", "source_url")
    readonly_fields = ("found_at",)


@admin.register(PublicContactForm)
class PublicContactFormAdmin(admin.ModelAdmin):
    list_display = (
        "page_url",
        "prospect",
        "has_email_field",
        "has_phone_field",
        "is_primary",
        "is_active",
        "found_at",
    )
    list_filter = ("has_email_field", "has_phone_field", "is_primary", "is_active")
    search_fields = ("page_url", "form_action", "prospect__name", "prospect__website")
    readonly_fields = ("found_at",)


@admin.register(PublicSocialLink)
class PublicSocialLinkAdmin(admin.ModelAdmin):
    list_display = ("platform", "url", "prospect", "is_active", "found_at")
    list_filter = ("platform", "is_active")
    search_fields = ("url", "source_url", "prospect__name", "prospect__website")
    readonly_fields = ("found_at",)


admin.site.register(CrawlRun)
admin.site.register(PageAudit)
admin.site.register(SiteAuditSummary)
admin.site.register(BacklinkSnapshot)
admin.site.register(ContactLog)
admin.site.register(Suppression)
admin.site.register(SearchConsoleConnection)
admin.site.register(SearchConsoleMetric)
admin.site.register(Report)
admin.site.register(EmailTemplate)
admin.site.register(EmailSend)
admin.site.register(SearchDecision)


@admin.register(EnrichmentSource)
class EnrichmentSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "source_type", "enabled", "requires_api_key", "api_key_env")
    list_filter = ("source_type", "enabled", "requires_api_key")
    search_fields = ("name", "key", "legal_notes")


@admin.register(EnrichmentRun)
class EnrichmentRunAdmin(admin.ModelAdmin):
    list_display = ("mode", "prospect", "status", "owner", "created_at", "finished_at")
    list_filter = ("mode", "status")
    search_fields = ("prospect__name", "error")


@admin.register(ProspectEvidence)
class ProspectEvidenceAdmin(admin.ModelAdmin):
    list_display = ("prospect", "field_name", "value", "source", "confidence_score", "verification_status", "last_checked_at")
    list_filter = ("field_name", "verification_status", "source")
    search_fields = ("prospect__name", "value", "source_url")


@admin.register(ContactPerson)
class ContactPersonAdmin(admin.ModelAdmin):
    list_display = ("full_name", "prospect", "job_title", "email", "phone", "confidence_score", "verification_status")
    list_filter = ("verification_status", "is_active", "source")
    search_fields = ("full_name", "email", "phone", "prospect__name")


# ---------------------------------------------------------------------------
# ETAPE 27 — Administration du moteur d'acquisition PredictNeed
# ---------------------------------------------------------------------------

@admin.register(ProductProfile)
class ProductProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "active", "monthly_price", "currency", "sender_email", "updated_at")
    list_filter = ("active",)
    search_fields = ("name", "slug", "sender_email")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(ICPProfile)
class ICPProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "product", "active", "employee_min", "employee_max", "minimum_outbound_score", "updated_at")
    list_filter = ("active", "product")
    search_fields = ("name", "description")


@admin.register(CompanySearchRun)
class CompanySearchRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "mode", "product", "icp", "status", "registry_count", "preselected_count",
        "with_site_count", "enriched_count", "with_email_count",
        "qualified_a_count", "qualified_b_count", "qualified_c_count",
        "user", "created_at",
    )
    list_filter = ("mode", "status", "product", "icp")
    search_fields = ("campaign_name",)
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")


@admin.register(SearchCandidate)
class SearchCandidateAdmin(admin.ModelAdmin):
    list_display = ("name", "siren", "search_run", "status", "pre_score", "final_score", "grade", "outbound_eligible")
    list_filter = ("status", "grade", "outbound_eligible")
    search_fields = ("name", "siren", "siret", "contact_email")


@admin.register(SearchPreset)
class SearchPresetAdmin(admin.ModelAdmin):
    list_display = ("name", "product", "icp", "active", "last_run_at")
    list_filter = ("active", "product")
    search_fields = ("name",)


@admin.register(ProspectTechnology)
class ProspectTechnologyAdmin(admin.ModelAdmin):
    list_display = ("technology", "category", "prospect", "confidence", "is_active", "detected_at")
    list_filter = ("category", "is_active")
    search_fields = ("technology", "prospect__name")


@admin.register(ProspectSignal)
class ProspectSignalAdmin(admin.ModelAdmin):
    list_display = ("label", "category", "prospect", "positive", "score_impact", "confidence", "detected_at")
    list_filter = ("category", "positive")
    search_fields = ("label", "signal_type", "prospect__name")


@admin.register(Competitor)
class CompetitorAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "scoring_penalty", "scoring_bonus", "active")
    list_filter = ("category", "active")
    search_fields = ("name",)


@admin.register(CompetitorDetection)
class CompetitorDetectionAdmin(admin.ModelAdmin):
    list_display = ("competitor", "prospect", "confidence", "detected_at")
    list_filter = ("competitor",)
    search_fields = ("prospect__name",)


@admin.register(AgentBrief)
class AgentBriefAdmin(admin.ModelAdmin):
    list_display = ("prospect", "product", "score", "recommended_contact", "generated_at")
    list_filter = ("product", "icp")
    search_fields = ("prospect__name", "why_this_company")
    readonly_fields = ("generated_at", "updated_at")


class CampaignProspectInline(admin.TabularInline):
    model = CampaignProspect
    extra = 0
    fields = ("prospect", "acquisition_score_snapshot", "grade", "status", "contacted_at", "last_engagement_at")
    readonly_fields = ("acquisition_score_snapshot", "grade")


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "product", "icp", "status", "score_threshold", "daily_send_limit", "total_limit", "created_by", "created_at")
    list_filter = ("status", "product", "icp")
    search_fields = ("name", "objective")
    readonly_fields = ("created_at", "updated_at", "validated_at")
    inlines = [CampaignProspectInline]


@admin.register(CampaignProspect)
class CampaignProspectAdmin(admin.ModelAdmin):
    list_display = ("prospect", "campaign", "grade", "acquisition_score_snapshot", "status", "contacted_at", "converted_at")
    list_filter = ("status", "grade", "campaign")
    search_fields = ("prospect__name",)
    readonly_fields = ("tracking_token", "created_at", "updated_at")


class EmailStepInline(admin.TabularInline):
    model = EmailStep
    extra = 0


@admin.register(EmailSequence)
class EmailSequenceAdmin(admin.ModelAdmin):
    list_display = ("name", "product", "icp", "active", "stop_on_reply", "stop_on_conversion")
    list_filter = ("active", "product")
    search_fields = ("name",)
    inlines = [EmailStepInline]


class EmailVariantInline(admin.TabularInline):
    model = EmailVariant
    extra = 0


@admin.register(EmailStep)
class EmailStepAdmin(admin.ModelAdmin):
    list_display = ("sequence", "order", "delay_days", "name", "active")
    list_filter = ("sequence",)
    inlines = [EmailVariantInline]


@admin.register(EmailVariant)
class EmailVariantAdmin(admin.ModelAdmin):
    list_display = ("name", "step", "cta_type", "active", "weight")
    list_filter = ("cta_type", "active")
    search_fields = ("name", "subject_template")


@admin.register(EngagementEvent)
class EngagementEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "source", "prospect", "campaign", "occurred_at")
    list_filter = ("event_type", "source", "campaign")
    search_fields = ("prospect__name", "idempotency_key")
    readonly_fields = ("created_at",)


@admin.register(ConversionEvent)
class ConversionEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "prospect", "campaign", "occurred_at")
    list_filter = ("event_type", "campaign")
    search_fields = ("prospect__name", "external_reference", "idempotency_key")


@admin.register(RevenueAttribution)
class RevenueAttributionAdmin(admin.ModelAdmin):
    list_display = ("prospect", "campaign", "mrr", "currency", "subscription_value", "attributed_at")
    list_filter = ("campaign", "currency")
    search_fields = ("prospect__name",)


@admin.register(EmailComplianceProfile)
class EmailComplianceProfileAdmin(admin.ModelAdmin):
    list_display = ("product", "organization_name", "contact_email", "compliance_ready_display", "active")
    search_fields = ("organization_name", "legal_name", "contact_email")

    @admin.display(description="Conformité prête")
    def compliance_ready_display(self, obj):
        return obj.compliance_ready
