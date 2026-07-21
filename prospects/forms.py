from django import forms
from .models import Prospect, ContactLog, EmailTemplate


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
