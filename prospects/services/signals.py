"""ETAPE 10/11 — Signaux business development et détection de concurrents.

Toute génération de signal est reliée à une preuve observée (quick scan, technologies
détectées) : on ne déduit jamais un signal sans donnée source.

Mission 6 : `signal_fingerprint()` est la fonction canonique de déduplication —
utilisée ici ET dans la migration 0007 (backfill) — pour ne jamais avoir deux
formules différentes. `CATEGORY_TO_GROUP` est le mapping canonique catégorie
technique -> rôle dans le scoring (FIT/INTENT/ENGAGEMENT/RISK), lui aussi
partagé entre ce module et la migration.
"""
import hashlib

from django.utils import timezone

from ..models import Competitor, CompetitorDetection, ProspectSignal, ProspectTechnology

ADVERTISING_TECHS = {"Google Ads", "Meta Pixel", "LinkedIn Insight Tag", "TikTok Pixel"}
ANALYTICS_TECHS = {"Google Analytics", "Google Analytics 4 (GA4)", "Google Tag Manager"}
CRM_TECHS = {"HubSpot", "Salesforce", "Pipedrive", "Brevo", "Mailchimp", "ActiveCampaign", "Klaviyo"}
BEHAVIOUR_ANALYTICS_TECHS = {"Hotjar", "Microsoft Clarity", "Contentsquare"}
VISITOR_INTELLIGENCE_TECHS = {"Dealfront / Leadfeeder", "Leadinfo"}

# Catégorie technique (nature du signal) -> rôle dans le scoring. Un outil
# détecté (analytics/advertising/crm/acquisition/competitor) est un indice de
# maturité structurelle (FIT), jamais d'intention d'achat à lui seul.
# "growth"/"conversion"/"timing" sont les catégories réellement temporelles
# retenues pour INTENT (mission 6, section 5).
CATEGORY_TO_GROUP = {
    "icp": "fit",
    "contactability": "fit",
    "analytics": "fit",
    "advertising": "fit",
    "crm": "fit",
    "acquisition": "fit",
    "competitor": "fit",
    "growth": "intent",
    "conversion": "intent",
    "timing": "intent",
    "risk": "risk",
}


def signal_fingerprint(signal_type, value="", evidence=""):
    """Identité de déduplication d'un signal (mission 6, section 3) : deux
    détections avec la même empreinte sont le même signal (rafraîchi, pas
    dupliqué) ; une empreinte différente — même `signal_type` — est un
    événement réellement distinct (nouvelle ligne conservée). Fonction pure,
    réutilisée telle quelle par la migration 0007 pour le backfill."""
    raw = f"{signal_type}|{(value or '').strip().lower()}|{(evidence or '').strip().lower()[:200]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _signal(
    prospect, signal_type, category, label, value="", source_url="", evidence="",
    confidence=70, score_impact=0, positive=True, source_kind="website", signal_group=None,
    observed_at=None,
):
    return ProspectSignal(
        prospect=prospect, signal_type=signal_type, category=category, label=label,
        value=value, source_url=source_url, evidence=evidence, confidence=confidence,
        score_impact=score_impact, positive=positive, source_kind=source_kind,
        signal_group=signal_group or CATEGORY_TO_GROUP.get(category, "fit"),
        observed_at=observed_at or timezone.now(),
        fingerprint=signal_fingerprint(signal_type, value, evidence),
    )


def build_signals_from_technologies(prospect, technologies, site_url=""):
    """technologies: liste de dicts {technology, category, source_url, evidence, confidence}."""
    signals = []
    names = {t["technology"] for t in technologies}

    if names & ANALYTICS_TECHS:
        detected = sorted(names & ANALYTICS_TECHS)
        signals.append(_signal(
            prospect, "analytics_detected", "analytics",
            "Outils d'analytics détectés", value=", ".join(detected),
            source_url=site_url, evidence=f"Technologies : {', '.join(detected)}",
            confidence=80, score_impact=6, positive=True, source_kind="technology",
        ))
        if "Google Tag Manager" in names:
            signals.append(_signal(
                prospect, "gtm_detected", "acquisition", "Google Tag Manager installé",
                source_url=site_url, evidence="GTM détecté sur le site.",
                confidence=85, score_impact=8, positive=True, source_kind="technology",
            ))

    ads = names & ADVERTISING_TECHS
    if ads:
        signals.append(_signal(
            prospect, "advertising_pixel_detected", "advertising",
            "Pixel publicitaire détecté", value=", ".join(sorted(ads)),
            source_url=site_url, evidence=f"Pixels : {', '.join(sorted(ads))}",
            confidence=82, score_impact=8, positive=True, source_kind="technology",
        ))

    crm = names & CRM_TECHS
    if crm:
        signals.append(_signal(
            prospect, "crm_detected", "crm", "CRM / marketing automation détecté",
            value=", ".join(sorted(crm)), source_url=site_url,
            evidence=f"Outils : {', '.join(sorted(crm))}",
            confidence=80, score_impact=6, positive=True, source_kind="technology",
        ))

    behaviour = names & BEHAVIOUR_ANALYTICS_TECHS
    if behaviour:
        # ETAPE 10 : un concurrent comme Hotjar/Clarity n'est pas négatif en soi —
        # il indique une forte maturité comportementale.
        signals.append(_signal(
            prospect, "behaviour_analytics_detected", "acquisition",
            "Outil d'analyse comportementale détecté", value=", ".join(sorted(behaviour)),
            source_url=site_url, evidence=f"Outils : {', '.join(sorted(behaviour))}",
            confidence=78, score_impact=5, positive=True, source_kind="technology",
        ))

    visitor_intel = names & VISITOR_INTELLIGENCE_TECHS
    if visitor_intel:
        signals.append(_signal(
            prospect, "visitor_intelligence_competitor_detected", "competitor",
            "Outil d'intelligence visiteurs B2B détecté (concurrent partiel)",
            value=", ".join(sorted(visitor_intel)), source_url=site_url,
            evidence=f"Outils : {', '.join(sorted(visitor_intel))}",
            confidence=78, score_impact=-4, positive=False, source_kind="technology",
        ))

    return signals


def build_signals_from_quick_scan(prospect, scan_data, site_url=""):
    signals = []
    if not scan_data:
        return signals

    def add(condition, signal_type, category, label, impact, positive=True, confidence=65):
        if condition:
            signals.append(_signal(
                prospect, signal_type, category, label, source_url=site_url,
                evidence=f"Détecté lors du quick scan ({scan_data.get('pages_checked', 0)} page(s) analysée(s)).",
                confidence=confidence, score_impact=impact, positive=positive,
            ))

    add(scan_data.get("has_contact_form"), "contact_form_detected", "conversion", "Formulaire de contact présent", 6)
    add(scan_data.get("has_booking"), "booking_detected", "conversion", "Prise de rendez-vous détectée", 6)
    add(scan_data.get("has_pricing_page"), "pricing_page_detected", "conversion", "Page tarifs détectée", 5)
    add(scan_data.get("has_services_page"), "services_page_detected", "conversion", "Page services/offres détectée", 4)
    add(scan_data.get("has_landing_pages"), "landing_pages_detected", "conversion", "Parcours de conversion structuré (landing pages)", 6)
    add(scan_data.get("has_signup"), "signup_form_detected", "conversion", "Formulaire d'inscription détecté", 5)
    add(scan_data.get("has_lead_magnet"), "lead_magnet_detected", "growth", "Lead magnet détecté (guide, livre blanc...)", 4)
    add(
        scan_data.get("business_type") == "formation", "training_organization_activity",
        "icp", "Activité de formation détectée sur le site", 5,
    )
    add(
        scan_data.get("business_type") == "agence", "marketing_agency_activity",
        "icp", "Activité d'agence marketing/web détectée", 5,
    )
    add(not scan_data.get("worth_full_analysis") and scan_data.get("pages_checked"), "weak_conversion_path", "risk", "Peu d'éléments de conversion détectés", 4, positive=False, confidence=55)

    return signals


def build_competitor_detections(prospect, technologies, site_url=""):
    names = {t["technology"] for t in technologies}
    competitor_lookup = {c.name: c for c in Competitor.objects.filter(active=True)}
    detections = []
    for technology_name in names:
        competitor = competitor_lookup.get(technology_name)
        if not competitor:
            continue
        detections.append(CompetitorDetection(
            prospect=prospect, competitor=competitor, source_url=site_url,
            evidence=f"Technologie '{technology_name}' détectée sur le site.",
            confidence=80,
        ))
    return detections


def persist_technologies(prospect, technologies):
    saved = []
    for tech in technologies:
        obj, _ = ProspectTechnology.objects.update_or_create(
            prospect=prospect, technology=tech["technology"],
            defaults={
                "category": tech["category"],
                "source_url": tech.get("source_url", ""),
                "evidence": tech.get("evidence", ""),
                "confidence": tech.get("confidence", 70),
                "is_active": True,
            },
        )
        saved.append(obj)
    return saved


def persist_signals(prospect, signal_objects):
    """Mission 6, section 3 : identité de déduplication = prospect + signal_type +
    source_kind + fingerprint (plus seulement prospect + signal_type). Une
    redétection strictement identique rafraîchit la même ligne (observed_at,
    last_checked_at) sans dupliquer. Un événement réellement différent — même
    signal_type — obtient un fingerprint différent et devient une nouvelle ligne,
    donc l'historique (ex. recrutement Growth en janvier, puis à nouveau en août)
    est conservé plutôt qu'écrasé."""
    now = timezone.now()
    saved = []
    for signal in signal_objects:
        obj, created = ProspectSignal.objects.update_or_create(
            prospect=prospect, signal_type=signal.signal_type,
            source_kind=signal.source_kind, fingerprint=signal.fingerprint,
            defaults={
                "category": signal.category, "signal_group": signal.signal_group,
                "label": signal.label, "value": signal.value,
                "source_url": signal.source_url, "evidence": signal.evidence,
                "confidence": signal.confidence, "score_impact": signal.score_impact,
                "positive": signal.positive, "last_checked_at": now,
                "observed_at": signal.observed_at or now,
            },
        )
        saved.append(obj)
    return saved


def persist_competitor_detections(prospect, detection_objects):
    saved = []
    for detection in detection_objects:
        obj, _ = CompetitorDetection.objects.update_or_create(
            prospect=prospect, competitor=detection.competitor,
            defaults={
                "source_url": detection.source_url, "evidence": detection.evidence,
                "confidence": detection.confidence,
            },
        )
        saved.append(obj)
    return saved
