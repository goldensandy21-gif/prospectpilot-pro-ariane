from django.db import models
from django.contrib.auth.models import User
import uuid

class Prospect(models.Model):
    STATUS_CHOICES = [
        ("new", "Nouveau"),
        ("qualified", "Qualifié"),
        ("contacted", "Contacté"),
        ("replied", "A répondu"),
        ("meeting", "Rendez-vous"),
        ("proposal", "Proposition envoyée"),
        ("won", "Client"),
        ("lost", "Non retenu"),
        ("do_not_contact", "Ne plus contacter"),
    ]
    name = models.CharField(max_length=255)
    legal_name = models.CharField(max_length=255, blank=True)
    sector = models.CharField(max_length=180, blank=True)
    naf_code = models.CharField(max_length=10, blank=True, db_index=True)
    department = models.CharField(max_length=5, blank=True, db_index=True)
    city = models.CharField(max_length=120, blank=True, db_index=True)
    country = models.CharField(max_length=120, blank=True, default="France")
    postal_code = models.CharField(max_length=10, blank=True)
    address = models.CharField(max_length=255, blank=True)
    siren = models.CharField(max_length=9, blank=True, db_index=True)
    siret = models.CharField(max_length=14, blank=True, unique=True, null=True)
    website = models.URLField(blank=True)
    website_confidence = models.PositiveSmallIntegerField(default=0)
    public_email = models.EmailField(blank=True)
    public_phone = models.CharField(max_length=40, blank=True)
    employee_band = models.CharField(max_length=80, blank=True)
    creation_date = models.DateField(null=True, blank=True)

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="new", db_index=True)
    source = models.CharField(max_length=120, default="api_recherche_entreprises")
    source_url = models.URLField(blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    prospecting_allowed = models.BooleanField(default=True)
    diffusion_partial = models.BooleanField(default=False)

    technical_score = models.PositiveSmallIntegerField(default=0)
    commercial_score = models.PositiveSmallIntegerField(default=0)
    fit_score = models.PositiveSmallIntegerField(default=0)
    priority_score = models.PositiveSmallIntegerField(default=0)
    score_reasons = models.JSONField(default=list, blank=True)

    owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True)
    next_action_at = models.DateTimeField(null=True, blank=True)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    last_replied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    unsubscribe_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    rejected = models.BooleanField(default=False)
    rejection_reason = models.CharField(max_length=255, blank=True)

    # --- PredictNeed IA acquisition scoring (ETAPE 13) ---------------------
    # Ne remplace pas les scores historiques ci-dessus : score distinct, additif.
    PREDICTNEED_GRADES = [("A", "A"), ("B", "B"), ("C", "C"), ("D", "D")]
    predictneed_product = models.ForeignKey(
        "prospects.ProductProfile", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="prospects",
    )
    predictneed_icp = models.ForeignKey(
        "prospects.ICPProfile", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="matched_prospects",
    )
    icp_fit_score = models.PositiveSmallIntegerField(default=0)
    need_score = models.PositiveSmallIntegerField(default=0)
    acquisition_maturity_score = models.PositiveSmallIntegerField(default=0)
    contactability_score = models.PositiveSmallIntegerField(default=0)
    timing_score = models.PositiveSmallIntegerField(default=0)
    predictneed_acquisition_score = models.PositiveSmallIntegerField(default=0, db_index=True)
    predictneed_grade = models.CharField(max_length=1, choices=PREDICTNEED_GRADES, blank=True, db_index=True)
    predictneed_score_reasons = models.JSONField(default=list, blank=True)

    # Mission 6 — FIT/INTENT/ENGAGEMENT. Ne remplacent pas predictneed_acquisition_score
    # (qui reste le score de PRIORITÉ affiché) : ce sont ses composantes explicables.
    # FIT réutilise icp_fit_score ci-dessus (pas de nouveau champ). INTENT et
    # ENGAGEMENT sont calculés par services/intent_scoring.py et
    # services/engagement_scoring.py, jamais recalculés ailleurs.
    intent_score = models.PositiveSmallIntegerField(default=0, db_index=True)
    intent_score_reasons = models.JSONField(default=list, blank=True)
    engagement_score = models.PositiveSmallIntegerField(default=0, db_index=True)
    engagement_score_reasons = models.JSONField(default=list, blank=True)
    scores_computed_at = models.DateTimeField(null=True, blank=True)

    outbound_eligible = models.BooleanField(default=False)
    outbound_ineligible_reason = models.CharField(max_length=255, blank=True)
    predictneed_excluded = models.BooleanField(default=False)
    predictneed_exclusion_reason = models.CharField(max_length=255, blank=True)

    # Mission 5, section 3 — sélection humaine explicite. Distingue les
    # prospects volontairement retenus pour la prospection (visibles dans la
    # liste principale « Prospects ») des simples candidats techniques générés
    # par le pipeline d'acquisition (données conservées, mais pas encombrants
    # tant qu'ils n'ont pas été choisis). Voir la migration de backfill : les
    # prospects historiques (source != acquisition_pipeline_predictneed) sont
    # considérés déjà sélectionnés, les candidats techniques ne le sont pas.
    selected_for_prospecting = models.BooleanField(default=False, db_index=True)
    selected_at = models.DateTimeField(null=True, blank=True)

    PREDICTNEED_STAGES = [
        ("identified", "Identifié"),
        ("enriched", "Enrichi"),
        ("qualified", "Qualifié"),
        ("ready_to_contact", "Prêt à contacter"),
        ("contacted", "Contacté"),
        ("engaged", "Engagé"),
        ("signed_up", "Inscrit"),
        ("activated", "Activé"),
        ("paying", "Client payant"),
        ("nurture", "Nurture"),
        ("lost", "Perdu"),
        ("do_not_contact", "Ne plus contacter"),
    ]
    predictneed_stage = models.CharField(max_length=20, choices=PREDICTNEED_STAGES, blank=True, db_index=True)

    class Meta:
        ordering = ["-priority_score", "-updated_at"]

    def __str__(self):
        return self.name
    
class PublicEmail(models.Model):
    EMAIL_TYPES = [
        ("generic", "Générique"),
        ("personal", "Nominatif"),
        ("unknown", "Inconnu"),
    ]

    SOURCES = [
        ("website", "Site web"),
        ("contact_page", "Page contact"),
        ("legal_notice", "Mentions légales"),
        ("manual", "Ajout manuel"),
        ("other", "Autre"),
    ]

    prospect = models.ForeignKey(
        Prospect,
        related_name="public_emails",
        on_delete=models.CASCADE
    )
    email = models.EmailField(db_index=True)
    email_type = models.CharField(
        max_length=20,
        choices=EMAIL_TYPES,
        default="unknown",
        db_index=True
    )
    confidence_score = models.PositiveSmallIntegerField(default=50)
    verification_status = models.CharField(
        max_length=30,
        default="format_valid",
        db_index=True,
    )
    source_name = models.CharField(max_length=120, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    source_type = models.CharField(
        max_length=30,
        choices=SOURCES,
        default="website"
    )
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    discovery_method = models.CharField(max_length=80, default="public_page")
    notes = models.TextField(blank=True)
    found_at = models.DateTimeField(auto_now_add=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-is_primary", "email"]
        constraints = [
            models.UniqueConstraint(
                fields=["prospect", "email"],
                name="unique_public_email_per_prospect"
            )
        ]

    def __str__(self):
        return self.email


class PublicPhone(models.Model):
    SOURCES = [
        ("website", "Site web"),
        ("contact_page", "Page contact"),
        ("legal_notice", "Mentions légales"),
        ("manual", "Ajout manuel"),
        ("other", "Autre"),
    ]

    prospect = models.ForeignKey(
        Prospect,
        related_name="public_phones",
        on_delete=models.CASCADE,
    )
    phone = models.CharField(max_length=40, db_index=True)
    confidence_score = models.PositiveSmallIntegerField(default=50)
    verification_status = models.CharField(
        max_length=30,
        default="format_valid",
        db_index=True,
    )
    source_name = models.CharField(max_length=120, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    source_type = models.CharField(
        max_length=30,
        choices=SOURCES,
        default="website",
    )
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    discovery_method = models.CharField(max_length=80, default="public_page")
    notes = models.TextField(blank=True)
    found_at = models.DateTimeField(auto_now_add=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-is_primary", "phone"]
        constraints = [
            models.UniqueConstraint(
                fields=["prospect", "phone"],
                name="unique_public_phone_per_prospect",
            )
        ]

    def __str__(self):
        return self.phone


class PublicContactForm(models.Model):
    prospect = models.ForeignKey(
        Prospect,
        related_name="contact_forms",
        on_delete=models.CASCADE,
    )
    page_url = models.URLField(max_length=1000)
    form_action = models.CharField(max_length=1000, blank=True)
    form_method = models.CharField(max_length=12, blank=True)
    has_email_field = models.BooleanField(default=False)
    has_phone_field = models.BooleanField(default=False)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    discovery_method = models.CharField(max_length=80, default="public_page")
    found_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_primary", "page_url"]
        constraints = [
            models.UniqueConstraint(
                fields=["prospect", "page_url", "form_action"],
                name="unique_contact_form_per_prospect",
            )
        ]

    def __str__(self):
        return self.page_url


class PublicSocialLink(models.Model):
    PLATFORMS = [
        ("linkedin", "LinkedIn"),
        ("facebook", "Facebook"),
        ("instagram", "Instagram"),
        ("x", "X / Twitter"),
        ("other", "Autre"),
    ]

    prospect = models.ForeignKey(
        Prospect,
        related_name="social_links",
        on_delete=models.CASCADE,
    )
    platform = models.CharField(max_length=30, choices=PLATFORMS, default="other")
    url = models.URLField(max_length=1000)
    source_url = models.URLField(max_length=1000, blank=True)
    is_active = models.BooleanField(default=True)
    discovery_method = models.CharField(max_length=80, default="public_page")
    found_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["platform", "url"]
        constraints = [
            models.UniqueConstraint(
                fields=["prospect", "url"],
                name="unique_social_link_per_prospect",
            )
        ]

    def __str__(self):
        return self.url



class EnrichmentSource(models.Model):
    SOURCE_TYPES = [
        ("public_registry", "Registre public"),
        ("company_website", "Site officiel"),
        ("public_page", "Page publique"),
        ("professional_directory", "Annuaire professionnel"),
        ("b2b_api", "API B2B"),
        ("user_import", "Import utilisateur"),
        ("open_web", "Web ouvert"),
        ("first_party", "Donnée propriétaire"),
    ]

    key = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=160)
    source_type = models.CharField(max_length=40, choices=SOURCE_TYPES, db_index=True)
    enabled = models.BooleanField(default=True)
    requires_api_key = models.BooleanField(default=False)
    api_key_env = models.CharField(max_length=120, blank=True)
    base_url = models.URLField(blank=True)
    legal_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_type", "name"]

    def __str__(self):
        return self.name


class EnrichmentRun(models.Model):
    STATUS = [
        ("queued", "En attente"),
        ("running", "En cours"),
        ("done", "Terminé"),
        ("failed", "Échec"),
    ]
    MODES = [
        ("prospect", "Fiche prospect"),
        ("search", "Recherche"),
        ("import", "Import"),
    ]

    prospect = models.ForeignKey(
        Prospect,
        related_name="enrichment_runs",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, choices=STATUS, default="queued", db_index=True)
    mode = models.CharField(max_length=20, choices=MODES, default="prospect")
    query = models.JSONField(default=dict, blank=True)
    sources_requested = models.JSONField(default=list, blank=True)
    sources_completed = models.JSONField(default=list, blank=True)
    totals = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_mode_display()} - {self.get_status_display()}"


class ProspectEvidence(models.Model):
    VALUE_TYPES = [
        ("company", "Entreprise"),
        ("website", "Site web"),
        ("address", "Adresse"),
        ("email", "Email"),
        ("phone", "Téléphone"),
        ("person", "Contact"),
        ("profile", "Profil public"),
        ("other", "Autre"),
    ]
    VERIFICATION = [
        ("unverified", "Non vérifié"),
        ("format_valid", "Format valide"),
        ("deliverability_unknown", "Délivrabilité inconnue"),
        ("verified", "Vérifié"),
        ("invalid", "Invalide"),
        ("blocked", "Bloqué"),
        ("conflict", "Conflit"),
    ]

    prospect = models.ForeignKey(
        Prospect,
        related_name="evidence_items",
        on_delete=models.CASCADE,
    )
    source = models.ForeignKey(
        EnrichmentSource,
        related_name="evidence_items",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    field_name = models.CharField(max_length=80, db_index=True)
    value = models.TextField()
    normalized_value = models.CharField(max_length=500, blank=True, db_index=True)
    value_type = models.CharField(max_length=30, choices=VALUE_TYPES, default="other")
    confidence_score = models.PositiveSmallIntegerField(default=50, db_index=True)
    verification_status = models.CharField(max_length=30, choices=VERIFICATION, default="unverified", db_index=True)
    source_url = models.URLField(max_length=1000, blank=True)
    is_current = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    collected_at = models.DateTimeField(auto_now_add=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["field_name", "-confidence_score", "-last_checked_at", "-collected_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["prospect", "field_name", "normalized_value"],
                name="unique_evidence_value_per_prospect",
            )
        ]

    def __str__(self):
        return f"{self.prospect} - {self.field_name}: {self.value}"


class ContactPerson(models.Model):
    VERIFICATION = ProspectEvidence.VERIFICATION

    prospect = models.ForeignKey(
        Prospect,
        related_name="contact_people",
        on_delete=models.CASCADE,
    )
    source = models.ForeignKey(
        EnrichmentSource,
        related_name="contact_people",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    first_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)
    full_name = models.CharField(max_length=255, blank=True, db_index=True)
    job_title = models.CharField(max_length=180, blank=True)
    email = models.EmailField(blank=True, db_index=True)
    phone = models.CharField(max_length=40, blank=True)
    profile_url = models.URLField(max_length=1000, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    confidence_score = models.PositiveSmallIntegerField(default=50, db_index=True)
    verification_status = models.CharField(max_length=30, choices=VERIFICATION, default="unverified", db_index=True)
    is_active = models.BooleanField(default=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-confidence_score", "full_name", "email"]

    def __str__(self):
        return self.full_name or self.email or f"Contact {self.pk}"


class CrawlRun(models.Model):
    STATUS = [("queued","En attente"),("running","En cours"),("done","Terminé"),("failed","Échec")]
    prospect = models.ForeignKey(Prospect, related_name="crawl_runs", on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS, default="queued")
    start_url = models.URLField()
    pages_requested = models.PositiveSmallIntegerField(default=12)
    pages_crawled = models.PositiveSmallIntegerField(default=0)
    robots_allowed = models.BooleanField(default=True)
    robots_url = models.URLField(blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class PageAudit(models.Model):
    crawl_run = models.ForeignKey(CrawlRun, related_name="pages", on_delete=models.CASCADE)
    prospect = models.ForeignKey(Prospect, related_name="page_audits", on_delete=models.CASCADE)
    url = models.URLField(max_length=1000)
    depth = models.PositiveSmallIntegerField(default=0)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_ms = models.PositiveIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    title = models.CharField(max_length=300, blank=True)
    meta_description = models.TextField(blank=True)
    canonical = models.URLField(max_length=1000, blank=True)
    h1_count = models.PositiveIntegerField(default=0)
    word_count = models.PositiveIntegerField(default=0)
    images_count = models.PositiveIntegerField(default=0)
    images_without_alt = models.PositiveIntegerField(default=0)
    internal_links = models.PositiveIntegerField(default=0)
    external_links = models.PositiveIntegerField(default=0)
    broken_links = models.PositiveIntegerField(default=0)
    has_https = models.BooleanField(default=False)
    has_viewport = models.BooleanField(default=False)
    has_contact_form = models.BooleanField(default=False)
    has_booking = models.BooleanField(default=False)
    has_phone = models.BooleanField(default=False)
    has_email = models.BooleanField(default=False)
    has_cta = models.BooleanField(default=False)
    technologies = models.JSONField(default=list, blank=True)
    issues = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class SiteAuditSummary(models.Model):
    prospect = models.ForeignKey(Prospect, related_name="audit_summaries", on_delete=models.CASCADE)
    crawl_run = models.OneToOneField(CrawlRun, related_name="summary", on_delete=models.CASCADE)
    pages_crawled = models.PositiveSmallIntegerField(default=0)
    broken_links = models.PositiveIntegerField(default=0)
    duplicate_titles = models.PositiveIntegerField(default=0)
    missing_titles = models.PositiveIntegerField(default=0)
    missing_descriptions = models.PositiveIntegerField(default=0)
    missing_h1 = models.PositiveIntegerField(default=0)
    images_without_alt = models.PositiveIntegerField(default=0)
    average_response_ms = models.PositiveIntegerField(default=0)
    performance_score = models.PositiveSmallIntegerField(null=True, blank=True)
    seo_score = models.PositiveSmallIntegerField(null=True, blank=True)
    accessibility_score = models.PositiveSmallIntegerField(null=True, blank=True)
    technologies = models.JSONField(default=list, blank=True)
    issues = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class BacklinkSnapshot(models.Model):
    prospect = models.ForeignKey(Prospect, related_name="backlink_snapshots", on_delete=models.CASCADE)
    source = models.CharField(max_length=80, default="common_crawl")
    target_domain = models.CharField(max_length=255)
    referring_domains = models.JSONField(default=list, blank=True)
    discovered_urls = models.JSONField(default=list, blank=True)
    capture_count = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class ContactLog(models.Model):
    CHANNELS = [("email","E-mail"),("phone","Téléphone"),("linkedin","LinkedIn"),("form","Formulaire"),("other","Autre")]
    OUTCOMES = [
        ("sent","Envoyé"),("opened","Ouvert"),("replied","Réponse"),("meeting","Rendez-vous"),
        ("proposal","Proposition"),("won","Client"),("lost","Refus"),("optout","Opposition"),
    ]
    prospect = models.ForeignKey(Prospect, related_name="contact_logs", on_delete=models.CASCADE)
    channel = models.CharField(max_length=20, choices=CHANNELS, default="email")
    subject = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    outcome = models.CharField(max_length=30, choices=OUTCOMES, default="sent")
    response_text = models.TextField(blank=True)
    follow_up_at = models.DateTimeField(null=True, blank=True)
    contacted_at = models.DateTimeField(auto_now_add=True)

class Suppression(models.Model):
    email = models.EmailField(blank=True)
    domain = models.CharField(max_length=255, blank=True)
    prospect = models.OneToOneField(Prospect, null=True, blank=True, on_delete=models.CASCADE)
    reason = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["email","domain"], name="unique_suppression_target")
        ]

class SearchConsoleConnection(models.Model):
    user = models.OneToOneField(User, related_name="search_console_connection", on_delete=models.CASCADE)
    token = models.TextField()
    refresh_token = models.TextField(blank=True)
    token_uri = models.URLField(default="https://oauth2.googleapis.com/token")
    scopes = models.JSONField(default=list)
    expiry = models.DateTimeField(null=True, blank=True)
    selected_property = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class SearchConsoleMetric(models.Model):
    connection = models.ForeignKey(SearchConsoleConnection, related_name="metrics", on_delete=models.CASCADE)
    property_url = models.CharField(max_length=500)
    date = models.DateField()
    query = models.CharField(max_length=500, blank=True)
    page = models.URLField(max_length=1000, blank=True)
    clicks = models.FloatField(default=0)
    impressions = models.FloatField(default=0)
    ctr = models.FloatField(default=0)
    position = models.FloatField(default=0)

class Report(models.Model):
    FORMAT = [("pdf","PDF"),("xlsx","Excel"),("csv","CSV")]
    prospect = models.ForeignKey(Prospect, related_name="reports", on_delete=models.CASCADE)
    format = models.CharField(max_length=10, choices=FORMAT)
    file = models.FileField(upload_to="reports/%Y/%m/")
    created_at = models.DateTimeField(auto_now_add=True)


class EmailTemplate(models.Model):
    name = models.CharField(max_length=120, unique=True)
    subject = models.CharField(max_length=255)
    preheader = models.CharField(max_length=255, blank=True)
    html_body = models.TextField()
    text_body = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    # ETAPE 31 — les anciens modèles ProspectPilot restent utilisables mais ne sont
    # plus proposés par défaut aux campagnes PredictNeed (is_legacy=True par migration).
    product = models.ForeignKey(
        "prospects.ProductProfile", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="email_templates",
    )
    is_legacy = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class EmailSend(models.Model):
    STATUS = [
        ("draft", "Brouillon"),
        ("queued", "En file"),
        ("sent", "Envoyé"),
        ("failed", "Échec"),
        ("blocked", "Bloqué"),
        ("bounced", "Rejeté (bounce)"),
        ("suppressed", "Supprimé (opposition)"),
    ]
    prospect = models.ForeignKey(Prospect, related_name="email_sends", on_delete=models.CASCADE)
    template = models.ForeignKey(EmailTemplate, null=True, blank=True, on_delete=models.SET_NULL)
    campaign_prospect = models.ForeignKey(
        "prospects.CampaignProspect", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="email_sends",
    )
    email_step = models.ForeignKey("prospects.EmailStep", null=True, blank=True, on_delete=models.SET_NULL)
    email_variant = models.ForeignKey("prospects.EmailVariant", null=True, blank=True, on_delete=models.SET_NULL)

    from_email = models.EmailField(blank=True)
    reply_to_email = models.EmailField(blank=True)
    to_email = models.EmailField()
    subject = models.CharField(max_length=255)
    html_body = models.TextField(blank=True)
    text_body = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="draft")
    provider_message_id = models.CharField(max_length=255, blank=True)

    message_id = models.CharField(max_length=255, blank=True, help_text="En-tête Message-ID généré à l'envoi.")
    in_reply_to = models.CharField(max_length=255, blank=True)
    references = models.CharField(max_length=1000, blank=True)

    smtp_response = models.CharField(max_length=500, blank=True)
    bounce_reason = models.CharField(max_length=500, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)

    is_test = models.BooleanField(default=False)

    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class SearchDecision(models.Model):
    DECISION = [("accepted","Acceptée"),("rejected","Refusée")]
    siren = models.CharField(max_length=9, db_index=True)
    siret = models.CharField(max_length=14, blank=True, db_index=True)
    company_name = models.CharField(max_length=255)
    decision = models.CharField(max_length=20, choices=DECISION)
    reason = models.CharField(max_length=255, blank=True)
    decided_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("siren", "decision")]
