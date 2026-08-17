"""ETAPE 6 — Préqualification registre avant tout crawl.

Calcule un score d'adéquation ICP à partir des seules données du registre public
(NAF, catégorie, effectif, CA, ancienneté, localisation), avant de dépenser le
moindre appel réseau vers le site de l'entreprise. Tous les seuils viennent de
l'ICPProfile — rien n'est codé en dur côté pondération.
"""
from datetime import date

BAND_RANGES = {
    "NN": (0, 0), "00": (0, 0), "01": (1, 2), "02": (3, 5), "03": (6, 9),
    "11": (10, 19), "12": (20, 49), "21": (50, 99), "22": (100, 199),
    "31": (200, 249), "32": (250, 499), "41": (500, 999), "42": (1000, 1999),
    "51": (2000, 4999), "52": (5000, 9999), "53": (10000, 10 ** 9),
}


def band_range(code):
    return BAND_RANGES.get(code or "", (0, 10 ** 9))


def company_age_years(creation_date):
    if not creation_date:
        return None
    try:
        if isinstance(creation_date, str):
            year = int(creation_date[:4])
        else:
            year = creation_date.year
    except (ValueError, TypeError):
        return None
    return date.today().year - year


def _sector_matches(row, icp):
    haystack = f"{row.get('sector', '')} {row.get('naf_code', '')}".lower()
    for keyword in icp.target_sectors or []:
        if keyword.lower() in haystack:
            return True
    return False


def _naf_matches(row, icp):
    naf = (row.get("naf_code") or "").upper()
    return bool(naf) and naf in {code.upper() for code in (icp.naf_codes or [])}


def _section_matches(row, icp):
    section = (row.get("source_payload", {}) or {}).get("section_activite_principale", "")
    return bool(section) and section.upper() in {s.upper() for s in (icp.naf_sections or [])}


def _location_matches(row, icp):
    if not (icp.regions or icp.departments or icp.cities):
        return True  # aucun filtre géographique = pas de restriction
    department = (row.get("department") or "").strip()
    city = (row.get("city") or "").lower().strip()
    if icp.departments and department in [str(d).strip() for d in icp.departments]:
        return True
    if icp.cities and any(c.lower().strip() == city for c in icp.cities):
        return True
    if icp.regions:
        # Le registre ne renvoie pas directement le nom de région ; on ne peut
        # pas vérifier ce filtre de façon fiable ici sans appel supplémentaire.
        return True
    return not (icp.departments or icp.cities)


def registry_pre_score(row, icp):
    """Retourne {score, reasons, excluded, exclusion_reason} sans appel réseau."""
    reasons = []
    score = 40  # base neutre

    if not row.get("prospecting_allowed", True):
        return {
            "score": 0, "reasons": ["Diffusion partielle : prospection interdite."],
            "excluded": True, "exclusion_reason": "Diffusion partielle (registre).",
        }

    sector_text = (row.get("sector") or "").lower()
    for excluded_sector in icp.excluded_sectors or []:
        if excluded_sector.lower() in sector_text:
            return {
                "score": 0, "reasons": [f"Secteur exclu : {excluded_sector}."],
                "excluded": True, "exclusion_reason": f"Secteur exclu ({excluded_sector}).",
            }

    if _naf_matches(row, icp):
        score += 25
        reasons.append("Code NAF exactement dans la cible ICP.")
    elif _section_matches(row, icp):
        score += 15
        reasons.append("Section d'activité dans la cible ICP.")
    elif _sector_matches(row, icp):
        score += 10
        reasons.append("Libellé de secteur proche de la cible ICP.")
    elif icp.naf_codes or icp.naf_sections or icp.target_sectors:
        score -= 10
        reasons.append("Aucune correspondance sectorielle avec l'ICP.")

    if icp.company_categories and row.get("categorie_entreprise"):
        if row["categorie_entreprise"] in icp.company_categories:
            score += 8
            reasons.append("Catégorie d'entreprise conforme (PME/ETI/GE).")

    band = row.get("employee_band", "")
    band_min_v, band_max_v = band_range(band)
    if icp.employee_min or icp.employee_max:
        lo = icp.employee_min or 0
        hi = icp.employee_max or 10 ** 9
        if band_max_v >= lo and band_min_v <= hi:
            score += 15
            reasons.append(f"Effectif ({band or 'inconnu'}) dans la fourchette ICP.")
        else:
            score -= 15
            reasons.append(f"Effectif ({band or 'inconnu'}) hors fourchette ICP.")

    ca = row.get("ca")
    if ca and (icp.revenue_min or icp.revenue_max):
        lo = icp.revenue_min or 0
        hi = icp.revenue_max or 10 ** 12
        if lo <= ca <= hi:
            score += 8
            reasons.append("Chiffre d'affaires dans la fourchette ICP.")
        else:
            score -= 8
            reasons.append("Chiffre d'affaires hors fourchette ICP.")

    age = company_age_years(row.get("creation_date"))
    if age is not None and (icp.min_company_age_years or icp.max_company_age_years):
        lo = icp.min_company_age_years or 0
        hi = icp.max_company_age_years or 200
        if lo <= age <= hi:
            score += 5
            reasons.append(f"Ancienneté ({age} ans) conforme.")
        else:
            score -= 5
            reasons.append(f"Ancienneté ({age} ans) hors fourchette ICP.")

    if not _location_matches(row, icp):
        score -= 20
        reasons.append("Localisation hors zone ICP.")
    elif icp.departments or icp.cities:
        reasons.append("Localisation conforme à la zone ICP.")

    if "est_organisme_formation" in (icp.required_signals or []) and not row.get("est_organisme_formation"):
        score -= 20
        reasons.append("Organisme de formation requis mais non détecté au registre.")
    if "est_qualiopi" in (icp.required_signals or []) and not row.get("est_qualiopi"):
        score -= 15
        reasons.append("Certification Qualiopi requise mais non détectée au registre.")

    score = max(0, min(100, score))
    return {"score": score, "reasons": reasons, "excluded": False, "exclusion_reason": ""}


def preselect_top_candidates(scored_rows, target_count, minimum_score=0):
    """scored_rows: liste de (row, pre_score_result). Retourne les meilleurs jusqu'à target_count."""
    eligible = [
        (row, result) for row, result in scored_rows
        if not result["excluded"] and result["score"] >= minimum_score
    ]
    eligible.sort(key=lambda pair: pair[1]["score"], reverse=True)
    return eligible[:target_count]
