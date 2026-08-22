"""Mission 7D — Email Finder à 3 niveaux, jamais de faux "vérifié" :

Niveau A — l'adresse a été vue telle quelle sur une page publique (déjà géré
par crawler.py/enrichment.py : reclassifiée ici en "public_source_confirmed").
Niveau B — motif de domaine déduit à partir d'au moins deux adresses
personnelles CONFIRMÉES qui s'accordent sur le même motif ; jamais présenté
comme trouvé ou vérifié, toujours marqué "pattern_inferred", jamais écrit
dans ContactPerson.email/PublicEmail.
Niveau C — vérification MX du domaine (DNS, jamais un envoi réel) ; prouve
seulement que le domaine peut recevoir du courrier, jamais que la boîte
existe précisément -> ne devient jamais "vérifié".

Réutilise ProspectEvidence/PublicEmail/ContactPerson existants — aucun
nouveau modèle, aucune deuxième logique de validation de format email
(verify_email() de enrichment.py reste la seule source de vérité pour ça).
"""
import re

import dns.resolver

from ..models import ProspectEvidence
from .enrichment import normalize_value, verify_email

PATTERN_TEMPLATES = {
    "first.last": lambda first, last: f"{first}.{last}" if first and last else "",
    "f.last": lambda first, last: f"{first[0]}.{last}" if first and last else "",
    "first": lambda first, last: first,
    "firstlast": lambda first, last: f"{first}{last}" if first and last else "",
}

UPGRADABLE_TO_MX = {"format_valid", "deliverability_unknown"}


def classify_public_source_email(email):
    """Reclasse le résultat de verify_email() (enrichment.py) au niveau A —
    jamais une deuxième logique de format. Un e-mail sur domaine gratuit
    reste "deliverability_unknown" (déjà honnête), un format invalide reste
    "invalid"."""
    check = verify_email(email)
    if check["status"] == "format_valid":
        return {**check, "status": "public_source_confirmed"}
    return check


def verify_mx(domain, timeout=4):
    """True si le domaine a un enregistrement MX, False s'il n'en a
    explicitement aucun, None si indéterminable (timeout, erreur réseau) —
    jamais confondu avec False."""
    domain = (domain or "").strip().lower()
    if not domain:
        return None
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
        return len(answers) > 0
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except Exception:
        return None


def upgrade_email_verification_with_mx(prospect):
    """Niveau C, action explicite (jamais automatique/silencieuse) : pour
    chaque PublicEmail encore à un statut faible, vérifie le MX du domaine et
    passe à "domain_mx_valid" si un MX existe. Ne rétrograde jamais un statut
    déjà plus fort, et ne déclare jamais "vérifié" sur la seule base du MX."""
    updated = []
    domain_cache = {}
    for public_email in prospect.public_emails.filter(is_active=True, verification_status__in=UPGRADABLE_TO_MX):
        domain = public_email.email.rsplit("@", 1)[-1]
        if domain not in domain_cache:
            domain_cache[domain] = verify_mx(domain)
        if domain_cache[domain] is True:
            public_email.verification_status = "domain_mx_valid"
            public_email.save(update_fields=["verification_status"])
            updated.append(public_email)
    return updated


def _split_name(contact):
    parts = [p for p in re.split(r"\s+", (contact.full_name or "").strip()) if p]
    if len(parts) >= 2:
        return parts[0].lower(), parts[-1].lower()
    if contact.first_name and contact.last_name:
        return contact.first_name.lower(), contact.last_name.lower()
    return "", ""


def _prospect_domain(prospect):
    website = (prospect.website or "").strip().lower()
    if not website:
        return ""
    return website.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")


def _detect_pattern_for_email(first, last, email):
    local = email.split("@", 1)[0].lower()
    for pattern_name, builder in PATTERN_TEMPLATES.items():
        candidate = builder(first, last)
        if candidate and candidate == local:
            return pattern_name
    return None


def infer_domain_email_pattern(prospect, minimum_examples=2):
    """Motif dominant de domaine, seulement si au moins `minimum_examples`
    adresses personnelles déjà connues du même domaine s'accordent sur le
    même motif. Retourne None sinon — jamais un motif deviné à partir d'un
    seul exemple (mission : "plusieurs emails... montrent un pattern
    cohérent")."""
    domain = _prospect_domain(prospect)
    if not domain:
        return None

    votes = {}
    examples_by_pattern = {}
    for contact in prospect.contact_people.filter(is_active=True).exclude(email=""):
        # Audit correctif §6 — un ContactPerson créé UNIQUEMENT en découpant
        # l'adresse e-mail elle-même (split_person_from_email, enrichment.py)
        # ne constitue pas une preuve indépendante du motif : "sales.europe@"
        # ou "marketing.team@" produiraient sinon un "nom" qui matche le
        # motif par construction (circularité). Exige un contact étayé
        # autrement (schema.org, page équipe...).
        if (contact.raw_payload or {}).get("inferred_from_email"):
            continue
        if not contact.email.lower().endswith("@" + domain):
            continue
        first, last = _split_name(contact)
        if not first or not last:
            continue
        pattern = _detect_pattern_for_email(first, last, contact.email)
        if pattern:
            votes[pattern] = votes.get(pattern, 0) + 1
            examples_by_pattern.setdefault(pattern, []).append(contact.pk)

    if not votes:
        return None
    best_pattern = max(votes, key=votes.get)
    count = votes[best_pattern]
    if count < minimum_examples:
        return None
    return {
        "pattern": best_pattern, "domain": domain, "confirmed_examples": count,
        "based_on_contact_ids": examples_by_pattern[best_pattern],
    }


def propose_inferred_email(prospect, contact, pattern_info=None):
    """Niveau B pour un ContactPerson précis. N'écrit JAMAIS dans
    ContactPerson.email ni PublicEmail (ce qui impliquerait une donnée
    trouvée/vérifiée) — seulement une ProspectEvidence explicitement marquée
    pattern_inferred, avec le motif et les exemples ayant servi de base.
    Retourne None si aucun motif fiable ou si le contact a déjà un e-mail."""
    if contact.email:
        return None
    pattern_info = pattern_info or infer_domain_email_pattern(prospect)
    if not pattern_info:
        return None
    first, last = _split_name(contact)
    if not first or not last:
        return None
    local = PATTERN_TEMPLATES[pattern_info["pattern"]](first, last)
    if not local:
        return None
    candidate_email = f"{local}@{pattern_info['domain']}"

    evidence, _ = ProspectEvidence.objects.update_or_create(
        prospect=prospect, field_name="email_pattern_inferred",
        normalized_value=normalize_value(candidate_email),
        defaults={
            "value": candidate_email, "value_type": "email", "confidence_score": 35,
            "verification_status": "pattern_inferred",
            "notes": (
                f"Motif « {pattern_info['pattern']} » déduit de "
                f"{pattern_info['confirmed_examples']} adresse(s) confirmée(s) du domaine — "
                "non confirmé, ne jamais présenter comme vérifié."
            ),
            "raw_payload": {**pattern_info, "contact_id": contact.pk, "contact_full_name": contact.full_name},
            "is_current": True,
        },
    )
    return evidence


def propose_inferred_emails_for_prospect(prospect):
    """Propose un e-mail probable pour chaque ContactPerson sans e-mail connu
    du prospect, si un motif fiable existe. Sûr à appeler automatiquement en
    fin d'enrichissement : n'écrit jamais ailleurs qu'en ProspectEvidence
    explicitement pattern_inferred."""
    pattern_info = infer_domain_email_pattern(prospect)
    if not pattern_info:
        return []
    created = []
    for contact in prospect.contact_people.filter(is_active=True, email=""):
        evidence = propose_inferred_email(prospect, contact, pattern_info=pattern_info)
        if evidence:
            created.append(evidence)
    return created
