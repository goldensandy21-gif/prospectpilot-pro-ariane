from urllib.parse import urlparse
import itertools
import re
import unicodedata
import httpx
import dns.resolver
from bs4 import BeautifulSoup
from django.conf import settings

COMMON_TLDS = [".fr", ".com", ".net", ".org"]
LEGAL_MENTION_TERMS = ["mentions légales", "mentions legales", "siren", "siret", "rcs "]


def slugify_domain(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", normalized.lower())


def candidate_domains(name: str, city: str = "") -> list[str]:
    base = slugify_domain(name)
    words = [slugify_domain(w) for w in re.split(r"\s+", name) if len(slugify_domain(w)) > 2]
    variants = [base, "-".join(words), "".join(words)]
    if city:
        c = slugify_domain(city)
        variants += [f"{base}{c}", f"{base}-{c}", f"{'-'.join(words)}-{c}"]
    out = []
    for variant, tld in itertools.product(dict.fromkeys(variants), COMMON_TLDS):
        if variant:
            out.append(variant + tld)
    return list(dict.fromkeys(out))[:30]


def domain_resolves(domain: str, lifetime: float = 0.75) -> bool:
    try:
        dns.resolver.resolve(domain, "A", lifetime=lifetime)
        return True
    except Exception:
        return False


def _siren_variants(siren: str) -> list[str]:
    if not siren:
        return []
    return [siren, " ".join([siren[:3], siren[3:6], siren[6:9]])]


def evaluate_site_confidence(text: str, title: str, company_name: str, city: str = "", siren: str = "", siret: str = "") -> tuple[int, dict]:
    """Calcule un score de confiance à partir de PREUVES observées sur la page,
    au lieu de se fier uniquement à la ressemblance du nom de domaine (ETAPE 7)."""
    normalized = slugify_domain(text)
    evidence = {}
    score = 0

    name_tokens = [slugify_domain(x) for x in company_name.split() if len(slugify_domain(x)) > 3]
    name_hits = sum(1 for token in name_tokens if token in normalized)
    if name_tokens:
        ratio = name_hits / len(name_tokens)
        if ratio >= 0.5:
            points = round(25 * ratio)
            score += points
            evidence["company_name_in_content"] = f"{name_hits}/{len(name_tokens)} mots du nom trouvés (+{points})"

    title_norm = slugify_domain(title or "")
    if any(token in title_norm for token in name_tokens):
        score += 10
        evidence["company_name_in_title"] = "Nom présent dans le titre de la page (+10)"

    if city:
        city_norm = slugify_domain(city)
        if city_norm and city_norm in normalized:
            score += 10
            evidence["city_match"] = f"Ville '{city}' mentionnée sur la page (+10)"

    for variant in _siren_variants(siren):
        if variant and re.sub(r"\s", "", variant) in re.sub(r"\s", "", text):
            score += 30
            evidence["siren_match"] = "SIREN trouvé sur la page (+30) — forte preuve d'identité."
            break

    if siret and re.sub(r"\s", "", siret) in re.sub(r"\s", "", text):
        score += 30
        evidence["siret_match"] = "SIRET trouvé sur la page (+30) — forte preuve d'identité."

    lower_text = text.lower()
    if any(term in lower_text for term in LEGAL_MENTION_TERMS):
        score += 8
        evidence["legal_mentions_present"] = "Vocabulaire de mentions légales détecté (+8)"

    return min(100, score), evidence


def score_site(url: str, company_name: str, city: str = "", siren: str = "", siret: str = "", timeout: float = 4) -> tuple[int, dict]:
    from .url_safety import assert_safe_response, is_safe_url

    if not is_safe_url(url):
        return 0, {}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": settings.USER_AGENT}) as client:
            r = client.get(url)
            assert_safe_response(r)
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", ""):
            return 0, {}
        soup = BeautifulSoup(r.text, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = title + " " + soup.get_text(" ", strip=True)[:8000]
        score, evidence = evaluate_site_confidence(text, title, company_name, city, siren, siret)
        redirected = str(r.url) != url
        if redirected:
            evidence["redirected_to"] = str(r.url)
        evidence["title"] = title
        evidence["final_url"] = str(r.url)
        return score, evidence
    except Exception:
        return 0, {}


def discover_official_site(company_name: str, city: str = "", siren: str = "", siret: str = "", max_candidates: int | None = None) -> dict:
    threshold = getattr(settings, "ACQUISITION_SITE_CONFIDENCE_THRESHOLD", 55)
    best = {"url": "", "confidence": 0, "evidence": {}, "needs_manual_review": True}
    candidates = candidate_domains(company_name, city)
    if max_candidates:
        candidates = candidates[:max_candidates]
    for domain in candidates:
        if not domain_resolves(domain):
            continue
        for scheme in ("https://", "http://"):
            score, evidence = score_site(scheme + domain, company_name, city, siren, siret)
            if score > best["confidence"]:
                best = {
                    "url": evidence.get("final_url", scheme + domain),
                    "confidence": score,
                    "evidence": evidence,
                    "needs_manual_review": score < threshold,
                }
            if score >= 75:
                return best
    return best
