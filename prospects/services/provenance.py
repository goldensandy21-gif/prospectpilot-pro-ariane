"""ETAPE 5 (mission 2) — provenance centralisée d'une adresse e-mail.

Ne jamais écrire "trouvé sur Internet" quand une source précise est disponible :
on relie systématiquement l'adresse à son PublicEmail / ProspectEvidence d'origine.
"""
SOURCE_TYPE_LABELS = {
    "website": "site professionnel public de l'entreprise",
    "contact_page": "page Contact du site professionnel public",
    "legal_notice": "mentions légales du site professionnel public",
    "manual": "ajout manuel dans ProspectPilot",
    "other": "source publique professionnelle",
}

EMAIL_TYPE_LABELS = {
    "personal": "Nominatif",
    "generic": "Générique",
    "unknown": "Type inconnu",
}


def get_email_provenance(prospect, email):
    normalized = (email or "").strip().lower()
    public_email = prospect.public_emails.filter(email__iexact=normalized).first()

    if public_email:
        source_type = public_email.source_type
        return {
            "email": public_email.email,
            "source_type": source_type,
            "source_name": public_email.source_name or SOURCE_TYPE_LABELS.get(source_type, "source publique professionnelle"),
            "source_url": public_email.source_url,
            "collected_at": public_email.found_at,
            "publicly_accessible": True,
            "confidence": public_email.confidence_score,
            "verification_status": public_email.verification_status,
            "email_type": EMAIL_TYPE_LABELS.get(public_email.email_type, "Type inconnu"),
        }

    evidence = prospect.evidence_items.filter(field_name="email", normalized_value=normalized).order_by("-confidence_score").first()
    if evidence:
        return {
            "email": evidence.value,
            "source_type": evidence.source.source_type if evidence.source else "other",
            "source_name": evidence.source.name if evidence.source else "source publique professionnelle",
            "source_url": evidence.source_url,
            "collected_at": evidence.collected_at,
            "publicly_accessible": True,
            "confidence": evidence.confidence_score,
            "verification_status": evidence.verification_status,
            "email_type": "Type inconnu",
        }

    return {
        "email": email,
        "source_type": "unknown",
        "source_name": "provenance non déterminée",
        "source_url": "",
        "collected_at": None,
        "publicly_accessible": False,
        "confidence": 0,
        "verification_status": "unverified",
        "email_type": "Type inconnu",
    }


def describe_provenance(provenance):
    """Phrase courte pour le footer de conformité / la page de transparence."""
    if provenance["source_type"] == "unknown":
        return "Provenance non déterminée."
    label = SOURCE_TYPE_LABELS.get(provenance["source_type"], provenance["source_name"])
    return f"Source : {label}."
