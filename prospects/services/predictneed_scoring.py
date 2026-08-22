"""ETAPE 13 — Score PredictNeed IA.

predictneed_acquisition_score = combinaison pondérée (ICPProfile.weights) de :
icp_fit_score, need_score, acquisition_maturity_score, contactability_score,
timing_score, intent_score, engagement_score.

Correctif d'audit (section 7) : intent_score/engagement_score (Mission 6,
pondérés par fraîcheur) sont maintenant des composantes du score canonique
"Priorité" — pas un cinquième score concurrent. Avant de les lire,
score_prospect() les rafraîchit via recompute_acquisition_scores() pour ne
jamais utiliser des valeurs obsolètes.

Toutes les raisons du score sont enregistrées (predictneed_score_reasons) pour qu'un
utilisateur puisse comprendre exactement pourquoi une entreprise a obtenu 86/100.
"""
from urllib.parse import urlparse

from django.utils import timezone

from ..models import Suppression
from .prescoring import registry_pre_score

RELEVANT_ROLES = [
    "ceo", "dirigeant", "gérant", "président", "fondateur",
    "directeur marketing", "responsable marketing", "growth", "acquisition",
    "digital", "e-commerce", "commercial", "sales", "responsable formation",
]


def _clip(value, lo=0, hi=100):
    return max(lo, min(hi, round(value)))


def _prospect_to_row(prospect):
    return {
        "sector": prospect.sector,
        "naf_code": prospect.naf_code,
        "categorie_entreprise": "",
        "employee_band": prospect.employee_band,
        "ca": None,
        "creation_date": prospect.creation_date.isoformat() if prospect.creation_date else None,
        "department": prospect.department,
        "city": prospect.city,
        "prospecting_allowed": prospect.prospecting_allowed,
        "est_organisme_formation": False,
        "est_qualiopi": False,
        "source_payload": prospect.source_payload or {},
    }


def compute_icp_fit_score(prospect, icp):
    if not icp:
        return 50, ["Aucun ICP associé : score neutre."]
    result = registry_pre_score(_prospect_to_row(prospect), icp)
    return result["score"], result["reasons"]


def compute_need_score(prospect):
    reasons = []
    score = 50
    for signal in prospect.signals.filter(category__in=["conversion", "growth"]):
        score += signal.score_impact if signal.positive else -abs(signal.score_impact)
        reasons.append(f"{signal.label} ({'+' if signal.positive else '-'}{abs(signal.score_impact)})")
    for signal in prospect.signals.filter(category="risk"):
        score -= abs(signal.score_impact)
        reasons.append(f"{signal.label} (-{abs(signal.score_impact)})")
    return _clip(score), reasons


def compute_acquisition_maturity_score(prospect):
    reasons = []
    score = 35
    for signal in prospect.signals.filter(category__in=["analytics", "advertising", "crm"]):
        if signal.positive:
            score += signal.score_impact
            reasons.append(f"{signal.label} (+{signal.score_impact})")
    for signal in prospect.signals.filter(signal_type="behaviour_analytics_detected"):
        score += signal.score_impact
        reasons.append(f"{signal.label} (+{signal.score_impact}) — maturité comportementale, pas nécessairement négatif.")
    for detection in prospect.competitor_detections.select_related("competitor"):
        score += detection.competitor.scoring_bonus - detection.competitor.scoring_penalty
        reasons.append(
            f"Concurrent détecté : {detection.competitor.name} "
            f"(bonus {detection.competitor.scoring_bonus}, pénalité {detection.competitor.scoring_penalty})."
        )
    return _clip(score), reasons


def compute_contactability_score(prospect):
    reasons = []
    best_email = prospect.public_emails.filter(is_active=True).order_by("-confidence_score").first()
    if not best_email:
        return 0, ["Aucune adresse e-mail professionnelle exploitable."]

    score = 30
    if best_email.email_type == "personal" and best_email.verification_status in ("verified", "format_valid"):
        score += 30
        reasons.append("Adresse nominative professionnelle disponible.")
        contact = prospect.contact_people.filter(email__iexact=best_email.email).first()
        if contact and contact.job_title:
            title = contact.job_title.lower()
            if any(role in title for role in RELEVANT_ROLES):
                score += 25
                reasons.append(f"Fonction pertinente détectée : {contact.job_title}.")
    elif best_email.email_type == "generic":
        score += 15
        reasons.append("Adresse générique professionnelle (contact@, info@...).")
    else:
        score += 5
        reasons.append("Adresse de type inconnu — confiance limitée.")

    if best_email.verification_status == "invalid":
        return 0, ["Adresse e-mail jugée invalide (format ou MX)."]

    reasons.append(f"Score de confiance de l'adresse : {best_email.confidence_score}/100.")
    return _clip(score + best_email.confidence_score * 0.2), reasons


def compute_timing_score(prospect):
    reasons = []
    score = 50
    timing_signals = prospect.signals.filter(category="timing")
    if not timing_signals.exists():
        reasons.append("Aucun signal de timing détecté : score neutre par défaut.")
        return score, reasons
    for signal in timing_signals:
        score += signal.score_impact if signal.positive else -abs(signal.score_impact)
        reasons.append(f"{signal.label} ({'+' if signal.positive else '-'}{abs(signal.score_impact)})")
    return _clip(score), reasons


def _grade_for(score):
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _hard_exclusion(prospect, icp):
    if not prospect.prospecting_allowed:
        return "Prospection interdite (diffusion partielle ou opposition registre)."
    if prospect.status == "do_not_contact":
        return "Prospect en opposition (ne plus contacter)."
    if Suppression.objects.filter(active=True, prospect=prospect).exists():
        return "Prospect présent dans la liste d'opposition (Suppression)."
    if prospect.public_email and Suppression.objects.filter(active=True, email__iexact=prospect.public_email).exists():
        return "Adresse e-mail présente dans la liste d'opposition (Suppression)."
    if prospect.predictneed_stage == "paying":
        return "Déjà client PredictNeed IA : exclu de la prospection."
    if prospect.website and icp and icp.excluded_domains:
        domain = urlparse(prospect.website if "://" in prospect.website else f"https://{prospect.website}").netloc.lower().removeprefix("www.")
        if any(domain == excluded.lower().removeprefix("www.") for excluded in icp.excluded_domains):
            return f"Domaine exclu ({domain})."
    return ""


DEFAULT_WEIGHTS_FALLBACK = {
    "icp_fit": 25, "need": 15, "acquisition_maturity": 15,
    "contactability": 15, "timing": 5, "intent": 15, "engagement": 10,
}


def score_prospect(prospect, icp=None, product=None, persist=True):
    icp = icp or prospect.predictneed_icp
    exclusion_reason = _hard_exclusion(prospect, icp)

    # Rafraîchit intent_score/engagement_score AVANT de les lire —
    # sinon la "Priorité" utiliserait des valeurs obsolètes (correctif d'audit).
    from .acquisition_scores import recompute_acquisition_scores
    recompute_acquisition_scores(prospect, persist=persist)

    icp_fit, icp_reasons = compute_icp_fit_score(prospect, icp)
    need, need_reasons = compute_need_score(prospect)
    maturity, maturity_reasons = compute_acquisition_maturity_score(prospect)
    contactability, contact_reasons = compute_contactability_score(prospect)
    timing, timing_reasons = compute_timing_score(prospect)
    intent = prospect.intent_score
    engagement = prospect.engagement_score

    outbound_eligible = contactability > 0 and not exclusion_reason
    outbound_reason = "" if outbound_eligible else (exclusion_reason or "Aucune adresse e-mail exploitable.")

    if exclusion_reason:
        final_score = 0
        grade = "D"
        reasons = [f"EXCLUSION : {exclusion_reason}"]
    else:
        weights = icp.effective_weights() if icp else DEFAULT_WEIGHTS_FALLBACK
        final_score = _clip(
            icp_fit * weights["icp_fit"] / 100
            + need * weights["need"] / 100
            + maturity * weights["acquisition_maturity"] / 100
            + contactability * weights["contactability"] / 100
            + timing * weights["timing"] / 100
            + intent * weights.get("intent", 0) / 100
            + engagement * weights.get("engagement", 0) / 100
        )
        grade = _grade_for(final_score)
        reasons = (
            [f"ICP fit ({icp_fit}/100, poids {weights['icp_fit']:.0f}%) : " + "; ".join(icp_reasons[:4])]
            + [f"Besoin détecté ({need}/100, poids {weights['need']:.0f}%) : " + "; ".join(need_reasons[:4] or ["aucun signal spécifique"])]
            + [f"Maturité acquisition ({maturity}/100, poids {weights['acquisition_maturity']:.0f}%) : " + "; ".join(maturity_reasons[:4] or ["aucun signal spécifique"])]
            + [f"Contactabilité ({contactability}/100, poids {weights['contactability']:.0f}%) : " + "; ".join(contact_reasons[:4])]
            + [f"Timing ({timing}/100, poids {weights['timing']:.0f}%) : " + "; ".join(timing_reasons[:2])]
            + [f"Intent ({intent}/100, poids {weights.get('intent', 0):.0f}%) : " + "; ".join((prospect.intent_score_reasons or [])[:3] or ["aucun signal d'intention"])]
            + [f"Engagement ({engagement}/100, poids {weights.get('engagement', 0):.0f}%) : " + "; ".join((prospect.engagement_score_reasons or [])[:3] or ["aucun engagement enregistré"])]
        )

    result = {
        "icp_fit_score": icp_fit,
        "need_score": need,
        "acquisition_maturity_score": maturity,
        "contactability_score": contactability,
        "timing_score": timing,
        "predictneed_acquisition_score": final_score,
        "predictneed_grade": grade,
        "predictneed_score_reasons": reasons,
        "outbound_eligible": outbound_eligible,
        "outbound_ineligible_reason": outbound_reason,
        "predictneed_excluded": bool(exclusion_reason),
        "predictneed_exclusion_reason": exclusion_reason,
    }

    if persist:
        if icp:
            prospect.predictneed_icp = icp
        if product:
            prospect.predictneed_product = product
        for field, value in result.items():
            setattr(prospect, field, value)
        prospect.save(update_fields=list(result.keys()) + ["predictneed_icp", "predictneed_product", "updated_at"])

    return result
