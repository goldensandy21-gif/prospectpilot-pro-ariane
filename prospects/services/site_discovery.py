from urllib.parse import urlparse
import itertools
import re
import unicodedata
import httpx
import dns.resolver
from bs4 import BeautifulSoup
from django.conf import settings

COMMON_TLDS = [".fr", ".com", ".net", ".org"]

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

def score_site(url: str, company_name: str, city: str = "", timeout: float = 4) -> tuple[int, dict]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent":settings.USER_AGENT}) as client:
            r = client.get(url)
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type",""):
            return 0, {}
        soup = BeautifulSoup(r.text, "lxml")
        text = (soup.title.get_text(" ",strip=True) if soup.title else "") + " " + soup.get_text(" ",strip=True)[:5000]
        normalized = slugify_domain(text)
        name_tokens = [slugify_domain(x) for x in company_name.split() if len(slugify_domain(x)) > 3]
        hits = sum(1 for token in name_tokens if token in normalized)
        score = min(90, 25 + hits * 20)
        if city and slugify_domain(city) in normalized:
            score += 10
        return min(100, score), {
            "title": soup.title.get_text(" ",strip=True) if soup.title else "",
            "final_url": str(r.url),
        }
    except Exception:
        return 0, {}

def discover_official_site(company_name: str, city: str = "", max_candidates: int | None = None) -> dict:
    best = {"url":"","confidence":0,"evidence":{}}
    candidates = candidate_domains(company_name, city)
    if max_candidates:
        candidates = candidates[:max_candidates]
    for domain in candidates:
        if not domain_resolves(domain):
            continue
        for scheme in ("https://","http://"):
            score, evidence = score_site(scheme + domain, company_name, city)
            if score > best["confidence"]:
                best = {"url": evidence.get("final_url", scheme+domain), "confidence":score, "evidence":evidence}
            if score >= 75:
                return best
    return best
