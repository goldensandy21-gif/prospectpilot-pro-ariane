"""Mission 2 — identité email, conformité, désinscription.

Ne jamais inventer d'information juridique ici : les champs obligatoires
manquants doivent rester vides jusqu'à validation par l'utilisateur.
"""
from django.db import models

from .acquisition import ProductProfile

REQUIRED_FOR_COMPLIANCE = (
    "organization_name",
    "contact_email",
    "privacy_policy_url",
)


class EmailComplianceProfile(models.Model):
    product = models.OneToOneField(ProductProfile, related_name="compliance_profile", on_delete=models.CASCADE)

    organization_name = models.CharField(max_length=255, blank=True)
    legal_name = models.CharField(max_length=255, blank=True)
    postal_address = models.CharField(max_length=500, blank=True)
    country = models.CharField(max_length=120, blank=True)
    company_registration_number = models.CharField(max_length=60, blank=True, help_text="SIREN/SIRET si connu. Ne jamais inventer.")

    contact_email = models.EmailField(blank=True)
    privacy_contact_email = models.EmailField(blank=True)
    dpo_email = models.EmailField(blank=True)

    privacy_policy_url = models.URLField(blank=True)
    legal_notice_url = models.URLField(blank=True)
    data_rights_url = models.URLField(blank=True)

    default_legal_basis = models.CharField(
        max_length=255, blank=True,
        help_text="Ex. intérêt légitime pour la prospection B2B — à faire valider juridiquement.",
    )
    default_purpose = models.CharField(max_length=255, blank=True)

    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__name"]

    def __str__(self):
        return f"Conformité e-mail — {self.product.name}"

    @property
    def missing_required_fields(self):
        return [field for field in REQUIRED_FOR_COMPLIANCE if not getattr(self, field, "")]

    @property
    def compliance_ready(self):
        return not self.missing_required_fields

    def readiness_reason(self):
        missing = self.missing_required_fields
        if not missing:
            return ""
        return "Configuration de conformité incomplète : " + ", ".join(missing)
