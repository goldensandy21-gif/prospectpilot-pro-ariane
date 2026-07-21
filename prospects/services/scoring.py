SECTOR_PROFILES = {
    "formation": {"booking":18,"form":18,"mobile":16,"seo":14,"fit":95},
    "beauté": {"booking":25,"form":14,"mobile":18,"seo":12,"fit":92},
    "restaurant": {"booking":22,"form":10,"mobile":20,"seo":12,"fit":88},
    "immobilier": {"booking":12,"form":20,"mobile":18,"seo":15,"fit":90},
    "fitness": {"booking":24,"form":12,"mobile":18,"seo":12,"fit":88},
    "conseil": {"booking":8,"form":24,"mobile":16,"seo":16,"fit":86},
    "commerce": {"booking":8,"form":15,"mobile":20,"seo":18,"fit":84},
}
DEFAULT_PROFILE = {"booking":10,"form":18,"mobile":17,"seo":14,"fit":72}

def profile_for(prospect):
    haystack = f"{prospect.sector} {prospect.naf_code}".lower()
    for key, profile in SECTOR_PROFILES.items():
        if key in haystack:
            return profile
    return DEFAULT_PROFILE

def calculate_scores(prospect, summary, pages):
    profile = profile_for(prospect)
    technical = 0
    commercial = 0
    reasons = []

    if not pages:
        return 0, 0, profile["fit"], 0, ["Aucune page analysable."]

    homepage = pages[0]
    if not homepage.has_https:
        technical += 12; reasons.append("HTTPS absent.")
    if not homepage.has_viewport:
        technical += profile["mobile"]; reasons.append("Configuration mobile absente.")
    if summary.missing_titles:
        technical += min(15, summary.missing_titles*4); reasons.append("Titres SEO manquants.")
    if summary.missing_descriptions:
        technical += min(12, summary.missing_descriptions*3); reasons.append("Méta-descriptions manquantes.")
    if summary.missing_h1:
        technical += min(10, summary.missing_h1*3); reasons.append("H1 manquants.")
    if summary.broken_links:
        technical += min(15, summary.broken_links*3); reasons.append("Liens cassés détectés.")
    if summary.images_without_alt:
        technical += min(10, summary.images_without_alt); reasons.append("Images sans texte alternatif.")
    if summary.average_response_ms > 2500:
        technical += 15; reasons.append("Site lent.")
    if summary.performance_score is not None and summary.performance_score < 50:
        technical += 15; reasons.append("Performance mobile faible.")

    has_contact_form = any(page.has_contact_form for page in pages)
    has_cta = any(page.has_cta for page in pages)
    has_booking = any(page.has_booking for page in pages)
    has_phone = any(page.has_phone for page in pages)
    has_email = any(page.has_email for page in pages)

    if not has_contact_form:
        commercial += profile["form"]; reasons.append("Formulaire de contact absent.")
    if not has_cta:
        commercial += 16; reasons.append("Appel à l’action absent ou faible.")
    if not has_booking:
        commercial += profile["booking"]; reasons.append("Réservation ou inscription en ligne non détectée.")
    if not has_phone:
        commercial += 7; reasons.append("Téléphone non détecté.")
    if not has_email:
        commercial += 7; reasons.append("E-mail non détecté.")
    if summary.seo_score is not None and summary.seo_score < 60:
        commercial += profile["seo"]; reasons.append("Visibilité SEO perfectible.")

    technical = min(100, technical)
    commercial = min(100, commercial)
    fit = profile["fit"]
    priority = min(100, round(technical*.42 + commercial*.38 + fit*.20))
    if not prospect.prospecting_allowed:
        priority = 0
        reasons.append("Prospection interdite ou diffusion partielle.")
    return technical, commercial, fit, priority, list(dict.fromkeys(reasons))
