"""ETAPE 7/10 (mission 2) — nouveau renderer PredictNeed IA.

Séparé du moteur legacy (emailing.py / EMAIL_DESIGNS) : sobre, un seul CTA, pas
d'image Unsplash, texte + HTML systématiques. Le contenu commercial (observation,
angle) vient uniquement de AgentBrief/ProspectSignal — jamais inventé.
"""
import uuid
from email.utils import make_msgid
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import Context, Template
from django.utils import timezone

from ..models import ContactLog, EmailSend, EngagementEvent
from .compliance_footer import render_compliance_footer_html, render_compliance_footer_text
from .email_identity import format_from_header, get_sender_identity
from .suppression import is_suppressed
from .tracking import build_privacy_url, build_tracking_url, build_unsubscribe_url, resolve_target_url

DEFAULT_CTA_LABELS = {
    "simulator": "Tester le simulateur",
    "product": "Voir comment fonctionne PredictNeed",
    "signup": "Créer un compte",
    "reply": "Répondre à cet e-mail",
}


def _render(template_str, ctx):
    return Template(template_str or "").render(Context(ctx)).strip()


def _first_name(prospect, email):
    contact = prospect.contact_people.filter(email__iexact=email, is_active=True).first()
    if contact and contact.first_name:
        return contact.first_name
    return ""


def _observation_line(agent_brief):
    if not agent_brief or not agent_brief.relevant_signals:
        return ""
    top = agent_brief.relevant_signals[0]
    return top.get("label", "")


def build_predictneed_context(campaign_prospect, email_step=None, email_variant=None, request=None):
    prospect = campaign_prospect.prospect
    campaign = campaign_prospect.campaign
    product = campaign.product
    agent_brief = campaign_prospect.agent_brief

    email = prospect.public_email or ""
    first_name = _first_name(prospect, email)
    cta_type = email_variant.cta_type if email_variant else "simulator"

    ctx = {
        "company_name": prospect.name,
        "sector": prospect.sector or "",
        "city": prospect.city or "",
        "first_name": first_name,
        "observation": _observation_line(agent_brief),
        "detected_signal": _observation_line(agent_brief),
        "detected_problem": agent_brief.detected_need if agent_brief else "",
        "product_name": product.name,
        "product_url": product.website_url,
        "simulator_url": product.simulator_url,
        "signup_url": product.signup_url,
        "value_proposition": product.short_value_proposition,
    }
    ctx["cta_url"] = build_tracking_url(campaign_prospect, cta_type=cta_type, email_step=email_step, email_variant=email_variant, request=request)
    ctx["cta_target_url"] = resolve_target_url(campaign_prospect, cta_type)
    ctx["cta_label"] = (email_variant.cta_label_override if email_variant else "") or DEFAULT_CTA_LABELS.get(cta_type, "En savoir plus")
    ctx["unsubscribe_url"] = build_unsubscribe_url(prospect, request=request)
    ctx["privacy_url"] = build_privacy_url(prospect, request=request)
    return ctx


def render_predictneed_subject(email_variant, ctx):
    if email_variant and email_variant.subject_template:
        return _render(email_variant.subject_template, ctx)
    return _render("{{ company_name }} — une observation sur votre parcours visiteurs", ctx)


def _signature_lines(product):
    lines = [product.sender_name or product.name]
    if product.sender_name and product.sender_name.strip() != product.name.strip():
        lines.append(product.name)
    if product.sender_email:
        lines.append(product.sender_email)
    return lines


def render_predictneed_text(ctx, product, compliance_profile, prospect, email):
    greeting = f"Bonjour {ctx['first_name']}," if ctx["first_name"] else "Bonjour,"
    body_lines = [greeting, ""]
    if ctx["observation"]:
        body_lines.append(f"En regardant {ctx['company_name']}, j'ai remarqué : {ctx['observation']}.")
        body_lines.append("")
    if ctx["detected_problem"]:
        body_lines.append(ctx["detected_problem"])
        body_lines.append("")
    if ctx["value_proposition"]:
        body_lines.append(ctx["value_proposition"])
        body_lines.append("")
    if ctx["cta_target_url"]:
        body_lines.append(f"{ctx['cta_label']} : {ctx['cta_url']}")
        body_lines.append("")
    body_lines.append("Bien cordialement,")
    body_lines.extend(_signature_lines(product))
    body_lines.append("")
    body_lines.append("---")
    body_lines.append(render_compliance_footer_text(prospect, product, compliance_profile, ctx["unsubscribe_url"], ctx["privacy_url"], email))
    return "\n".join(body_lines)


def render_predictneed_html(ctx, product, compliance_profile, prospect, email):
    greeting = f"Bonjour {escape(ctx['first_name'])}," if ctx["first_name"] else "Bonjour,"
    logo_html = ""
    if product.logo_url:
        logo_html = f'<img src="{escape(product.logo_url)}" alt="{escape(product.name)}" height="28" style="height:28px;width:auto;display:block;margin-bottom:6px;border:0;">'

    paragraphs = []
    if ctx["observation"]:
        paragraphs.append(f"En regardant {escape(ctx['company_name'])}, j'ai remarqué : {escape(ctx['observation'])}.")
    if ctx["detected_problem"]:
        paragraphs.append(escape(ctx["detected_problem"]))
    if ctx["value_proposition"]:
        paragraphs.append(escape(ctx["value_proposition"]))

    body_paragraphs = "".join(
        f'<p style="margin:0 0 14px 0;font-size:15px;line-height:1.65;color:#1f2530;">{p}</p>' for p in paragraphs
    )

    cta_html = ""
    if ctx["cta_target_url"]:
        cta_html = (
            '<p style="margin:22px 0 0 0;">'
            f'<a href="{escape(ctx["cta_url"])}" style="display:inline-block;padding:11px 20px;'
            'background:#1f6feb;color:#ffffff;text-decoration:none;border-radius:8px;font-size:14px;font-weight:600;">'
            f'{escape(ctx["cta_label"])}</a></p>'
        )

    signature_html = "<br>".join(escape(line) for line in _signature_lines(product))

    return (
        '<!doctype html><html lang="fr"><body style="margin:0;background:#f4f5f7;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif;color:#1f2530;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e7eaf0;">'
        f'<tr><td style="padding:24px 28px 8px 28px;">{logo_html}'
        f'<div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#8b93a3;font-weight:600;">{escape(product.name)}</div>'
        "</td></tr>"
        f'<tr><td style="padding:8px 28px 4px 28px;">'
        f'<p style="margin:0 0 14px 0;font-size:15px;line-height:1.65;color:#1f2530;">{greeting}</p>'
        f"{body_paragraphs}{cta_html}"
        f'<p style="margin:26px 0 0 0;font-size:14px;line-height:1.6;color:#1f2530;">Bien cordialement,<br>{signature_html}</p>'
        "</td></tr>"
        f"{render_compliance_footer_html(prospect, product, compliance_profile, ctx['unsubscribe_url'], ctx['privacy_url'], email)}"
        "</table></td></tr></table></body></html>"
    )


def render_predictneed_email(campaign_prospect, email_step=None, email_variant=None, request=None):
    prospect = campaign_prospect.prospect
    product = campaign_prospect.campaign.product
    compliance_profile = getattr(product, "compliance_profile", None)
    email = prospect.public_email or ""

    ctx = build_predictneed_context(campaign_prospect, email_step=email_step, email_variant=email_variant, request=request)
    subject = render_predictneed_subject(email_variant, ctx)
    html = render_predictneed_html(ctx, product, compliance_profile, prospect, email)
    text = render_predictneed_text(ctx, product, compliance_profile, prospect, email)
    return subject, html, text


def send_predictneed_campaign_email(campaign_prospect, email_step=None, email_variant=None, request=None, is_test=False, test_recipient=""):
    """ETAPE 22/24/25 — envoi réel, avec re-vérification de l'opposition juste
    avant SMTP, identité d'expéditeur centralisée, Message-ID et List-Unsubscribe."""
    prospect = campaign_prospect.prospect
    campaign = campaign_prospect.campaign
    product = campaign.product

    subject, html, text = render_predictneed_email(campaign_prospect, email_step, email_variant, request)
    identity = get_sender_identity(product=product, campaign=campaign)
    to_email = test_recipient if is_test else (prospect.public_email or "")

    record = EmailSend.objects.create(
        prospect=prospect,
        campaign_prospect=campaign_prospect,
        email_step=email_step,
        email_variant=email_variant,
        from_email=identity["from_email"],
        reply_to_email=identity["reply_to"],
        to_email=to_email,
        subject=("[TEST] " + subject) if is_test else subject,
        html_body=html,
        text_body=text,
        status="draft",
        is_test=is_test,
    )

    if not to_email:
        record.status = "blocked"
        record.error = "Adresse destinataire manquante."
        record.save(update_fields=["status", "error"])
        return record

    if not is_test and is_suppressed(prospect.public_email, prospect=prospect):
        record.status = "suppressed"
        record.error = "Opposition détectée juste avant l'envoi."
        record.save(update_fields=["status", "error"])
        return record

    message_id = make_msgid(domain=(identity["from_email"].split("@", 1)[-1] or "predictneed-ia.com"))
    headers = {
        "Message-ID": message_id,
        "List-Unsubscribe": f"<{build_unsubscribe_url(prospect, request)}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    previous_send = (
        EmailSend.objects.filter(campaign_prospect=campaign_prospect, status="sent")
        .exclude(pk=record.pk).order_by("created_at").first()
    )
    if previous_send and previous_send.message_id:
        headers["In-Reply-To"] = previous_send.message_id
        headers["References"] = (previous_send.references + " " + previous_send.message_id).strip()
        record.in_reply_to = previous_send.message_id
        record.references = headers["References"]

    record.attempt_count += 1
    record.last_attempt_at = timezone.now()

    try:
        msg = EmailMultiAlternatives(
            subject=record.subject, body=text,
            from_email=format_from_header(identity),
            to=[to_email],
            reply_to=[identity["reply_to"]] if identity["reply_to"] else None,
            headers=headers,
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)

        record.status = "sent"
        record.sent_at = timezone.now()
        record.message_id = message_id
        record.save()

        if not is_test:
            campaign_prospect.status = "contacted" if campaign_prospect.status in ("identified", "selected", "ready_to_contact") else campaign_prospect.status
            campaign_prospect.contacted_at = campaign_prospect.contacted_at or timezone.now()
            campaign_prospect.save(update_fields=["status", "contacted_at"])
            prospect.predictneed_stage = "contacted"
            prospect.last_contacted_at = timezone.now()
            prospect.save(update_fields=["predictneed_stage", "last_contacted_at"])
            ContactLog.objects.create(prospect=prospect, channel="email", subject=record.subject, message=text, outcome="sent")
            EngagementEvent.objects.create(
                campaign_prospect=campaign_prospect, prospect=prospect, campaign=campaign,
                email_step=email_step, email_variant=email_variant,
                event_type="email_sent", source="prospectpilot",
                metadata={"email_send_id": record.pk},
            )
        return record
    except Exception as exc:
        record.status = "failed"
        record.error = str(exc)
        record.save()
        if not is_test:
            EngagementEvent.objects.create(
                campaign_prospect=campaign_prospect, prospect=prospect, campaign=campaign,
                email_step=email_step, email_variant=email_variant,
                event_type="email_failed", source="prospectpilot",
                metadata={"error": str(exc)[:500]},
            )
        return record
