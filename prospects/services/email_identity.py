"""ETAPE 22 (mission 2) — identité d'expéditeur centralisée.

Aucune campagne ne peut définir une adresse From arbitraire : la seule source
d'identité est ProductProfile (administré), vérifiée contre une whitelist
optionnelle (ALLOWED_SENDER_IDENTITIES)."""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _legacy_identity():
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "") or ""
    from_name = getattr(settings, "EMAIL_SENDER_NAME", "") or ""
    # DEFAULT_FROM_EMAIL peut déjà être au format "Nom <email>"
    if "<" in from_email and ">" in from_email:
        return {"from_name": from_name, "from_email": from_email, "reply_to": getattr(settings, "CONTACT_EMAIL", from_email)}
    return {
        "from_name": from_name,
        "from_email": from_email,
        "reply_to": getattr(settings, "CONTACT_EMAIL", from_email) or from_email,
    }


def _is_whitelisted(email):
    whitelist = getattr(settings, "ALLOWED_SENDER_IDENTITIES", [])
    if not whitelist:
        return True  # whitelist non configurée = pas de restriction supplémentaire
    normalized = (email or "").strip().lower()
    for entry in whitelist:
        candidate = entry.strip().lower()
        if candidate == normalized or normalized in candidate:
            return True
    return False


def get_sender_identity(product=None, campaign=None):
    """Retourne {from_name, from_email, reply_to} pour un envoi.

    - Avec un `product` (ou `campaign.product`) configuré : utilise son identité
      d'expédition (sender_name/sender_email/reply_to_email), après vérification
      whitelist.
    - Sans produit : comportement historique de ProspectPilot (inchangé).
    """
    product = product or (campaign.product if campaign else None)
    if not product:
        return _legacy_identity()

    from_email = product.sender_email or ""
    from_name = product.sender_name or product.sender_brand_name or product.name
    reply_to = product.reply_to_email or from_email

    if not from_email:
        logger.warning("ProductProfile %s sans sender_email configuré : repli sur l'identité ProspectPilot.", product.slug)
        return _legacy_identity()

    if not _is_whitelisted(from_email):
        logger.error(
            "Adresse d'expédition '%s' non présente dans ALLOWED_SENDER_IDENTITIES : envoi bloqué avec cette identité.",
            from_email,
        )
        return _legacy_identity()

    return {"from_name": from_name, "from_email": from_email, "reply_to": reply_to}


def format_from_header(identity):
    if identity["from_name"] and "<" not in identity["from_email"]:
        return f'{identity["from_name"]} <{identity["from_email"]}>'
    return identity["from_email"]
