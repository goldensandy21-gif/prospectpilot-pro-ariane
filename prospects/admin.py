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
