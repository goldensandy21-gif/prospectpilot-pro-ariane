"""ETAPE 7/10 (mission 2) — nouveau renderer PredictNeed IA.

Séparé du moteur legacy (emailing.py / EMAIL_DESIGNS) : sobre, un seul CTA, pas
d'image Unsplash, texte + HTML systématiques. Le contenu commercial (observation,
angle) vient uniquement de AgentBrief/ProspectSignal — jamais inventé.
"""
import secrets
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
from .tracking import (
    build_open_tracking_url,
    build_privacy_url,
    build_test_unsubscribe_preview_url,
    build_tracking_url,
    build_unsubscribe_url,
    resolve_target_url,
    resolve_target_url_for_test,
)

DEFAULT_CTA_LABELS = {
    "simulator": "Tester le simulateur",
    "product": "Voir comment fonctionne PredictNeed",
    "signup": "Créer un compte",
    "reply": "Répondre à cet e-mail",
}

# Identité visuelle PredictNeed IA — bleu marine dominant, bleu clair en accent,
# blanc, gris très clair pour le footer. Aucune référence Plezia, aucune photo.
NAVY = "#0B1F3A"
ACCENT_BLUE = "#2F6FE0"
ACCENT_BLUE_SOFT = "#EAF1FE"
INK = "#1F2530"
INK_SOFT = "#5B6472"
BORDER = "#E2E6ED"
PAGE_BG = "#EEF1F5"

# Trois blocs bénéfices courts, formulés en observations/probabilités,
# jamais en certitude sur ce qu'un visiteur pense ou veut (voir
# services/message_guardrails.py::BLOCKED_CLAIM_PATTERNS — aucune de ces
# phrases fixes n'utilise "vous cherchez", "votre intention", etc.).
BENEFIT_BLOCKS = [
    (
        "Comportements observés & intentions probables",
        "Identifier les signaux qui peuvent indiquer ce qu'un visiteur semble rechercher.",
    ),
    (
        "Plus de clarté sur les parcours de conversion",
        "Repérer les étapes et signaux susceptibles d'influencer le passage à l'action.",
    ),
    (
        "Actions recommandées",
        "Transformer les signaux détectés en prochaines actions marketing ou commerciales.",
    ),
]


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


def _secondary_signal_line(agent_brief):
    """Section H (Round D) : le VRAI deuxième signal, distinct de celui déjà
    utilisé en J0 (_observation_line -> relevant_signals[0]). Chaîne vide si
    l'AgentBrief n'en contient pas au moins deux — jamais de fabrication."""
    if not agent_brief or not agent_brief.relevant_signals or len(agent_brief.relevant_signals) < 2:
        return ""
    return agent_brief.relevant_signals[1].get("label", "")


def build_predictneed_context(campaign_prospect, email_step=None, email_variant=None, request=None, is_test=False):
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
        "secondary_signal": _secondary_signal_line(agent_brief),
        "detected_problem": agent_brief.detected_need if agent_brief else "",
        "product_name": product.name,
        "product_url": product.website_url,
        "simulator_url": product.simulator_url,
        "signup_url": product.signup_url,
        "value_proposition": product.short_value_proposition,
    }
    # Section C (Round D, verrous production) : un e-mail is_test=True ne
    # doit jamais embarquer de lien capable de muter le vrai prospect — ni
    # le tracking_token réel (`/t/<token>/`), ni le vrai lien de
    # désabonnement (`/unsubscribe/<token>/`). Le contenu VISIBLE reste
    # identique (même labels, mêmes conditions d'affichage) ; seules les
    # URLs changent.
    if is_test:
        ctx["cta_url"] = resolve_target_url_for_test(campaign_prospect, cta_type)
        ctx["cta_target_url"] = ctx["cta_url"]
        ctx["unsubscribe_url"] = build_test_unsubscribe_preview_url(request=request)
    else:
        ctx["cta_url"] = build_tracking_url(campaign_prospect, cta_type=cta_type, email_step=email_step, email_variant=email_variant, request=request)
        ctx["cta_target_url"] = resolve_target_url(campaign_prospect, cta_type)
        ctx["unsubscribe_url"] = build_unsubscribe_url(prospect, request=request)
    ctx["cta_label"] = (email_variant.cta_label_override if email_variant else "") or DEFAULT_CTA_LABELS.get(cta_type, "En savoir plus")
    # La page de confidentialité est purement en lecture (aucune mutation) —
    # elle peut donc rester la vraie page, y compris pour un test.
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
    body_lines.append("Vous pouvez aussi simplement répondre à cet e-mail si vous avez une question.")
    body_lines.append("")
    body_lines.append("Bien cordialement,")
    body_lines.extend(_signature_lines(product))
    body_lines.append("")
    body_lines.append("---")
    body_lines.append(render_compliance_footer_text(prospect, product, compliance_profile, ctx["unsubscribe_url"], ctx["privacy_url"], email))
    return "\n".join(body_lines)


def _benefit_block_html(title, description):
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px 0;">'
        '<tr>'
        '<td width="34" valign="top" style="padding:0 12px 0 0;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="28" height="28" align="center" valign="middle" bgcolor="{ACCENT_BLUE_SOFT}" '
        f'style="background:{ACCENT_BLUE_SOFT};border-radius:6px;font-family:Arial,Helvetica,sans-serif;'
        f'font-size:14px;line-height:28px;color:{ACCENT_BLUE};font-weight:700;">→</td>'
        "</tr></table>"
        "</td>"
        '<td valign="top">'
        f'<p style="margin:0 0 2px 0;font-size:14px;line-height:1.4;font-weight:700;color:{NAVY};">{escape(title)}</p>'
        f'<p style="margin:0;font-size:13px;line-height:1.55;color:{INK_SOFT};">{escape(description)}</p>'
        "</td>"
        "</tr></table>"
    )


def _cta_button_html(ctx):
    if not ctx["cta_target_url"]:
        return ""
    cta_url = escape(ctx["cta_url"])
    cta_label = escape(ctx["cta_label"])
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 22px 0;">'
        '<tr><td align="center">'
        "<!--[if mso]>"
        f'<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" '
        f'href="{cta_url}" style="height:46px;v-text-anchor:middle;width:280px;" arcsize="10%" '
        f'strokecolor="{ACCENT_BLUE}" fillcolor="{ACCENT_BLUE}">'
        '<w:anchorlock/>'
        f'<center style="color:#ffffff;font-family:Arial,sans-serif;font-size:15px;font-weight:bold;">{cta_label}</center>'
        "</v:roundrect>"
        "<![endif]-->"
        "<!--[if !mso]><!-->"
        f'<a href="{cta_url}" style="background:{ACCENT_BLUE};border-radius:8px;color:#ffffff;'
        'display:inline-block;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;'
        'line-height:46px;padding:0 28px;text-align:center;text-decoration:none;-webkit-text-size-adjust:none;'
        f'mso-hide:all;">{cta_label}</a>'
        "<!--<![endif]-->"
        "</td></tr></table>"
    )


def _cta_link_html(ctx):
    """CTA discret (lien texte, pas de gros bouton) — utilisé J8/J14
    (section 6, audit correctif final) : une relance déjà avancée dans la
    séquence appelle un ton plus discret qu'un premier contact."""
    if not ctx["cta_target_url"]:
        return ""
    return (
        '<p style="margin:0 0 22px 0;font-size:14px;line-height:1.6;">'
        f'<a href="{escape(ctx["cta_url"])}" style="color:{ACCENT_BLUE};font-weight:600;text-decoration:underline;">'
        f'{escape(ctx["cta_label"])}</a></p>'
    )


def _reply_line_html():
    return (
        f'<p style="margin:0 0 26px 0;font-size:13px;line-height:1.6;color:{INK_SOFT};">'
        "Vous pouvez aussi simplement répondre à cet e-mail si vous avez une question.</p>"
    )


def _body_blocks_j0_html(ctx):
    """J0 — premier contact : template complet inchangé (observation +
    accroche encadrée + proposition de valeur + 3 blocs bénéfices + CTA)."""
    intro_html = ""
    if ctx["observation"]:
        intro_html = (
            f'<p style="margin:0 0 18px 0;font-size:15px;line-height:1.65;color:{INK};">'
            f'En regardant {escape(ctx["company_name"])}, nous avons observé : {escape(ctx["observation"])}.</p>'
        )

    # Accroche forte visuellement mise en avant — même donnée réelle que
    # ctx["detected_problem"] (AgentBrief.detected_need), jamais un texte
    # inventé pour l'occasion, seulement une mise en forme différente.
    headline_html = ""
    if ctx["detected_problem"]:
        headline_html = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px 0;">'
            f'<tr><td style="background:{ACCENT_BLUE_SOFT};border-left:3px solid {ACCENT_BLUE};'
            f'border-radius:6px;padding:16px 18px;">'
            f'<p style="margin:0;font-size:15px;line-height:1.6;font-weight:600;color:{NAVY};">'
            f'{escape(ctx["detected_problem"])}</p>'
            "</td></tr></table>"
        )

    value_prop_html = ""
    if ctx["value_proposition"]:
        value_prop_html = (
            f'<p style="margin:0 0 22px 0;font-size:15px;line-height:1.65;color:{INK};">{escape(ctx["value_proposition"])}</p>'
        )

    benefits_html = "".join(_benefit_block_html(title, desc) for title, desc in BENEFIT_BLOCKS)
    benefits_section = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 26px 0;padding:20px 20px 6px 20px;background:#fbfcfe;border:1px solid {BORDER};border-radius:8px;">'
        f"<tr><td>{benefits_html}</td></tr></table>"
    )

    return intro_html + headline_html + value_prop_html + benefits_section + _cta_button_html(ctx) + _reply_line_html()


def _body_blocks_j4_html(ctx):
    """J4 — rappel court (section 6) : ne répète JAMAIS les 3 blocs
    bénéfices ni l'accroche encadrée de J0 — un simple rappel bref de
    l'observation déjà envoyée, puis le même type de CTA."""
    recall_html = ""
    if ctx["observation"]:
        recall_html = (
            f'<p style="margin:0 0 20px 0;font-size:15px;line-height:1.65;color:{INK};">'
            f'Pour rappel, à propos de {escape(ctx["company_name"])} : {escape(ctx["observation"])}.</p>'
        )
    elif ctx["detected_problem"]:
        recall_html = (
            f'<p style="margin:0 0 20px 0;font-size:15px;line-height:1.65;color:{INK};">'
            f'Pour rappel, au sujet de {escape(ctx["company_name"])} : {escape(ctx["detected_problem"])}.</p>'
        )
    return recall_html + _cta_button_html(ctx) + _reply_line_html()


_FRICTION_HEDGE_SENTENCE = "Cela peut correspondre à un point de friction possible dans le parcours, sans certitude."


def _body_blocks_j8_html(ctx):
    """J8 — nouvel angle (section 6, affiné section H Round D) : jamais de
    concaténation en milieu de phrase avec `detected_problem` (peut être une
    phrase complète autonome, pas un fragment à mettre en minuscule au
    milieu d'une autre phrase). Utilise le VRAI deuxième signal
    (`secondary_signal`) s'il existe — jamais le même signal que le J0
    présenté comme « un autre ». À défaut d'un second signal réel, présente
    un autre angle de lecture du signal déjà évoqué, sans prétendre qu'il
    s'agit d'un signal différent. Toujours hedged (« peut correspondre »,
    « sans certitude »)."""
    parts = []
    if ctx["secondary_signal"]:
        parts.append(
            f'<p style="margin:0 0 12px 0;font-size:15px;line-height:1.65;color:{INK};">'
            f'Un autre signal observé chez {escape(ctx["company_name"])} : {escape(ctx["secondary_signal"])}.</p>'
        )
        parts.append(f'<p style="margin:0 0 20px 0;font-size:15px;line-height:1.65;color:{INK};">{_FRICTION_HEDGE_SENTENCE}</p>')
    elif ctx["detected_problem"]:
        parts.append(
            f'<p style="margin:0 0 12px 0;font-size:15px;line-height:1.65;color:{INK};">'
            f'Un autre angle à vérifier : {escape(ctx["detected_problem"])}</p>'
        )
        parts.append(f'<p style="margin:0 0 20px 0;font-size:15px;line-height:1.65;color:{INK};">{_FRICTION_HEDGE_SENTENCE}</p>')
    return "".join(parts) + _cta_link_html(ctx) + _reply_line_html()


def _body_blocks_j14_html(ctx):
    """J14 — dernière relance (section 6) : très court, indique
    explicitement qu'aucune autre relance automatique ne suivra."""
    last_html = (
        f'<p style="margin:0 0 20px 0;font-size:15px;line-height:1.65;color:{INK};">'
        "Dernier message automatique à ce sujet — je ne relancerai plus après celui-ci.</p>"
    )
    return last_html + _cta_link_html(ctx) + _reply_line_html()


_BODY_BUILDERS_HTML = {
    1: _body_blocks_j0_html,
    2: _body_blocks_j4_html,
    3: _body_blocks_j8_html,
    4: _body_blocks_j14_html,
}


def _body_blocks_html(ctx, step_order):
    return _BODY_BUILDERS_HTML.get(step_order, _body_blocks_j0_html)(ctx)


def render_predictneed_html(ctx, product, compliance_profile, prospect, email, open_tracking_url=None, step_order=1):
    greeting = f"Bonjour {escape(ctx['first_name'])}," if ctx["first_name"] else "Bonjour,"
    body_html = _body_blocks_html(ctx, step_order)
    signature_html = "<br>".join(escape(line) for line in _signature_lines(product))

    # Section H (automatisation email) — pixel d'ouverture indicatif, jamais
    # pour un envoi is_test (aucun open_tracking_url n'est alors fourni par
    # l'appelant). 1x1 transparent, en toute fin de corps, sans dépendance
    # requise au rendu du reste de l'email si l'image est bloquée.
    pixel_html = ""
    if open_tracking_url:
        pixel_html = f'<img src="{escape(open_tracking_url)}" width="1" height="1" alt="" style="display:block;border:0;">'

    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>'
        f'<body style="margin:0;padding:0;background:{PAGE_BG};'
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif;color:{INK};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE_BG};">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;'
        f'border:1px solid {BORDER};">'
        # Bandeau supérieur bleu marine — identité PredictNeed IA, aucune image.
        f'<tr><td style="background:{NAVY};padding:26px 32px;">'
        '<span style="font-family:Arial,Helvetica,sans-serif;font-size:14px;letter-spacing:1.5px;'
        'text-transform:uppercase;color:#ffffff;font-weight:700;">'
        f'{escape(product.name)}</span>'
        "</td></tr>"
        # Corps blanc.
        f'<tr><td style="padding:32px 32px 8px 32px;">'
        f'<p style="margin:0 0 18px 0;font-size:15px;line-height:1.65;color:{INK};">{greeting}</p>'
        f"{body_html}"
        f'<p style="margin:0 0 6px 0;font-size:14px;line-height:1.6;color:{INK};">Bien cordialement,<br>{signature_html}</p>'
        "</td></tr>"
        f"{render_compliance_footer_html(prospect, product, compliance_profile, ctx['unsubscribe_url'], ctx['privacy_url'], email)}"
        f"</table></td></tr></table>{pixel_html}</body></html>"
    )


def _body_lines_j0(ctx):
    lines = []
    if ctx["observation"]:
        lines += [f"En regardant {ctx['company_name']}, j'ai remarqué : {ctx['observation']}.", ""]
    if ctx["detected_problem"]:
        lines += [ctx["detected_problem"], ""]
    if ctx["value_proposition"]:
        lines += [ctx["value_proposition"], ""]
    if ctx["cta_target_url"]:
        lines += [f"{ctx['cta_label']} : {ctx['cta_url']}", ""]
    lines.append("Vous pouvez aussi simplement répondre à cet e-mail si vous avez une question.")
    return lines


def _body_lines_j4(ctx):
    lines = []
    if ctx["observation"]:
        lines += [f"Pour rappel, à propos de {ctx['company_name']} : {ctx['observation']}.", ""]
    elif ctx["detected_problem"]:
        lines += [f"Pour rappel, au sujet de {ctx['company_name']} : {ctx['detected_problem']}.", ""]
    if ctx["cta_target_url"]:
        lines += [f"{ctx['cta_label']} : {ctx['cta_url']}", ""]
    lines.append("N'hésitez pas à simplement répondre à cet e-mail si vous avez une question.")
    return lines


def _body_lines_j8(ctx):
    lines = []
    if ctx["secondary_signal"]:
        lines += [f"Un autre signal observé chez {ctx['company_name']} : {ctx['secondary_signal']}.", ""]
        lines += [_FRICTION_HEDGE_SENTENCE, ""]
    elif ctx["detected_problem"]:
        lines += [f"Un autre angle à vérifier : {ctx['detected_problem']}", ""]
        lines += [_FRICTION_HEDGE_SENTENCE, ""]
    if ctx["cta_target_url"]:
        lines += [f"{ctx['cta_label']} : {ctx['cta_url']}", ""]
    lines.append("Vous pouvez aussi simplement répondre à cet e-mail.")
    return lines


def _body_lines_j14(ctx):
    lines = ["Dernier message automatique à ce sujet — je ne relancerai plus après celui-ci.", ""]
    if ctx["cta_target_url"]:
        lines += [f"{ctx['cta_label']} : {ctx['cta_url']}", ""]
    lines.append("Vous pouvez bien sûr répondre à cet e-mail si vous avez une question.")
    return lines


_BODY_BUILDERS_TEXT = {
    1: _body_lines_j0,
    2: _body_lines_j4,
    3: _body_lines_j8,
    4: _body_lines_j14,
}


def _body_lines(ctx, step_order):
    return _BODY_BUILDERS_TEXT.get(step_order, _body_lines_j0)(ctx)


def render_predictneed_text(ctx, product, compliance_profile, prospect, email, step_order=1):
    greeting = f"Bonjour {ctx['first_name']}," if ctx["first_name"] else "Bonjour,"
    body_lines = [greeting, ""]
    body_lines.extend(_body_lines(ctx, step_order))
    body_lines.append("")
    body_lines.append("Bien cordialement,")
    body_lines.extend(_signature_lines(product))
    body_lines.append("")
    body_lines.append("---")
    body_lines.append(render_compliance_footer_text(prospect, product, compliance_profile, ctx["unsubscribe_url"], ctx["privacy_url"], email))
    return "\n".join(body_lines)


def inject_open_pixel(html_body, open_tracking_token, request=None):
    """Insère le pixel d'ouverture UNIQUEMENT au moment du VRAI envoi
    commercial (section 5, audit correctif final) — jamais dans le contenu
    préparé/testé (PlannedEmailContent.html_body reste pixel-free). Le token
    est généré par l'appelant à cet instant précis, jamais réutilisé depuis
    PlannedEmailContent : réutiliser un même pixel entre le test et l'envoi
    commercial risquerait un cache client (Gmail/Outlook) masquant
    l'ouverture réelle, et un faux open_count si le test est rouvert après
    coup."""
    pixel_url = build_open_tracking_url(open_tracking_token, request=request)
    pixel_html = f'<img src="{escape(pixel_url)}" width="1" height="1" alt="" style="display:block;border:0;">'
    if "</body>" in html_body:
        return html_body.replace("</body>", f"{pixel_html}</body>", 1)
    return html_body + pixel_html


def _test_safe_link_replacements(campaign_prospect, email_step, request=None):
    """Section C (Round D) : recalcule les VRAIS liens (mêmes fonctions,
    mêmes paramètres que lors du rendu figé => URL identique octet pour
    octet) afin de pouvoir les substituer par leurs équivalents test-safe
    juste avant un envoi is_test=True, SANS jamais re-rendre le texte
    visible lui-même (le contenu figé/testé/validé reste octet pour octet
    identique, seuls les liens techniques changent)."""
    prospect = campaign_prospect.prospect
    variant = email_step.variants.filter(active=True).first() if email_step else None
    cta_type = variant.cta_type if variant else "simulator"
    real_cta_url = build_tracking_url(campaign_prospect, cta_type=cta_type, email_step=email_step, email_variant=variant, request=request)
    real_unsubscribe_url = build_unsubscribe_url(prospect, request=request)
    test_cta_url = resolve_target_url_for_test(campaign_prospect, cta_type)
    test_unsubscribe_url = build_test_unsubscribe_preview_url(request=request)
    return {real_cta_url: test_cta_url, real_unsubscribe_url: test_unsubscribe_url}


def neutralize_test_links(html_body, text_body, campaign_prospect, email_step, request=None):
    """Applique `_test_safe_link_replacements` au contenu FIGÉ (frozen_content)
    avant un envoi is_test=True : le contenu figé/validé a été rendu une
    seule fois avec les VRAIS liens (nécessaire pour l'envoi commercial), un
    test ne doit jamais les recevoir tels quels. Remplace la forme HTML
    échappée (attributs href) et la forme brute (texte)."""
    replacements = _test_safe_link_replacements(campaign_prospect, email_step, request=request)
    for real_url, test_url in replacements.items():
        if not real_url:
            continue
        html_body = html_body.replace(escape(real_url), escape(test_url)).replace(real_url, test_url)
        text_body = text_body.replace(real_url, test_url)
    return html_body, text_body


def render_predictneed_email(campaign_prospect, email_step=None, email_variant=None, request=None, open_tracking_token=None, is_test=False):
    prospect = campaign_prospect.prospect
    product = campaign_prospect.campaign.product
    compliance_profile = getattr(product, "compliance_profile", None)
    email = prospect.public_email or ""
    step_order = email_step.order if email_step else 1

    ctx = build_predictneed_context(campaign_prospect, email_step=email_step, email_variant=email_variant, request=request, is_test=is_test)
    subject = render_predictneed_subject(email_variant, ctx)
    open_tracking_url = build_open_tracking_url(open_tracking_token, request=request) if open_tracking_token else None
    html = render_predictneed_html(ctx, product, compliance_profile, prospect, email, open_tracking_url=open_tracking_url, step_order=step_order)
    text = render_predictneed_text(ctx, product, compliance_profile, prospect, email, step_order=step_order)
    return subject, html, text


def send_predictneed_campaign_email(campaign_prospect, email_step=None, email_variant=None, request=None, is_test=False, test_recipient="", frozen_content=None, now=None):
    """ETAPE 22/24/25 — envoi réel, avec re-vérification de l'opposition juste
    avant SMTP, identité d'expéditeur centralisée, Message-ID et List-Unsubscribe.

    `frozen_content` (section E, automatisation email) : dict optionnel
    {"subject", "html_body", "text_body"} produit par
    services/email_automation.py::prepare_planned_content(). Quand fourni, le
    texte n'est PAS régénéré ici — c'est exactement le contenu approuvé et
    figé au moment de la validation humaine qui part en SMTP, jamais un
    nouveau rendu silencieux. Sans `frozen_content`, comportement inchangé
    (rendu live, comme avant cette section).

    Le pixel d'ouverture n'est JAMAIS présent dans `frozen_content["html_body"]`
    (section 5, audit correctif final) : pour un test (`is_test=True`), le
    HTML part tel quel, sans pixel. Pour un envoi commercial réel, un token
    unique est généré ICI et injecté seulement maintenant — jamais réutilisé
    depuis PlannedEmailContent — pour éviter tout cache client (Gmail/Outlook)
    sur le pixel du test, et tout faux `open_count` si le test est rouvert
    après l'envoi réel.

    `now` (section 6, retry/backoff) : uniquement utilisé pour calculer
    `next_retry_at` en cas d'échec, cohérent avec le `now` simulé par
    advance_campaign_prospect/run_planning_scheduler. Ne change aucun autre
    horodatage de cette fonction (sent_at, last_attempt_at... restent
    l'horloge réelle, comme avant cette section)."""
    prospect = campaign_prospect.prospect
    campaign = campaign_prospect.campaign
    product = campaign.product

    if frozen_content:
        subject = frozen_content["subject"]
        text = frozen_content["text_body"]
        if is_test:
            # Section C (Round D, verrous production) : le contenu figé a
            # été rendu une seule fois avec les VRAIS liens (nécessaire pour
            # l'envoi commercial) — un test ne doit jamais les recevoir tels
            # quels. Le texte visible reste octet pour octet identique.
            html, text = neutralize_test_links(frozen_content["html_body"], text, campaign_prospect, email_step, request=request)
            open_tracking_token = ""
        else:
            open_tracking_token = secrets.token_urlsafe(32)
            html = inject_open_pixel(frozen_content["html_body"], open_tracking_token, request=request)
    else:
        # Pixel d'ouverture (section H) : jamais pour un envoi de test, pour
        # ne polluer aucune donnée commerciale ; token opaque non séquentiel.
        # Section C (Round D) : is_test=True fait rendre directement des
        # liens test-safe (build_predictneed_context), pas de post-traitement
        # nécessaire ici.
        open_tracking_token = "" if is_test else secrets.token_urlsafe(32)
        subject, html, text = render_predictneed_email(
            campaign_prospect, email_step, email_variant, request,
            open_tracking_token=open_tracking_token or None, is_test=is_test,
        )

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
        open_tracking_token=open_tracking_token,
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
    headers = {"Message-ID": message_id}
    if not is_test:
        # Round E, point 1 : un e-mail is_test=True ne doit JAMAIS porter le
        # vrai List-Unsubscribe du prospect — un client mail (Gmail,
        # Outlook...) propose sinon une désinscription en un clic (RFC 8058)
        # d'un vrai prospect à partir d'un simple test. Aucun en-tête
        # List-Unsubscribe/List-Unsubscribe-Post n'est émis du tout pour un
        # test plutôt que d'y mettre un lien neutralisé : ce sont des
        # en-têtes réglementaires réels, jamais un simulacre. Comportement
        # commercial inchangé.
        headers["List-Unsubscribe"] = f"<{build_unsubscribe_url(prospect, request)}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    # Section D (Round D, verrous production) : le threading (In-Reply-To/
    # References) ne doit JAMAIS référencer un envoi de test — un test J0
    # ne doit jamais faire croire au vrai J0 qu'il répond à quelque chose,
    # et un Message-ID de test ne doit JAMAIS apparaître dans les en-têtes
    # d'un email commercial. On ne construit donc ce threading que pour un
    # envoi commercial réel (`not is_test`), et on ne considère QUE les
    # envois commerciaux réussis (`is_test=False, status="sent"`) — le plus
    # RÉCENT (`-created_at`), pas le plus ancien : J8 doit répondre au vrai
    # J4, jamais toujours au vrai J0.
    previous_send = None
    if not is_test:
        previous_send = (
            EmailSend.objects.filter(campaign_prospect=campaign_prospect, status="sent", is_test=False)
            .exclude(pk=record.pk).order_by("-created_at").first()
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
        if not is_test and campaign_prospect.campaign.planning_managed:
            # Section 6 (correctif automatisation) — backoff réel, jamais
            # une rafale de tentatives toutes les 5 minutes. Scopé aux
            # campagnes planning_managed pour ne rien changer au
            # comportement existant des campagnes manuelles.
            from .email_automation import finalize_failed_send
            record = finalize_failed_send(record, campaign_prospect, email_step, now=now)
        record.save()
        if not is_test:
            EngagementEvent.objects.create(
                campaign_prospect=campaign_prospect, prospect=prospect, campaign=campaign,
                email_step=email_step, email_variant=email_variant,
                event_type="email_failed", source="prospectpilot",
                metadata={"error": str(exc)[:500]},
            )
        return record
