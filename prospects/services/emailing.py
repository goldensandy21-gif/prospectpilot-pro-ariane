from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import Context, Template
from django.urls import reverse
from django.utils import timezone

from .messaging import build_message
from prospects.models import ContactLog, EmailSend, Suppression


VISUAL_TEMPLATE_PREFIX = "VISUAL_TEMPLATE:"
DEFAULT_DESIGN_KEY = "signal"
PLACEHOLDER_TEMPLATE_MARKERS = (
    "Utilisez le modèle visuel",
    "Utilisez le modele visuel",
)

EMAIL_DESIGNS = {
    "signal": {
        "label": "Signal clair",
        "brand": "ProspectPilot Pro",
        "headline": "Anticipez les besoins avant qu'ils deviennent urgents.",
        "subtitle": "Une approche claire pour transformer les signaux métier en décisions concrètes.",
        "primary": "#12263a",
        "accent": "#8de0bd",
        "button": "#ec6b4f",
        "background": "#eef4f1",
        "surface": "#ffffff",
        "footer": "#f7faf8",
        "image": "https://images.unsplash.com/photo-1551288049-bebda4e38f71?auto=format&fit=crop&w=1200&q=80",
    },
    "blueprint": {
        "label": "Plan d'action",
        "brand": "ProspectPilot Pro",
        "headline": "Transformez vos signaux métier en plan d'action.",
        "subtitle": "Un message sobre, structuré et orienté décision.",
        "primary": "#183b7a",
        "accent": "#9fd5ff",
        "button": "#3158ff",
        "background": "#edf4ff",
        "surface": "#ffffff",
        "footer": "#f5f8ff",
        "image": "https://images.unsplash.com/photo-1552664730-d307ca884978?auto=format&fit=crop&w=1200&q=80",
    },
    "growth": {
        "label": "Croissance & priorités",
        "brand": "ProspectPilot Pro",
        "headline": "Repérez les priorités qui accélèrent la croissance.",
        "subtitle": "Un modèle plus dynamique, pensé pour les dirigeants et équipes commerciales.",
        "primary": "#123c2c",
        "accent": "#b7f4cf",
        "button": "#2ea676",
        "background": "#eef8f2",
        "surface": "#ffffff",
        "footer": "#f6fbf8",
        "image": "https://images.unsplash.com/photo-1460925895917-afdab827c52f?auto=format&fit=crop&w=1200&q=80",
    },
    "warm": {
        "label": "Approche chaleureuse",
        "brand": "ProspectPilot Pro",
        "headline": "Une prise de contact simple, humaine et professionnelle.",
        "subtitle": "Un ton plus accessible pour ouvrir la discussion sans pression.",
        "primary": "#432818",
        "accent": "#ffd6a5",
        "button": "#d8642a",
        "background": "#fff4ea",
        "surface": "#ffffff",
        "footer": "#fff8f1",
        "image": "https://images.unsplash.com/photo-1497366811353-6870744d04b2?auto=format&fit=crop&w=1200&q=80",
    },
}


def design_key_from_template(template_obj):
    if not template_obj:
        return DEFAULT_DESIGN_KEY
    marker = (template_obj.html_body or "").strip()
    if marker.startswith(VISUAL_TEMPLATE_PREFIX):
        key = marker.replace(VISUAL_TEMPLATE_PREFIX, "", 1).strip()
        if key in EMAIL_DESIGNS:
            return key
    return DEFAULT_DESIGN_KEY


def _setting(name, default=""):
    return getattr(settings, name, default)


def _render(value, ctx):
    return Template(value or "").render(Context(ctx)).strip()


def _append_unsubscribe(text, unsubscribe_url):
    text = (text or "").strip()
    if unsubscribe_url and unsubscribe_url not in text:
        text += "\n\nOpposition : " + unsubscribe_url
    return text


def _message_body(text, unsubscribe_url):
    cleaned = []
    for block in (text or "").strip().split("\n\n"):
        value = block.strip()
        if not value:
            continue
        if unsubscribe_url and unsubscribe_url in value:
            continue
        if value.lower().startswith("opposition"):
            continue
        cleaned.append(value)
    return cleaned


def _legal_footer(ctx, design):
    return (
        f'<tr><td style="background:{design["footer"]};padding:22px 32px 28px 32px;'
        'border-top:1px solid #e1e7e4;">'
        '<p style="margin:0 0 10px 0;font-size:12px;line-height:1.6;color:#667085;">'
        "Vous recevez ce message dans un contexte professionnel, en lien avec votre activité. "
        "Source des coordonnées : informations professionnelles publiques.</p>"
        '<p style="margin:0;font-size:12px;line-height:1.6;color:#667085;">'
        "Vous pouvez vous opposer gratuitement à toute nouvelle sollicitation : "
        f'<a href="{escape(ctx["unsubscribe_url"])}" style="color:#3158ff;text-decoration:underline;">'
        "ne plus recevoir de messages</a>.<br>"
        f'{escape(ctx["company_name_sender"])} - {escape(ctx["postal_address"])}</p>'
        "</td></tr>"
    )


def build_email_context(prospect, request=None, template_obj=None):
    summary = prospect.audit_summaries.first()
    observation = ""
    if summary and summary.issues:
        observation = "Notre analyse publique a notamment relevé : " + ", ".join(summary.issues[:3]).lower() + "."

    public_base_url = _setting("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    product_url = _setting("PRODUCT_URL", public_base_url)
    path = reverse("unsubscribe", kwargs={"token": prospect.unsubscribe_token})
    unsubscribe_url = request.build_absolute_uri(path) if request else public_base_url.rstrip("/") + path
    ctx = {
        "company_name": prospect.name,
        "sector": prospect.sector or "votre secteur",
        "city": prospect.city or "",
        "observation": observation,
        "product_url": product_url,
        "contact_email": _setting("CONTACT_EMAIL", _setting("DEFAULT_FROM_EMAIL", "")),
        "unsubscribe_url": unsubscribe_url,
        "company_name_sender": _setting("COMPANY_NAME", "ProspectPilot Pro"),
        "postal_address": _setting("COMPANY_POSTAL_ADDRESS", "Lyon, France"),
        "preheader": "Une proposition courte pour présenter ProspectPilot Pro.",
    }
    if template_obj and template_obj.preheader:
        ctx["preheader"] = _render(template_obj.preheader, ctx)
    return ctx


def visual_html_message(text, ctx, design_key=DEFAULT_DESIGN_KEY):
    design = EMAIL_DESIGNS.get(design_key, EMAIL_DESIGNS[DEFAULT_DESIGN_KEY])
    body_parts = _message_body(text, ctx["unsubscribe_url"])
    if not body_parts:
        body_parts = [
            "Bonjour,",
            "Je vous contacte au sujet de ProspectPilot Pro.",
            "Je peux vous transmettre une courte présentation adaptée à votre activité.",
            "Bien cordialement,\nAriane",
        ]

    paragraphs = [
        f'<p style="margin:0 0 16px 0;font-size:16px;line-height:1.7;color:#283345;">'
        f"{escape(part).replace(chr(10), '<br>')}</p>"
        for part in body_parts
    ]
    body = "".join(paragraphs)
    image_row = ""
    if design.get("image"):
        image_row = (
            '<tr><td>'
            f'<img src="{escape(design["image"])}" width="680" alt="" '
            'style="display:block;width:100%;max-width:680px;height:auto;border:0;">'
            '</td></tr>'
        )

    return (
        '<!doctype html><html lang="fr"><body style="margin:0;'
        f'background:{design["background"]};font-family:Arial,Helvetica,sans-serif;color:#182033;">'
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">'
        f'{escape(ctx["preheader"])}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{design["background"]};">'
        '<tr><td align="center" style="padding:30px 14px;">'
        '<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="width:100%;max-width:680px;'
        f'background:{design["surface"]};border-radius:22px;overflow:hidden;border:1px solid #dfe8e3;'
        'box-shadow:0 18px 45px rgba(21,31,46,0.12);">'
        f"{image_row}"
        f'<tr><td style="background:{design["primary"]};padding:34px 32px;color:#ffffff;">'
        f'<div style="font-size:11px;letter-spacing:2.8px;text-transform:uppercase;color:{design["accent"]};font-weight:bold;">'
        f'{escape(design["brand"])}</div>'
        '<h1 style="margin:14px 0 10px 0;font-size:32px;line-height:1.08;color:#ffffff;font-weight:800;">'
        f'{escape(design["headline"])}</h1>'
        '<p style="margin:0;color:#e5edf4;font-size:15px;line-height:1.7;">'
        f'{escape(design["subtitle"])}</p>'
        "</td></tr>"
        f'<tr><td style="padding:34px 32px 28px 32px;">{body}'
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px 0 0 0;">'
        f'<tr><td bgcolor="{design["button"]}" style="background:{design["button"]};border-radius:12px;">'
        f'<a href="{escape(ctx["product_url"])}" style="display:inline-block;padding:14px 22px;'
        'color:#ffffff;text-decoration:none;font-weight:bold;font-size:15px;">Découvrir ProspectPilot Pro</a>'
        "</td></tr></table></td></tr>"
        f"{_legal_footer(ctx, design)}"
        "</table></td></tr></table></body></html>"
    )


def custom_html_message(text, ctx):
    return visual_html_message(text, ctx)


def render_email(prospect, template_obj=None, request=None, subject_override="", text_override=None):
    ctx = build_email_context(prospect, request, template_obj)
    subject_source = template_obj.subject if template_obj else "{{ company_name }} - découvrir ProspectPilot Pro"
    subject = _render(subject_source, ctx)
    if subject_override and subject_override.strip():
        subject = subject_override.strip()

    summary = prospect.audit_summaries.first()
    if template_obj and template_obj.text_body:
        text = _render(template_obj.text_body, ctx)
    else:
        text = build_message(prospect, summary)

    if text_override is not None and str(text_override).strip():
        text = str(text_override).strip()
    text = _append_unsubscribe(text, ctx["unsubscribe_url"])

    html_source = template_obj.html_body if template_obj else ""
    marker_source = (html_source or "").strip()
    use_visual_shell = (
        text_override is not None
        or not html_source
        or marker_source.startswith(VISUAL_TEMPLATE_PREFIX)
        or any(marker in html_source for marker in PLACEHOLDER_TEMPLATE_MARKERS)
    )
    if use_visual_shell:
        html = visual_html_message(text, ctx, design_key_from_template(template_obj))
    else:
        html = Template(html_source).render(Context(ctx))
    return subject, html, text


def can_email(prospect):
    if not prospect.prospecting_allowed or prospect.status == "do_not_contact":
        return False, "Prospect en opposition."
    if not prospect.public_email:
        return False, "Aucune adresse e-mail professionnelle."
    if Suppression.objects.filter(active=True, email__iexact=prospect.public_email).exists():
        return False, "Adresse présente dans la liste d'opposition."
    return True, ""


def send_prospect_email(prospect, template_obj=None, request=None, subject_override="", text_override=""):
    allowed, reason = can_email(prospect)
    subject, html, text = render_email(
        prospect,
        template_obj,
        request,
        subject_override=subject_override,
        text_override=text_override,
    )
    record = EmailSend.objects.create(
        prospect=prospect,
        template=template_obj,
        to_email=prospect.public_email or "",
        subject=subject,
        html_body=html,
        text_body=text,
        status="draft",
    )
    if not allowed:
        record.status = "blocked"
        record.error = reason
        record.save(update_fields=["status", "error"])
        return record
    try:
        msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [prospect.public_email])
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)
        now = timezone.now()
        record.status = "sent"
        record.sent_at = now
        record.save(update_fields=["status", "sent_at"])
        ContactLog.objects.create(
            prospect=prospect,
            channel="email",
            subject=subject,
            message=text,
            outcome="sent",
        )
        prospect.status = "contacted"
        prospect.last_contacted_at = now
        prospect.save(update_fields=["status", "last_contacted_at"])
        return record
    except Exception as exc:
        record.status = "failed"
        record.error = str(exc)
        record.save(update_fields=["status", "error"])
        return record
