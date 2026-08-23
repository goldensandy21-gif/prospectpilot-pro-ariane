from django import forms
from .models import (
    Campaign,
    ContactLog,
    EmailComplianceProfile,
    EmailTemplate,
    ICPProfile,
    ProductProfile,
    Prospect,
)


class CompanySearchForm(forms.Form):
    query = forms.CharField(label="Nom ou activité", max_length=180, required=False)
    naf_code = forms.CharField(label="Code NAF", max_length=10, required=False, help_text="Ex. 8559A")
    postal_code = forms.CharField(label="Code postal", max_length=5, required=False)
    department = forms.CharField(label="Département", max_length=3, required=False, help_text="Ex. 69")
    city = forms.CharField(label="Ville", max_length=120, required=False)
    employee_min = forms.IntegerField(label="Effectif minimum", required=False, min_value=0)
    page = forms.IntegerField(min_value=1, initial=1, widget=forms.HiddenInput())


class ProspectForm(forms.ModelForm):
    class Meta:
        model = Prospect
        fields = [
            "name", "legal_name", "sector", "naf_code", "department", "city", "postal_code",
            "address", "country", "siren", "siret", "website", "public_email", "public_phone",
            "status", "prospecting_allowed", "notes", "next_action_at",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "next_action_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }


class ContactLogForm(forms.ModelForm):
    class Meta:
        model = ContactLog
        fields = ["channel", "subject", "message", "outcome", "response_text", "follow_up_at"]
        widgets = {
            "message": forms.Textarea(attrs={"rows": 7}),
            "response_text": forms.Textarea(attrs={"rows": 3}),
            "follow_up_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }


class BulkAuditForm(forms.Form):
    selected = forms.ModelMultipleChoiceField(
        queryset=Prospect.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )

    def __init__(self, *args, **kwargs):
        qs = kwargs.pop("queryset", Prospect.objects.all())
        super().__init__(*args, **kwargs)
        self.fields["selected"].queryset = qs


class EmailComposeForm(forms.Form):
    template = forms.ModelChoiceField(
        queryset=EmailTemplate.objects.none(),
        required=False,
        label="Modèle",
    )
    subject = forms.CharField(label="Sujet", max_length=255, required=False)
    message = forms.CharField(
        label="Message",
        required=False,
        widget=forms.Textarea(attrs={"rows": 12}),
    )
    confirm_professional_relevance = forms.BooleanField(
        label="Je confirme que ce message est pertinent pour l'activité professionnelle du destinataire.",
        error_messages={"required": "Confirme la pertinence professionnelle avant l'envoi."},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template"].queryset = EmailTemplate.objects.filter(active=True).order_by("name")
        self.fields["template"].empty_label = "Modèle par défaut"
        for field in self.fields.values():
            css = "form-control"
            if isinstance(field.widget, forms.CheckboxInput):
                css = "form-check-input"
            existing = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (existing + " " + css).strip()


class RejectCompanyForm(forms.Form):
    reason = forms.CharField(label="Motif", max_length=255, required=False)


class ProspectImportForm(forms.Form):
    file = forms.FileField(label="Fichier CSV ou Excel")
    enrich_after_import = forms.BooleanField(
        label="Lancer l'enrichissement multi-sources après import",
        required=False,
        initial=True,
    )


class AcquisitionSearchForm(forms.Form):
    product = forms.ModelChoiceField(queryset=ProductProfile.objects.filter(active=True), label="Produit")
    icp = forms.ModelChoiceField(queryset=ICPProfile.objects.none(), label="ICP")
    department = forms.CharField(label="Département", max_length=3, required=False, help_text="Ex. 69 (laisser vide = France entière)")
    region = forms.CharField(label="Région (code INSEE)", max_length=3, required=False)
    volume_max_candidates = forms.IntegerField(label="Volume registre maximum", min_value=10, max_value=5000, initial=500)
    volume_max_enrich = forms.IntegerField(label="Volume à enrichir (site + contact + score)", min_value=5, max_value=1000, initial=100)
    score_threshold = forms.IntegerField(label="Seuil de score final", min_value=0, max_value=100, initial=50)
    campaign_name = forms.CharField(label="Nom de la recherche (optionnel)", max_length=160, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["icp"].queryset = ICPProfile.objects.filter(active=True).select_related("product")
        for field in self.fields.values():
            existing = field.widget.attrs.get("class", "")
            css = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-control"
            field.widget.attrs["class"] = (existing + " " + css).strip()


class CampaignCreateForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = ["name", "product", "icp", "sequence", "objective", "score_threshold", "daily_send_limit", "total_limit", "start_date", "end_date", "planning_managed"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }
        labels = {
            "planning_managed": "Piloter depuis le Planning e-mail (J0/J4/J8/J14, validation obligatoire avant envoi)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Correctif d'audit (LinkedIn/Hunter.io) : jusqu'ici la séquence
        # était TOUJOURS écrasée par get_or_create_default_sequence()
        # (e-mail seul), rendant impossible une campagne LinkedIn ou
        # multicanale même si une séquence existait déjà (créée dans
        # l'administration, où EmailSequence/EmailStep/EmailVariant sont
        # déjà enregistrés). Optionnel : laissé vide, le comportement par
        # défaut (séquence e-mail auto-créée) est inchangé.
        self.fields["sequence"].required = False
        self.fields["sequence"].help_text = (
            "Laisser vide pour la séquence e-mail par défaut. Choisir une séquence "
            "existante (LinkedIn ou multicanale, créée dans l'administration) pour "
            "en démarrer une différente."
        )
        for field in self.fields.values():
            existing = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (existing + " form-control").strip()


class ProductProfileForm(forms.ModelForm):
    class Meta:
        model = ProductProfile
        fields = [
            "name", "active", "website_url", "simulator_url", "signup_url", "pricing_url",
            "monthly_price", "currency", "short_value_proposition", "long_value_proposition",
            "target_problem", "primary_cta_label", "primary_cta_url",
            "sender_brand_name", "sender_name", "sender_email", "reply_to_email", "logo_url", "contact_url",
        ]
        widgets = {
            "long_value_proposition": forms.Textarea(attrs={"rows": 3}),
            "target_problem": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"


class EmailComplianceProfileForm(forms.ModelForm):
    class Meta:
        model = EmailComplianceProfile
        fields = [
            "organization_name", "legal_name", "postal_address", "country", "company_registration_number",
            "contact_email", "privacy_contact_email", "dpo_email",
            "privacy_policy_url", "legal_notice_url", "data_rights_url",
            "default_legal_basis", "default_purpose", "active",
        ]
        widgets = {"postal_address": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"
