"""ETAPE 14 — Agent Brief.

Le brief est entièrement composé à partir de données stockées (ProspectSignal,
ProspectTechnology, CompetitorDetection, ContactPerson, PublicEmail) : aucune phrase
n'invente une observation qui ne serait pas rattachée à une preuve en base.
"""
from ..models import AgentBrief
from .next_best_action import compute_next_best_action
from .predictneed_scoring import score_prospect


def _contact_recommendation(prospect):
    email = prospect.public_emails.filter(is_active=True).order_by("-is_primary", "-confidence_score").first()
    if not email:
        return "", "Aucune adresse e-mail professionnelle exploitable détectée."

    person = prospect.contact_people.filter(email__iexact=email.email).first()
    if person and person.full_name:
        reason = f"{person.job_title or 'Contact'} identifié via {email.source_type or 'site officiel'} ({email.source_url or 'source non précisée'})."
        return f"{person.full_name} <{email.email}>", reason

    label = "Adresse nominative professionnelle" if email.email_type == "personal" else "Adresse générique professionnelle"
    reason = f"{label}, source : {email.source_name or email.source_type} ({email.source_url or 'URL non précisée'})."
    return email.email, reason


# Quality gate (audit correctif) — ces deux textes sont des messages de
# repli INTERNES, écrits pour un opérateur qui relit un AgentBrief (ils
# expliquent honnêtement qu'aucun signal spécifique n'a été confirmé) —
# jamais destinés à apparaître tels quels dans un e-mail reçu par un
# prospect. predictneed_email.py les détecte via ces constantes pour omettre
# proprement le bloc "problème détecté" plutôt que de les afficher (voir
# _is_internal_fallback_need ci-dessous et son usage dans build_predictneed_context).
GENERIC_FALLBACK_NEED_PREFIX = "Aucun signal spécifique confirmé chez ce prospect."
INSUFFICIENT_SIGNAL_NEED_TEXT = "Signaux insuffisants pour formuler un besoin détecté précis."


def _detected_need(prospect, product):
    """Correctif d'audit (section 16) : `product.target_problem` est un
    énoncé GÉNÉRIQUE au niveau du produit — il ne doit jamais être présenté
    tel quel comme un besoin DÉTECTÉ chez CE prospect sans preuve
    correspondante. On sépare toujours : signaux INTENT réellement observés
    chez ce prospect (préférés, cités nommément) vs. problème générique du
    produit (explicitement étiqueté comme tel, jamais présenté comme
    "détecté")."""
    intent_signals = list(prospect.signals.filter(signal_group="intent", positive=True)[:3])
    if intent_signals:
        labels = ", ".join(s.label for s in intent_signals)
        return f"Signaux observés chez ce prospect pouvant indiquer un besoin : {labels}."
    if product and product.target_problem:
        return (
            f"{GENERIC_FALLBACK_NEED_PREFIX} Problème générique "
            f"adressé par {product.name} : {product.target_problem}"
        )
    return INSUFFICIENT_SIGNAL_NEED_TEXT


def _recommended_angle(prospect, competitor_detections):
    for detection in competitor_detections:
        if detection.competitor.suggested_angle:
            return detection.competitor.suggested_angle
    if prospect.signals.filter(category="advertising", positive=True).exists():
        return "L'entreprise investit déjà dans l'acquisition payante : mettre en avant la priorisation des visiteurs à fort potentiel."
    if prospect.signals.filter(category="conversion", positive=True).exists():
        return "Un parcours de conversion existe déjà : mettre en avant l'identification des visiteurs les plus engagés."
    return "Angle générique : présenter PredictNeed comme moyen de mieux prioriser les visiteurs déjà qualifiés."


def generate_agent_brief(prospect, icp=None, product=None, campaign=None, persist=True):
    icp = icp or prospect.predictneed_icp
    # score_prospect() rafraîchit intent_score/engagement_score en interne
    # avant de calculer "Priorité" (correctif d'audit) — donc déjà à jour
    # ici pour le calcul de la NBA juste après.
    score_result = score_prospect(prospect, icp=icp, product=product, persist=persist)

    positive_signals = list(prospect.signals.filter(positive=True).order_by("-score_impact")[:8])
    risk_signals = list(prospect.signals.filter(category="risk")[:5])
    competitor_detections = list(prospect.competitor_detections.select_related("competitor")[:5])
    technologies = list(prospect.technologies.filter(is_active=True)[:10])

    company_summary = (
        f"{prospect.name} — {prospect.sector or 'secteur non précisé'}, "
        f"{prospect.city or 'ville non précisée'} "
        f"({prospect.employee_band or 'effectif inconnu'})."
    )

    why_this_company = "; ".join(f"{s.label}" for s in positive_signals) or "Correspond aux critères de l'ICP sélectionné."

    detected_need = _detected_need(prospect, product)

    tech_names = ", ".join(sorted({t.technology for t in technologies})) or "aucune technologie détectée"
    acquisition_maturity = f"Score de maturité acquisition : {score_result['acquisition_maturity_score']}/100. Technologies détectées : {tech_names}."

    recommended_contact, recommended_contact_reason = _contact_recommendation(prospect)

    evidence_urls = sorted({
        *(s.source_url for s in positive_signals + risk_signals if s.source_url),
        *(t.source_url for t in technologies if t.source_url),
        *(d.source_url for d in competitor_detections if d.source_url),
    })

    # Mission 6, section 12 : NBA structurée (code + raison + confiance + signal
    # déclencheur), composée ici en une seule phrase pour le champ existant
    # AgentBrief.next_best_action — aucun second système de recommandation.
    nba = compute_next_best_action(prospect)
    next_best_action = f"{nba['code']} — {nba['reason']}"[:255]

    defaults = {
        "product": product,
        "icp": icp,
        "company_summary": company_summary,
        "why_this_company": why_this_company,
        "detected_need": detected_need,
        "acquisition_maturity": acquisition_maturity,
        "relevant_signals": [
            {"label": s.label, "category": s.category, "positive": s.positive, "source_url": s.source_url}
            for s in positive_signals + risk_signals
        ],
        "competitors_detected": [
            {"name": d.competitor.name, "category": d.competitor.category, "angle": d.competitor.suggested_angle}
            for d in competitor_detections
        ],
        "recommended_contact": recommended_contact,
        "recommended_contact_reason": recommended_contact_reason,
        "recommended_angle": _recommended_angle(prospect, competitor_detections),
        "objections_or_risks": [s.label for s in risk_signals] or (
            [] if score_result["outbound_eligible"] else [score_result["outbound_ineligible_reason"]]
        ),
        "suggested_cta": product.primary_cta_label if product else "",
        "score": score_result["predictneed_acquisition_score"],
        "score_reasons": score_result["predictneed_score_reasons"],
        "evidence_urls": evidence_urls,
        "next_best_action": next_best_action,
    }

    if not persist:
        return AgentBrief(prospect=prospect, **defaults)

    brief = AgentBrief.objects.create(prospect=prospect, **defaults)
    return brief
