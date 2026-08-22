"""Mission 7E, section 11 — adaptateur France Travail (API publique et
gratuite "Offres d'emploi", api.francetravail.io). Documenté et testable
(HTTP entièrement mockable), mais DORMANT : `is_configured()` doit renvoyer
True (FRANCE_TRAVAIL_CLIENT_ID/FRANCE_TRAVAIL_CLIENT_SECRET réels dans
l'environnement) avant que la moindre requête réseau soit tentée. Aucun
identifiant n'est disponible dans cet environnement — voir
docs/WEB_DATA_INTELLIGENCE.md, section 12, pour la décision.

Authentification OAuth2 client-credentials (même schéma que
GOOGLE_OAUTH_CLIENT_ID/SECRET dans config/settings.py — pas un simple
`_API_KEY`, France Travail exige un échange de jeton)."""
import httpx
from django.conf import settings

from .temporal_signals import GROWTH_KEYWORDS

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"


def is_configured():
    return bool(
        getattr(settings, "FRANCE_TRAVAIL_CLIENT_ID", "")
        and getattr(settings, "FRANCE_TRAVAIL_CLIENT_SECRET", "")
    )


def _get_access_token(client):
    response = client.post(
        TOKEN_URL,
        params={"realm": "/partenaire"},
        data={
            "grant_type": "client_credentials",
            "client_id": settings.FRANCE_TRAVAIL_CLIENT_ID,
            "client_secret": settings.FRANCE_TRAVAIL_CLIENT_SECRET,
            "scope": SCOPE,
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]


def search_recent_offers(company_name, siret="", max_results=10):
    """Offres récentes pour l'entreprise (avec date de publication officielle),
    ou [] si l'API n'est pas configurée ou indisponible — ne lève jamais.
    N'effectue AUCUNE requête réseau tant que is_configured() est False."""
    if not is_configured() or not company_name:
        return []
    params = {"motsCles": company_name, "range": f"0-{max(0, max_results - 1)}"}
    if siret:
        params["siret"] = siret
    try:
        with httpx.Client(timeout=10) as client:
            token = _get_access_token(client)
            response = client.get(
                SEARCH_URL, params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code >= 400:
                return []
            payload = response.json()
    except httpx.HTTPError:
        return []
    return payload.get("resultats", [])


def offers_to_evidence_candidates(offers):
    """Ne garde que les offres growth/marketing/digital pertinentes, avec une
    date de publication officielle explicite — jamais une offre sans date."""
    candidates = []
    for offer in offers:
        title = str(offer.get("intitule") or "")
        if not any(keyword in title.lower() for keyword in GROWTH_KEYWORDS):
            continue
        date_creation = str(offer.get("dateCreation") or "")[:10]
        if not date_creation:
            continue
        origin = offer.get("origineOffre") or {}
        candidates.append({
            "field_name": "job_posting_growth",
            "headline": title,
            "event_date": date_creation,
            "source_url": origin.get("urlOrigine", ""),
            "confidence": 90,
        })
    return candidates


class FranceTravailSource:
    """Source d'enrichissement optionnelle (comme dropcontact/apollo/...) —
    disponible dans SOURCE_CLASSES mais volontairement absente de
    DEFAULT_SOURCE_KEYS tant qu'aucun identifiant n'est configuré."""
    key = "france_travail"

    def collect(self, prospect):
        from .enrichment import EvidenceCandidate

        if not is_configured():
            return []
        offers = search_recent_offers(prospect.name, siret=prospect.siret)
        return [
            EvidenceCandidate(
                field_name=item["field_name"],
                value=item["headline"],
                value_type="other",
                confidence_score=item["confidence"],
                verification_status="verified",
                source_key=self.key,
                source_url=item["source_url"],
                raw_payload={"event_date": item["event_date"], "headline": item["headline"], "method": "france_travail_api"},
            )
            for item in offers_to_evidence_candidates(offers)
        ]
