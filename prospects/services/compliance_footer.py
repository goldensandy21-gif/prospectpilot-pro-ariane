"""ETAPE 6/14 (mission 2) — enveloppe obligatoire non supprimable.

render_compliance_footer() est appelé au moment RÉEL du rendu et de l'envoi, jamais
à partir du HTML enregistré par l'utilisateur : impossible de le supprimer en
modifiant le corps commercial dans l'admin ou l'aperçu.
"""
from html import escape

from .provenance import describe_provenance, get_email_provenance


def _context_lines(prospect, product, compliance_profile, email=""):
    org_name = (compliance_profile.organization_name if compliance_profile else "") or product.sender_brand_name or product.name
    lines = [
        f"Vous recevez ce message dans le cadre de votre activité professionnelle. "
        f"{org_name} utilise vos coordonnées professionnelles afin de vous présenter un service en lien avec votre activité.",
    ]
    if email:
        provenance = get_email_provenance(prospect, email)
        if provenance["source_type"] != "unknown":
            lines.append(describe_provenance(provenance))
    contact_email = (compliance_profile.contact_email if compliance_profile else "") or product.sender_email
    if contact_email:
        lines.append(f"Contact : {contact_email}.")
    return lines


def compliance_footer_lines(prospect, product, compliance_profile, unsubscribe_url, privacy_url, email=""):
    """Niveau 1 : texte court. Le détail complet vit sur la page de confidentialité (niveau 2)."""
    lines = _context_lines(prospect, product, compliance_profile, email)
    privacy_link = privacy_url or (compliance_profile.privacy_policy_url if compliance_profile else "")
    if privacy_link:
        lines.append(f"Confidentialité et vos droits : {privacy_link}")
    lines.append(f"Se désabonner (gratuit, immédiat, sans justification) : {unsubscribe_url}")
    if compliance_profile and compliance_profile.legal_notice_url:
        lines.append(f"Mentions légales : {compliance_profile.legal_notice_url}")
    return lines


def render_compliance_footer_text(prospect, product, compliance_profile, unsubscribe_url, privacy_url, email=""):
    return "\n".join(compliance_footer_lines(prospect, product, compliance_profile, unsubscribe_url, privacy_url, email))


def render_compliance_footer_html(prospect, product, compliance_profile, unsubscribe_url, privacy_url, email=""):
    context_lines = _context_lines(prospect, product, compliance_profile, email)
    paragraphs = "".join(
        f'<p style="margin:0 0 8px 0;font-size:11px;line-height:1.6;color:#7a8699;">{escape(line)}</p>'
        for line in context_lines
    )
    privacy_link = privacy_url or (compliance_profile.privacy_policy_url if compliance_profile else "")
    links = []
    if privacy_link:
        links.append(f'<a href="{escape(privacy_link)}" style="color:#4a5570;">Confidentialité et vos droits</a>')
    links.append(f'<a href="{escape(unsubscribe_url)}" style="color:#4a5570;">Se désabonner</a>')
    if compliance_profile and compliance_profile.legal_notice_url:
        links.append(f'<a href="{escape(compliance_profile.legal_notice_url)}" style="color:#4a5570;">Mentions légales</a>')
    links_html = (
        '<p style="margin:0;font-size:11px;line-height:1.6;color:#7a8699;">' + " · ".join(links) + "</p>"
    )
    return (
        '<tr><td style="padding:18px 28px 24px 28px;border-top:1px solid #e7eaf0;">'
        f"{paragraphs}{links_html}"
        "</td></tr>"
    )
