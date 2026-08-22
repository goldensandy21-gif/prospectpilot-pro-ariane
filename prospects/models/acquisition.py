"""Acquisition engine models: turns ProspectPilot into PredictNeed IA's business-development cockpit.

These models are additive — nothing here removes or renames existing Prospect-related
functionality in core.py. They live in the same Django app/migration history.
"""
import secrets
import uuid

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .core import Prospect


# ---------------------------------------------------------------------------
# ETAPE 2 — Produit vendu (configurable, jamais codé en dur dans les emails)
# ---------------------------------------------------------------------------

class ProductProfile(models.Model):
    name = models.CharField(max_length=160, unique=True)
    slug = models.SlugField(max_length=160, unique=True)
    active = models.BooleanField(default=True)

    website_url = models.URLField(blank=True)
    simulator_url = models.URLField(blank=True)
    signup_url = models.URLField(blank=True)
    pricing_url = models.URLField(blank=True)

    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=8, default="EUR")

    short_value_proposition = models.CharField(max_length=280, blank=True)
    long_value_proposition = models.TextField(blank=True)
    target_problem = models.TextField(blank=True)

    primary_cta_label = models.CharField(max_length=120, blank=True)
    primary_cta_url = models.URLField(blank=True)

    features = models.JSONField(default=list, blank=True)
    differentiators = models.JSONField(default=list, blank=True)
    competitor_notes = models.JSONField(default=dict, blank=True)

    sender_brand_name = models.CharField(max_length=160, blank=True)
    # Enveloppe expéditeur (mission 2) — évite le doublon avec EmailComplianceProfile,
    # qui porte l'identité JURIDIQUE (raison sociale, adresse...) et pas l'identité d'envoi.
    sender_name = models.CharField(max_length=160, blank=True, help_text="Nom visible de l'expéditeur, ex. 'PredictNeed IA'.")
    sender_email = models.EmailField(blank=True, help_text="Adresse From officielle du produit.")
    reply_to_email = models.EmailField(blank=True)
    logo_url = models.URLField(blank=True, help_text="Laisser vide tant qu'aucun logo officiel n'est disponible.")
    contact_url = models.URLField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def display_sender_identity(self):
        name = self.sender_name or self.sender_brand_name or self.name
        if self.sender_email:
            return f"{name} <{self.sender_email}>"
        return name


# ---------------------------------------------------------------------------
# ETAPE 3 — ICP commercial
# ---------------------------------------------------------------------------

# Correctif d'audit (post-Mission 6, section 7) : "Priorité"
# (predictneed_acquisition_score) ignorait INTENT/ENGAGEMENT — un score qui
# n'incorpore pas ce qui vient d'être construit ne mérite plus ce nom. Ajout
# de "intent"/"engagement" comme composantes pondérées, PAS un cinquième
# score concurrent : rebalancé à partir des poids historiques (icp_fit
# 30->25, need 25->15, acquisition_maturity 20->15, contactability 15->15
# inchangé — reste un verrou dur via outbound_eligible, timing 10->5 car
# largement remplacé par le nouveau intent_score, pondéré par fraîcheur,
# que timing_score n'était pas). effective_weights() ci-dessous fusionne ce
# dict avec ICPProfile.weights puis renormalise à 100% — un ICPProfile
# existant sans clés "intent"/"engagement" en hérite automatiquement de ces
# valeurs par défaut, sans migration nécessaire.
DEFAULT_ICP_WEIGHTS = {
    "icp_fit": 25,
    "need": 15,
    "acquisition_maturity": 15,
    "contactability": 15,
    "timing": 5,
    "intent": 15,
    "engagement": 10,
}


class ICPProfile(models.Model):
    name = models.CharField(max_length=160)
    product = models.ForeignKey(ProductProfile, related_name="icp_profiles", on_delete=models.CASCADE)
    active = models.BooleanField(default=True)
    description = models.TextField(blank=True)

    target_sectors = models.JSONField(default=list, blank=True)
    naf_codes = models.JSONField(default=list, blank=True)
    naf_sections = models.JSONField(default=list, blank=True)
    company_categories = models.JSONField(default=list, blank=True)
    employee_bands = models.JSONField(default=list, blank=True)
    employee_min = models.PositiveIntegerField(null=True, blank=True)
    employee_max = models.PositiveIntegerField(null=True, blank=True)
    revenue_min = models.BigIntegerField(null=True, blank=True)
    revenue_max = models.BigIntegerField(null=True, blank=True)

    regions = models.JSONField(default=list, blank=True)
    departments = models.JSONField(default=list, blank=True)
    cities = models.JSONField(default=list, blank=True)

    min_company_age_years = models.PositiveSmallIntegerField(null=True, blank=True)
    max_company_age_years = models.PositiveSmallIntegerField(null=True, blank=True)

    required_signals = models.JSONField(default=list, blank=True)
    positive_signals = models.JSONField(default=list, blank=True)
    negative_signals = models.JSONField(default=list, blank=True)
    excluded_signals = models.JSONField(default=list, blank=True)
    excluded_domains = models.JSONField(default=list, blank=True)
    excluded_sectors = models.JSONField(default=list, blank=True)

    weights = models.JSONField(default=dict, blank=True, help_text="Pondération icp_fit/need/acquisition_maturity/contactability/timing/intent/engagement (somme=100). Vide = valeurs par défaut (DEFAULT_ICP_WEIGHTS).")
    minimum_outbound_score = models.PositiveSmallIntegerField(default=50)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["product", "name"], name="unique_icp_name_per_product"),
        ]

    def __str__(self):
        return f"{self.name} ({self.product.name})"

    def effective_weights(self):
        weights = {**DEFAULT_ICP_WEIGHTS, **(self.weights or {})}
        total = sum(weights.values()) or 1
        return {key: (value / total) * 100 for key, value in weights.items()}


# ---------------------------------------------------------------------------
# ETAPE 4/5/8/25 — Pipeline de recherche (deux modes)
# ---------------------------------------------------------------------------

class CompanySearchRun(models.Model):
    MODES = [
        ("manual", "Recherche manuelle"),
        ("acquisition", "Acquisition PredictNeed IA"),
    ]
    STATUS = [
        ("draft", "Brouillon"),
        ("queued", "En file"),
        ("running", "En cours"),
        ("paused", "En pause"),
        ("done", "Terminé"),
        ("failed", "Échec"),
        ("cancelled", "Annulé"),
    ]

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="search_runs")
    mode = models.CharField(max_length=20, choices=MODES, default="manual")
    product = models.ForeignKey(ProductProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="search_runs")
    icp = models.ForeignKey(ICPProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="search_runs")
    preset = models.ForeignKey("SearchPreset", null=True, blank=True, on_delete=models.SET_NULL, related_name="search_runs")

    campaign_name = models.CharField(max_length=160, blank=True)
    criteria = models.JSONField(default=dict, blank=True)

    volume_max_candidates = models.PositiveIntegerField(default=500)
    volume_max_enrich = models.PositiveIntegerField(default=100)
    score_threshold = models.PositiveSmallIntegerField(default=50)
    ranking_option = models.CharField(max_length=40, blank=True, default="priority_score")

    status = models.CharField(max_length=20, choices=STATUS, default="draft", db_index=True)

    registry_count = models.PositiveIntegerField(default=0)
    preselected_count = models.PositiveIntegerField(default=0)
    with_site_count = models.PositiveIntegerField(default=0)
    # "Réellement scannés" (mission 5, section 10) : candidats ayant vraiment
    # eu un quick scan de leur site — exclut explicitement les no_site, qui ne
    # sont jamais passés par une analyse réelle malgré un status final "not_eligible".
    enriched_count = models.PositiveIntegerField(default=0)
    with_email_count = models.PositiveIntegerField(default=0)
    qualified_a_count = models.PositiveIntegerField(default=0)
    qualified_b_count = models.PositiveIntegerField(default=0)
    qualified_c_count = models.PositiveIntegerField(default=0)
    # Non éligibles APRÈS analyse réelle uniquement (exclut les no_site : voir enriched_count).
    not_eligible_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)

    errors = models.JSONField(default=list, blank=True)
    current_stage = models.CharField(max_length=40, blank=True)
    cursor = models.JSONField(default=dict, blank=True, help_text="Position de reprise (page registre, index candidat...).")

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_mode_display()} #{self.pk} ({self.get_status_display()})"

    def append_error(self, message, save=True):
        self.errors = (self.errors or []) + [{"message": str(message)[:500], "at": timezone.now().isoformat()}]
        if save:
            self.save(update_fields=["errors"])


class SearchCandidate(models.Model):
    """Résultat registre en attente de qualification — pas encore un Prospect."""
    STATUS = [
        ("pending", "En attente"),
        ("preselected", "Présélectionné"),
        ("rejected_prescore", "Rejeté (pré-score)"),
        ("site_found", "Site trouvé"),
        ("no_site", "Site introuvable"),
        ("scanned", "Analysé (quick scan)"),
        ("enriched", "Enrichi"),
        ("scored", "Scoré"),
        ("converted", "Converti en prospect"),
        ("not_eligible", "Non éligible"),
        ("error", "Erreur"),
    ]

    search_run = models.ForeignKey(CompanySearchRun, related_name="candidates", on_delete=models.CASCADE)
    siren = models.CharField(max_length=9, db_index=True)
    siret = models.CharField(max_length=14, blank=True, db_index=True)
    name = models.CharField(max_length=255)
    raw_data = models.JSONField(default=dict, blank=True)

    pre_score = models.PositiveSmallIntegerField(default=0)
    pre_score_reasons = models.JSONField(default=list, blank=True)

    site_url = models.URLField(blank=True)
    site_confidence = models.PositiveSmallIntegerField(default=0)

    quick_scan_data = models.JSONField(default=dict, blank=True)

    contact_email = models.EmailField(blank=True)
    outbound_eligible = models.BooleanField(default=False)
    outbound_ineligible_reason = models.CharField(max_length=255, blank=True)

    final_score = models.PositiveSmallIntegerField(default=0)
    grade = models.CharField(max_length=1, blank=True)

    status = models.CharField(max_length=20, choices=STATUS, default="pending", db_index=True)
    error = models.TextField(blank=True)

    prospect = models.ForeignKey(Prospect, null=True, blank=True, on_delete=models.SET_NULL, related_name="search_candidate_origins")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-final_score", "-pre_score"]
        constraints = [
            models.UniqueConstraint(fields=["search_run", "siren"], name="unique_candidate_per_run_siren"),
        ]

    def __str__(self):
        return f"{self.name} ({self.siren})"


class SearchPreset(models.Model):
    name = models.CharField(max_length=160)
    product = models.ForeignKey(ProductProfile, on_delete=models.CASCADE, related_name="search_presets")
    icp = models.ForeignKey(ICPProfile, on_delete=models.CASCADE, related_name="search_presets")
    filters = models.JSONField(default=dict, blank=True, help_text="Filtres registre additionnels (zone, etc).")
    volume_max_candidates = models.PositiveIntegerField(default=500)
    volume_max_enrich = models.PositiveIntegerField(default=100)
    score_threshold = models.PositiveSmallIntegerField(default=50)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    periodic_task = models.ForeignKey(
        "django_celery_beat.PeriodicTask", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="search_preset",
    )
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# ETAPE 9 — Technologies & maturité acquisition
# ---------------------------------------------------------------------------

class ProspectTechnology(models.Model):
    CATEGORIES = [
        ("analytics", "Analytics"),
        ("advertising", "Publicité"),
        ("crm", "CRM / Marketing automation"),
        ("scheduling", "Prise de RDV / formulaires"),
        ("support", "Support / chat"),
        ("ecommerce", "E-commerce"),
        ("behaviour_analytics", "Analyse comportementale"),
        ("visitor_intelligence", "Intelligence visiteurs B2B"),
        ("other", "Autre"),
    ]

    prospect = models.ForeignKey(Prospect, related_name="technologies", on_delete=models.CASCADE)
    technology = models.CharField(max_length=120, db_index=True)
    category = models.CharField(max_length=30, choices=CATEGORIES, default="other")
    source_url = models.URLField(max_length=1000, blank=True)
    evidence = models.TextField(blank=True)
    confidence = models.PositiveSmallIntegerField(default=70)
    is_active = models.BooleanField(default=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["category", "technology"]
        constraints = [
            models.UniqueConstraint(fields=["prospect", "technology"], name="unique_technology_per_prospect"),
        ]

    def __str__(self):
        return f"{self.technology} ({self.prospect})"


# ---------------------------------------------------------------------------
# ETAPE 10 — Signaux business development
# ---------------------------------------------------------------------------

class ProspectSignal(models.Model):
    CATEGORIES = [
        ("icp", "ICP"),
        ("acquisition", "Acquisition"),
        ("conversion", "Conversion"),
        ("analytics", "Analytics"),
        ("advertising", "Publicité"),
        ("crm", "CRM"),
        ("competitor", "Concurrent"),
        ("contactability", "Contactabilité"),
        ("growth", "Croissance"),
        ("timing", "Timing"),
        ("risk", "Risque"),
    ]

    # Mission 6 — rôle du signal dans les trois scores (FIT/INTENT/ENGAGEMENT).
    # Distinct de `category` (qui décrit la NATURE technique du signal, ex.
    # "analytics") : `signal_group` décrit son RÔLE dans le scoring. Un signal
    # "analytics" (outil détecté) est un indice de FIT/maturité, jamais d'INTENT
    # à lui seul — voir services/intent_scoring.py.
    SIGNAL_GROUPS = [
        ("fit", "Fit"),
        ("intent", "Intent"),
        ("engagement", "Engagement"),
        ("risk", "Risque"),
    ]
    # D'où vient l'observation — distinct de `source_url` (l'URL précise).
    SOURCE_KINDS = [
        ("registry", "Registre officiel"),
        ("website", "Site web"),
        ("technology", "Technologie détectée"),
        ("open_web", "Web ouvert"),
        ("social", "Réseau social"),
        ("linkedin", "LinkedIn"),
        ("campaign", "Campagne ProspectPilot"),
        ("predictneed", "PredictNeed IA"),
        ("manual", "Ajout manuel"),
    ]

    prospect = models.ForeignKey(Prospect, related_name="signals", on_delete=models.CASCADE)
    signal_type = models.CharField(max_length=80, db_index=True)
    category = models.CharField(max_length=30, choices=CATEGORIES, default="acquisition")
    signal_group = models.CharField(max_length=20, choices=SIGNAL_GROUPS, default="fit")
    source_kind = models.CharField(max_length=20, choices=SOURCE_KINDS, blank=True)
    label = models.CharField(max_length=255)
    value = models.CharField(max_length=255, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    evidence = models.TextField(blank=True)
    confidence = models.PositiveSmallIntegerField(default=70)
    score_impact = models.SmallIntegerField(default=0)
    positive = models.BooleanField(default=True)
    # `observed_at` = quand l'événement/état réel a eu lieu (peut être connu avec
    # retard) ; `detected_at` = quand ProspectPilot a créé cette ligne. Les deux
    # peuvent différer ; la fraîcheur (services/signal_freshness.py) se base sur
    # `observed_at`, jamais sur `detected_at`.
    observed_at = models.DateTimeField(null=True, blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    # Identité de déduplication (mission 6, section 3) : deux détections avec la
    # même empreinte sont le même signal (on rafraîchit `last_checked_at`) ; une
    # empreinte différente est un événement réellement distinct (nouvelle ligne),
    # même si `signal_type` est identique. Voir services/signals.py::signal_fingerprint.
    fingerprint = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-positive", "-score_impact"]
        constraints = [
            models.UniqueConstraint(
                fields=["prospect", "signal_type", "source_kind", "fingerprint"],
                name="unique_signal_identity_per_prospect",
            )
        ]

    def __str__(self):
        return f"{self.label} ({self.prospect})"


# ---------------------------------------------------------------------------
# Mission 6, section 15 — Alertes
# ---------------------------------------------------------------------------

class Alert(models.Model):
    """Nouveau modèle justifié : aucun modèle existant ne représente une
    NOTIFICATION destinée à l'utilisateur commercial — ProspectSignal décrit
    un fait détecté CHEZ le prospect, EngagementEvent une action DU
    prospect ; Alert est la couche "quelque chose mérite l'attention de
    l'utilisateur maintenant", une entité métier distincte des deux."""
    ALERT_TYPES = [
        ("intent_threshold_crossed", "Seuil d'intention franchi"),
        ("strong_signal", "Signal fort détecté"),
        ("new_engagement", "Nouvel engagement PredictNeed"),
        ("reactivated", "Prospect réactivé après inactivité"),
    ]

    prospect = models.ForeignKey(Prospect, related_name="alerts", on_delete=models.CASCADE)
    alert_type = models.CharField(max_length=30, choices=ALERT_TYPES, db_index=True)
    message = models.CharField(max_length=255)
    # Dédoublonnage obligatoire (mission 6, section 15) : la même paire
    # (prospect, alert_type, dedup_key) ne peut exister qu'une fois. Chaque
    # type d'alerte choisit sa clé pour qu'un événement réel donne EXACTEMENT
    # une alerte — voir services/alerts.py pour le détail par type.
    dedup_key = models.CharField(max_length=120)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["prospect", "alert_type", "dedup_key"], name="unique_alert_dedup"),
        ]

    def __str__(self):
        return f"{self.get_alert_type_display()} - {self.prospect}"


# ---------------------------------------------------------------------------
# ETAPE 11 — Concurrents
# ---------------------------------------------------------------------------

class Competitor(models.Model):
    CATEGORIES = [
        ("behaviour_analytics", "Analyse comportementale"),
        ("visitor_intelligence", "Intelligence visiteurs B2B"),
        ("other", "Autre"),
    ]
    name = models.CharField(max_length=120, unique=True)
    category = models.CharField(max_length=30, choices=CATEGORIES, default="other")
    positioning = models.TextField(blank=True)
    relation_to_predictneed = models.TextField(blank=True, help_text="Complémentaire, concurrent direct, adjacent...")
    suggested_angle = models.TextField(blank=True)
    scoring_penalty = models.SmallIntegerField(default=0)
    scoring_bonus = models.SmallIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return self.name


class CompetitorDetection(models.Model):
    prospect = models.ForeignKey(Prospect, related_name="competitor_detections", on_delete=models.CASCADE)
    competitor = models.ForeignKey(Competitor, related_name="detections", on_delete=models.CASCADE)
    source_url = models.URLField(max_length=1000, blank=True)
    evidence = models.TextField(blank=True)
    confidence = models.PositiveSmallIntegerField(default=70)
    detected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-detected_at"]
        constraints = [
            models.UniqueConstraint(fields=["prospect", "competitor"], name="unique_competitor_per_prospect"),
        ]

    def __str__(self):
        return f"{self.competitor} détecté chez {self.prospect}"


# ---------------------------------------------------------------------------
# ETAPE 14 — Agent Brief
# ---------------------------------------------------------------------------

class AgentBrief(models.Model):
    prospect = models.ForeignKey(Prospect, related_name="agent_briefs", on_delete=models.CASCADE)
    product = models.ForeignKey(ProductProfile, null=True, blank=True, on_delete=models.SET_NULL)
    icp = models.ForeignKey(ICPProfile, null=True, blank=True, on_delete=models.SET_NULL)

    company_summary = models.TextField(blank=True)
    why_this_company = models.TextField(blank=True)
    detected_need = models.TextField(blank=True)
    acquisition_maturity = models.TextField(blank=True)
    relevant_signals = models.JSONField(default=list, blank=True)
    competitors_detected = models.JSONField(default=list, blank=True)
    recommended_contact = models.CharField(max_length=255, blank=True)
    recommended_contact_reason = models.TextField(blank=True)
    recommended_angle = models.TextField(blank=True)
    objections_or_risks = models.JSONField(default=list, blank=True)
    suggested_cta = models.CharField(max_length=255, blank=True)
    score = models.PositiveSmallIntegerField(default=0)
    score_reasons = models.JSONField(default=list, blank=True)
    evidence_urls = models.JSONField(default=list, blank=True)
    next_best_action = models.CharField(max_length=255, blank=True)

    generated_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-generated_at"]

    def __str__(self):
        return f"Brief {self.prospect} ({self.generated_at:%Y-%m-%d})"


# ---------------------------------------------------------------------------
# ETAPE 15 — Campagnes
# ---------------------------------------------------------------------------

class Campaign(models.Model):
    STATUS = [
        ("draft", "Brouillon"),
        ("ready", "Prêt (en attente de validation)"),
        ("active", "Active"),
        ("paused", "En pause"),
        ("completed", "Terminée"),
        ("cancelled", "Annulée"),
    ]

    name = models.CharField(max_length=160)
    product = models.ForeignKey(ProductProfile, related_name="campaigns", on_delete=models.CASCADE)
    icp = models.ForeignKey(ICPProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="campaigns")
    objective = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="draft", db_index=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    score_threshold = models.PositiveSmallIntegerField(default=65)
    daily_send_limit = models.PositiveIntegerField(default=30)
    total_limit = models.PositiveIntegerField(default=200)
    sequence = models.ForeignKey("EmailSequence", null=True, blank=True, on_delete=models.SET_NULL, related_name="campaigns")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    validated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="validated_campaigns")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def is_sendable(self):
        return self.status in ("ready", "active") and self.validated_at is not None


class CampaignProspect(models.Model):
    STATUS = [
        ("identified", "Identifié"),
        ("selected", "Sélectionné"),
        ("ready_to_contact", "Prêt à contacter"),
        ("contacted", "Contacté"),
        ("engaged", "Engagé"),
        ("signed_up", "Inscrit"),
        ("activated", "Activé"),
        ("paying", "Client payant"),
        # Correctif d'audit (round 3) — même état que Prospect.predictneed_stage,
        # voir services/predictneed_webhook.py.
        ("churned", "Client perdu (résiliation)"),
        ("nurture", "Nurture"),
        ("lost", "Perdu"),
        ("do_not_contact", "Ne plus contacter"),
        ("excluded", "Exclu"),
    ]

    campaign = models.ForeignKey(Campaign, related_name="campaign_prospects", on_delete=models.CASCADE)
    prospect = models.ForeignKey(Prospect, related_name="campaign_memberships", on_delete=models.CASCADE)
    acquisition_score_snapshot = models.PositiveSmallIntegerField(default=0)
    grade = models.CharField(max_length=1, blank=True)
    agent_brief = models.ForeignKey(AgentBrief, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, choices=STATUS, default="identified", db_index=True)
    tracking_token = models.CharField(max_length=64, unique=True, editable=False, default=secrets.token_urlsafe)

    selected_at = models.DateTimeField(null=True, blank=True)
    contacted_at = models.DateTimeField(null=True, blank=True)
    last_engagement_at = models.DateTimeField(null=True, blank=True)
    converted_at = models.DateTimeField(null=True, blank=True)
    excluded_reason = models.CharField(max_length=255, blank=True)

    # Mission 6, section 11 — position dans la séquence multicanal : l'étape
    # dont l'action a été exécutée en dernier (None = aucune étape encore
    # exécutée). Sert à garantir qu'une seule action est en vol à la fois —
    # voir services/campaign_sequencing.py.
    current_step = models.ForeignKey("prospects.EmailStep", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    current_step_started_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-acquisition_score_snapshot"]
        constraints = [
            models.UniqueConstraint(fields=["campaign", "prospect"], name="unique_prospect_per_campaign"),
        ]

    def __str__(self):
        return f"{self.prospect} - {self.campaign}"


# ---------------------------------------------------------------------------
# ETAPE 16 — Séquences email
# ---------------------------------------------------------------------------

class EmailSequence(models.Model):
    product = models.ForeignKey(ProductProfile, related_name="sequences", on_delete=models.CASCADE)
    icp = models.ForeignKey(ICPProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="sequences")
    name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)
    stop_on_reply = models.BooleanField(default=True)
    stop_on_conversion = models.BooleanField(default=True)
    stop_on_unsubscribe = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.product.name})"


class EmailStep(models.Model):
    # Mission 6, section 11 — une étape de séquence n'est plus forcément un
    # e-mail : reste le même modèle/la même table (pas de second système de
    # campagne), une étape LinkedIn utilise juste `channel` au lieu d'un
    # EmailVariant. `delay_days` continue de porter le délai depuis l'étape
    # précédente pour toutes les étapes, y compris LinkedIn.
    CHANNELS = [
        ("email", "E-mail"),
        ("linkedin_connect", "Invitation LinkedIn"),
        ("linkedin_message", "Message LinkedIn"),
    ]
    # Condition à satisfaire, EN PLUS du délai, avant d'exécuter cette étape.
    # "always" reproduit exactement le comportement historique (délai seul) —
    # aucune régression pour les séquences e-mail existantes.
    ADVANCE_CONDITIONS = [
        ("always", "Toujours (délai seul)"),
        ("linkedin_accepted", "Invitation LinkedIn acceptée"),
    ]

    sequence = models.ForeignKey(EmailSequence, related_name="steps", on_delete=models.CASCADE)
    order = models.PositiveSmallIntegerField(default=1)
    delay_days = models.PositiveSmallIntegerField(default=0, help_text="Délai depuis l'étape précédente (0 = premier email).")
    name = models.CharField(max_length=160, blank=True)
    active = models.BooleanField(default=True)
    channel = models.CharField(max_length=20, choices=CHANNELS, default="email")
    advance_condition = models.CharField(max_length=30, choices=ADVANCE_CONDITIONS, default="always")

    class Meta:
        ordering = ["sequence", "order"]
        constraints = [
            models.UniqueConstraint(fields=["sequence", "order"], name="unique_step_order_per_sequence"),
        ]

    def __str__(self):
        return f"{self.sequence.name} - étape {self.order}"


class EmailVariant(models.Model):
    CTA_CHOICES = [
        ("simulator", "Tester le simulateur"),
        ("product", "Voir PredictNeed IA"),
        ("signup", "Créer un compte"),
        ("reply", "Répondre à l'email"),
    ]
    step = models.ForeignKey(EmailStep, related_name="variants", on_delete=models.CASCADE)
    name = models.CharField(max_length=160)
    subject_template = models.CharField(max_length=255)
    cta_type = models.CharField(max_length=20, choices=CTA_CHOICES, default="simulator")
    cta_label_override = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)
    weight = models.PositiveSmallIntegerField(default=100, help_text="Poids relatif pour la répartition A/B.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["step", "name"]

    def __str__(self):
        return f"{self.step} - {self.name}"


# ---------------------------------------------------------------------------
# ETAPE 19 — Tracking
# ---------------------------------------------------------------------------

class EngagementEvent(models.Model):
    EVENT_TYPES = [
        ("email_sent", "E-mail envoyé"),
        ("email_failed", "E-mail en échec"),
        ("link_clicked", "Lien cliqué"),
        ("product_visited", "Visite du produit"),
        ("simulator_started", "Simulateur démarré"),
        ("simulator_completed", "Simulateur terminé"),
        ("signup_started", "Inscription démarrée"),
        ("signup_completed", "Inscription terminée"),
        ("checkout_started", "Paiement démarré"),
        ("subscription_activated", "Abonnement activé"),
        ("subscription_cancelled", "Abonnement annulé"),
    ]
    SOURCES = [("prospectpilot", "ProspectPilot"), ("predictneed", "PredictNeed IA")]

    campaign_prospect = models.ForeignKey(CampaignProspect, null=True, blank=True, on_delete=models.CASCADE, related_name="events")
    prospect = models.ForeignKey(Prospect, null=True, blank=True, on_delete=models.CASCADE, related_name="engagement_events")
    campaign = models.ForeignKey(Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    email_step = models.ForeignKey(EmailStep, null=True, blank=True, on_delete=models.SET_NULL)
    email_variant = models.ForeignKey(EmailVariant, null=True, blank=True, on_delete=models.SET_NULL)

    event_type = models.CharField(max_length=30, choices=EVENT_TYPES, db_index=True)
    source = models.CharField(max_length=20, choices=SOURCES, default="prospectpilot")
    metadata = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=120, unique=True, null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["event_type", "occurred_at"])]

    def __str__(self):
        return f"{self.event_type} - {self.prospect}"


# ---------------------------------------------------------------------------
# ETAPE 21 — Revenue attribution
# ---------------------------------------------------------------------------

class ConversionEvent(models.Model):
    EVENT_TYPES = [
        ("signup", "Inscription"),
        ("activation", "Activation"),
        ("paying", "Abonnement payant"),
        ("cancelled", "Annulation"),
    ]
    prospect = models.ForeignKey(Prospect, related_name="conversion_events", on_delete=models.CASCADE)
    campaign = models.ForeignKey(Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="conversion_events")
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    external_reference = models.CharField(max_length=160, blank=True, help_text="Référence côté PredictNeed (jamais de données bancaires).")
    occurred_at = models.DateTimeField(default=timezone.now)
    raw_payload = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=120, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]

    def __str__(self):
        return f"{self.event_type} - {self.prospect}"


class RevenueAttribution(models.Model):
    conversion_event = models.OneToOneField(ConversionEvent, related_name="attribution", on_delete=models.CASCADE)
    prospect = models.ForeignKey(Prospect, related_name="revenue_attributions", on_delete=models.CASCADE)
    campaign = models.ForeignKey(Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="revenue_attributions")
    icp = models.ForeignKey(ICPProfile, null=True, blank=True, on_delete=models.SET_NULL)
    email_sequence = models.ForeignKey(EmailSequence, null=True, blank=True, on_delete=models.SET_NULL)
    email_step = models.ForeignKey(EmailStep, null=True, blank=True, on_delete=models.SET_NULL)
    email_variant = models.ForeignKey(EmailVariant, null=True, blank=True, on_delete=models.SET_NULL)
    subscription_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    mrr = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default="EUR")
    attributed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-attributed_at"]

    def __str__(self):
        return f"{self.mrr} {self.currency} MRR - {self.prospect}"
