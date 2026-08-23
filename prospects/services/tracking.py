"""ETAPE 19/29 — liens de tracking de campagne.

CampaignProspect.tracking_token est un token aléatoire (secrets.token_urlsafe,
généré une fois, jamais dérivé d'un ID séquentiel) : il sert de clé de lookup pour
le lien de clic et n'expose aucun identifiant interne prévisible.
"""
from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse


def _base_url(request=None):
    if request is not None:
        return request.build_absolute_uri("/").rstrip("/")
    return getattr(settings, "PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


TARGET_URLS = {
    "simulator": lambda product: product.simulator_url,
    "product": lambda product: product.website_url,
    "signup": lambda product: product.signup_url,
}


def build_tracking_url(campaign_prospect, cta_type="product", email_step=None, email_variant=None, request=None):
    """URL de clic ProspectPilot — redirige vers PredictNeed avec les UTM, en
    enregistrant l'EngagementEvent avant redirection (voir views.track_click)."""
    path = reverse("campaign_click", kwargs={"token": campaign_prospect.tracking_token})
    params = {"cta": cta_type}
    if email_step:
        params["step"] = email_step.pk
    if email_variant:
        params["variant"] = email_variant.pk
    return f"{_base_url(request)}{path}?{urlencode(params)}"


def build_utm_params(campaign, email_variant=None):
    params = {
        "utm_source": "prospectpilot",
        "utm_medium": "email",
        "utm_campaign": campaign.name.lower().replace(" ", "-")[:80] if campaign else "campagne",
    }
    if email_variant:
        params["utm_content"] = email_variant.name.lower().replace(" ", "-")[:80]
    return params


def resolve_target_url(campaign_prospect, cta_type):
    """ETAPE 20 : le token de campagne (`ppt`) voyage dans l'URL pour que PredictNeed
    puisse le restituer lors des événements remontés via l'API serveur-à-serveur."""
    product = campaign_prospect.campaign.product
    getter = TARGET_URLS.get(cta_type, TARGET_URLS["product"])
    target = getter(product) or product.website_url or ""
    if not target:
        return ""
    params = {**build_utm_params(campaign_prospect.campaign), "ppt": campaign_prospect.tracking_token}
    separator = "&" if "?" in target else "?"
    return f"{target}{separator}{urlencode(params)}"


def build_privacy_url(prospect, request=None):
    path = reverse("prospect_privacy", kwargs={"token": prospect.unsubscribe_token})
    return f"{_base_url(request)}{path}"


def build_unsubscribe_url(prospect, request=None):
    path = reverse("unsubscribe", kwargs={"token": prospect.unsubscribe_token})
    return f"{_base_url(request)}{path}"


def build_open_tracking_url(open_tracking_token, request=None):
    """Section H (automatisation email) — pixel d'ouverture indicatif.
    `open_tracking_token` est un token opaque non séquentiel (voir
    EmailSend.open_tracking_token) ; jamais généré pour un envoi is_test."""
    path = reverse("track_email_open", kwargs={"token": open_tracking_token})
    return f"{_base_url(request)}{path}"
