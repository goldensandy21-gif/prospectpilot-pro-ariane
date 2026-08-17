"""Client pour l'API Recherche d'entreprises (recherche-entreprises.api.gouv.fr).

Source de vérité : https://recherche-entreprises.api.gouv.fr/docs/
Ce module respecte les limites du service : requêtes espacées (rate limiting interne),
retry avec backoff exponentiel, gestion explicite de 429/Retry-After, timeouts, et un
User-Agent descriptif (voir settings.USER_AGENT).
"""
import csv
import io
import logging
import re
import threading
import time

import httpx
from django.conf import settings
from openpyxl import Workbook

logger = logging.getLogger(__name__)

BASE_URL = "https://recherche-entreprises.api.gouv.fr/search"
MAX_PER_PAGE = 25

# Paramètres API supportés (ETAPE 5) — voir la documentation officielle.
_MULTI_VALUE_PARAMS = (
    "activite_principale", "code_postal", "code_commune", "departement",
    "region", "epci", "nature_juridique", "section_activite_principale",
    "tranche_effectif_salarie",
)
_BOOLEAN_PARAMS = (
    "est_organisme_formation", "est_qualiopi", "est_association", "est_ess",
    "est_entrepreneur_individuel", "est_rge", "est_bio", "est_societe_mission",
    "sort_by_size", "minimal",
)
_SCALAR_PARAMS = (
    "q", "categorie_entreprise", "etat_administratif", "ca_min", "ca_max",
    "resultat_net_min", "resultat_net_max", "code_collectivite_territoriale",
    "nom_personne", "prenoms_personne", "include",
)


class RateLimiter:
    """Espacement conservateur entre appels — l'API est publique et gratuite,
    on ne demande jamais plus vite qu'un seuil raisonnable et configurable."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call = time.monotonic()


_rate_limiter = RateLimiter(float(getattr(settings, "SEARCH_API_MIN_INTERVAL", 0.25) or 0.25))


def normalize_naf(value):
    value = (value or "").strip().upper().replace(" ", "")
    compact = value.replace(".", "")
    if re.fullmatch(r"\d{4}[A-Z]", compact):
        return f"{compact[:2]}.{compact[2:]}"
    return value


def _join_multi(value):
    if value in (None, ""):
        return ""
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def build_params(
    query="", naf_code="", postal_code="", department="", page=1, per_page=25,
    naf_codes=None, section_activite_principale=None, categorie_entreprise="",
    code_commune=None, region=None, tranche_effectif_salarie=None,
    ca_min=None, ca_max=None, est_organisme_formation=None, est_qualiopi=None,
    nature_juridique=None, etat_administratif="A", sort_by_size=None,
    minimal=None, include=None, **extra
):
    params = {
        "page": max(1, int(page or 1)),
        "per_page": min(MAX_PER_PAGE, int(per_page or MAX_PER_PAGE)),
    }
    if etat_administratif:
        params["etat_administratif"] = etat_administratif
    if query:
        params["q"] = query.strip()

    naf_values = list(naf_codes or [])
    single_naf = normalize_naf(naf_code)
    if single_naf:
        naf_values.append(single_naf)
    naf_joined = _join_multi([normalize_naf(v) for v in naf_values if v])
    if naf_joined:
        params["activite_principale"] = naf_joined

    if postal_code:
        params["code_postal"] = _join_multi(postal_code)
    if code_commune:
        params["code_commune"] = _join_multi(code_commune)
    if department:
        params["departement"] = _join_multi(department)
    if region:
        params["region"] = _join_multi(region)
    if section_activite_principale:
        params["section_activite_principale"] = _join_multi(section_activite_principale)
    if categorie_entreprise:
        params["categorie_entreprise"] = categorie_entreprise
    if tranche_effectif_salarie:
        params["tranche_effectif_salarie"] = _join_multi(tranche_effectif_salarie)
    if ca_min not in (None, ""):
        params["ca_min"] = int(ca_min)
    if ca_max not in (None, ""):
        params["ca_max"] = int(ca_max)
    if est_organisme_formation is not None:
        params["est_organisme_formation"] = bool(est_organisme_formation)
    if est_qualiopi is not None:
        params["est_qualiopi"] = bool(est_qualiopi)
    if nature_juridique:
        params["nature_juridique"] = _join_multi(nature_juridique)
    if sort_by_size is not None:
        params["sort_by_size"] = bool(sort_by_size)
    if minimal is not None:
        params["minimal"] = bool(minimal)
        if include:
            params["include"] = _join_multi(include)

    for key, value in extra.items():
        if value not in (None, ""):
            params[key] = value

    return params, naf_joined


def _band_min(code):
    return {"NN": 0, "00": 0, "01": 1, "02": 3, "03": 6, "11": 10, "12": 20, "21": 50, "22": 100, "31": 200, "32": 250, "41": 500, "42": 1000, "51": 2000, "52": 5000, "53": 10000}.get(code or "", 0)


def convert(item, naf=""):
    siege = item.get("siege") or {}
    cp = siege.get("code_postal") or ""
    diffusion = (item.get("statut_diffusion") or "").upper()
    partial = diffusion == "P"
    return {
        "name": item.get("nom_complet") or item.get("nom_raison_sociale") or "Entreprise",
        "legal_name": item.get("nom_raison_sociale") or "",
        "sector": item.get("libelle_activite_principale") or naf,
        "naf_code": item.get("activite_principale") or naf,
        "department": siege.get("departement") or cp[:2],
        "city": siege.get("libelle_commune") or "",
        "postal_code": cp,
        "address": siege.get("adresse") or " ".join(filter(None, [siege.get("numero_voie"), siege.get("type_voie"), siege.get("libelle_voie")])),
        "siren": item.get("siren") or "",
        "siret": siege.get("siret") or None,
        "employee_band": item.get("tranche_effectif_salarie") or siege.get("tranche_effectif_salarie") or "",
        "creation_date": item.get("date_creation"),
        "categorie_entreprise": item.get("categorie_entreprise") or "",
        "nature_juridique": item.get("nature_juridique") or "",
        "ca": item.get("chiffre_affaires") or None,
        "est_organisme_formation": bool(item.get("est_organisme_formation")),
        "est_qualiopi": bool(item.get("est_qualiopi")),
        "diffusion_partial": partial,
        "prospecting_allowed": not partial,
        "source": "api_recherche_entreprises",
        "source_url": "https://annuaire-entreprises.data.gouv.fr/entreprise/" + (item.get("siren") or ""),
        "source_payload": item,
    }


class SearchAPIError(Exception):
    def __init__(self, message, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _parse_retry_after(response):
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _request_with_retry(params, max_retries=3, timeout=30):
    """Un appel réseau, avec backoff exponentiel + respect explicite de 429/Retry-After."""
    attempt = 0
    last_exc = None
    while attempt <= max_retries:
        _rate_limiter.wait()
        headers = {
            "Accept": "application/json",
            "User-Agent": getattr(settings, "USER_AGENT", "ProspectPilotBot/4.0"),
        }
        try:
            with httpx.Client(timeout=timeout, headers=headers) as client:
                response = client.get(BASE_URL, params=params)
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning("Recherche entreprises: timeout (tentative %s/%s)", attempt + 1, max_retries + 1)
            time.sleep(min(8, 0.5 * (2 ** attempt)))
            attempt += 1
            continue
        except httpx.HTTPError as exc:
            last_exc = exc
            logger.warning("Recherche entreprises: erreur réseau %s (tentative %s/%s)", exc, attempt + 1, max_retries + 1)
            time.sleep(min(8, 0.5 * (2 ** attempt)))
            attempt += 1
            continue

        if response.status_code == 429:
            retry_after = _parse_retry_after(response) or min(30, 1.5 * (2 ** attempt))
            logger.warning("Recherche entreprises: 429 reçu, attente %.1fs avant nouvel essai.", retry_after)
            if attempt >= max_retries:
                raise SearchAPIError("Limite de requêtes atteinte (429).", status_code=429, retry_after=retry_after)
            time.sleep(retry_after)
            attempt += 1
            continue

        if response.status_code == 400:
            raise SearchAPIError(f"Recherche invalide : {response.text}", status_code=400)

        if response.status_code >= 500:
            logger.warning("Recherche entreprises: erreur serveur %s (tentative %s/%s)", response.status_code, attempt + 1, max_retries + 1)
            if attempt >= max_retries:
                raise SearchAPIError(f"Erreur serveur API ({response.status_code}).", status_code=response.status_code)
            time.sleep(min(8, 0.5 * (2 ** attempt)))
            attempt += 1
            continue

        response.raise_for_status()
        return response.json()

    raise SearchAPIError(f"Échec après {max_retries + 1} tentatives : {last_exc}")


def search_companies(query="", naf_code="", postal_code="", department="", city="", employee_min=None, page=1, per_page=25, **kwargs):
    params, naf = build_params(
        query=query, naf_code=naf_code, postal_code=postal_code,
        department=department, page=page, per_page=per_page, **kwargs
    )
    payload = _request_with_retry(params)
    out = []
    for item in payload.get("results", []):
        row = convert(item, naf)
        if city and city.lower().strip() not in row["city"].lower():
            continue
        if employee_min not in (None, "") and _band_min(row["employee_band"]) < int(employee_min):
            continue
        out.append(row)
    return {
        "results": out,
        "page": int(payload.get("page", page)),
        "per_page": int(payload.get("per_page", per_page)),
        "total_results": int(payload.get("total_results", len(out))),
        "total_pages": int(payload.get("total_pages", 1)),
    }


def dedupe_rows(rows):
    """Déduplication par SIREN (fallback SIRET), en conservant la première occurrence."""
    seen = set()
    out = []
    for row in rows:
        key = row.get("siren") or row.get("siret")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def fetch_all_companies(max_results=2000, start_page=1, on_page=None, **kwargs):
    """Récupère jusqu'à `max_results` lignes, en repartant de `start_page` (reprise après erreur).

    `on_page(page_number, rows)` est appelé après chaque page réussie — utile pour
    persister un curseur de reprise sur un CompanySearchRun.
    """
    per_page = kwargs.pop("per_page", MAX_PER_PAGE)
    first = search_companies(page=start_page, per_page=per_page, **kwargs)
    rows = list(first["results"])
    if on_page:
        on_page(start_page, first["results"])
    total_pages = min(first["total_pages"], start_page + (max_results + per_page - 1) // per_page)
    for page_number in range(start_page + 1, total_pages + 1):
        if len(rows) >= max_results:
            break
        try:
            page_data = search_companies(page=page_number, per_page=per_page, **kwargs)
        except SearchAPIError as exc:
            logger.error("Recherche entreprises: page %s abandonnée (%s).", page_number, exc)
            break
        rows.extend(page_data["results"])
        if on_page:
            on_page(page_number, page_data["results"])
    return dedupe_rows(rows)[:max_results]


def get_company_by_siren(siren):
    data = search_companies(query=siren, page=1)
    exact = [x for x in data["results"] if x["siren"] == siren]
    if not exact:
        raise LookupError("Entreprise introuvable")
    return exact[0]


def to_csv(rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Entreprise", "Raison sociale", "Secteur", "NAF", "Département", "Ville", "Code postal", "Adresse", "SIREN", "SIRET", "Effectif", "Prospection"])
    for x in rows:
        w.writerow([x["name"], x["legal_name"], x["sector"], x["naf_code"], x["department"], x["city"], x["postal_code"], x["address"], x["siren"], x["siret"], x["employee_band"], "Oui" if x["prospecting_allowed"] else "Non"])
    return out.getvalue().encode("utf-8-sig")


def to_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Entreprises"
    heads = ["Entreprise", "Raison sociale", "Secteur", "NAF", "Département", "Ville", "Code postal", "Adresse", "SIREN", "SIRET", "Effectif", "Prospection"]
    ws.append(heads)
    for x in rows:
        ws.append([x["name"], x["legal_name"], x["sector"], x["naf_code"], x["department"], x["city"], x["postal_code"], x["address"], x["siren"], x["siret"], x["employee_band"], "Oui" if x["prospecting_allowed"] else "Non"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
