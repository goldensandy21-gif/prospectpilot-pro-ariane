from collections import Counter, deque
from dataclasses import dataclass, asdict
from urllib.parse import unquote, urljoin, urlparse, urldefrag
import re
import time
import httpx
from bs4 import BeautifulSoup
from django.conf import settings
from .robots import RobotsPolicy
from .technology import detect_technologies

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:\+33|0)[1-9](?:[ .-]?\d{2}){4}")
CTA_TERMS = ["contact", "devis", "réserver", "reservation", "inscription", "commencer", "prendre rendez-vous"]
BOOKING_TERMS = ["réserver", "reservation", "booking", "prendre rendez-vous", "inscription en ligne"]
IMPORTANT_PAGE_TERMS = [
    "contact", "mentions", "legales", "légales", "legal", "about", "a-propos",
    "apropos", "qui-sommes-nous", "equipe", "équipe", "team", "recrutement",
    "carrieres", "carrières", "jobs", "emploi",
]
IMPORTANT_PATHS = [
    "/contact", "/contactez-nous", "/nous-contacter", "/mentions-legales",
    "/mentions-légales", "/a-propos", "/apropos", "/qui-sommes-nous",
    "/equipe", "/équipe", "/team", "/recrutement", "/carrieres", "/carrières",
    "/jobs", "/emploi",
]
SOCIAL_PLATFORMS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "twitter.com": "x",
    "x.com": "x",
}

def normalize_url(url):
    if not url.startswith(("http://","https://")):
        return "https://" + url
    return url

def same_domain(a, b):
    return urlparse(a).netloc.lower().removeprefix("www.") == urlparse(b).netloc.lower().removeprefix("www.")

def important_candidate_urls(start_url):
    parsed = urlparse(normalize_url(start_url))
    root = f"{parsed.scheme}://{parsed.netloc}"
    return [urljoin(root, path) for path in IMPORTANT_PATHS]

def is_important_url(url):
    parsed = urlparse(url)
    haystack = f"{parsed.path} {parsed.query}".lower()
    return any(term in haystack for term in IMPORTANT_PAGE_TERMS)

def clean_email(value):
    value = unquote(value or "").replace("mailto:", "").split("?", 1)[0].strip().lower()
    match = EMAIL_RE.search(value)
    return match.group(0).lower() if match else ""

def clean_phone(value):
    value = unquote(value or "").replace("tel:", "").split("?", 1)[0].strip()
    match = PHONE_RE.search(value)
    return match.group(0) if match else ""

def platform_for_url(url):
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for domain, platform in SOCIAL_PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return ""

def analyze_contact_forms(soup, url):
    forms = []
    for form in soup.find_all("form"):
        action = form.get("action", "").strip()
        method = form.get("method", "get").strip().lower()[:12]
        fields = " ".join(
            " ".join(filter(None, [
                tag.get("type", ""),
                tag.get("name", ""),
                tag.get("id", ""),
                tag.get("placeholder", ""),
                tag.get("aria-label", ""),
            ]))
            for tag in form.find_all(["input", "textarea", "select"])
        ).lower()
        forms.append({
            "page_url": url,
            "form_action": urljoin(url, action) if action else "",
            "form_method": method,
            "has_email_field": "email" in fields or "e-mail" in fields or "mail" in fields,
            "has_phone_field": "tel" in fields or "phone" in fields or "téléphone" in fields or "telephone" in fields,
        })
    return forms

def analyze_html(url, response, depth):
    soup = BeautifulSoup(response.text, "lxml")
    text = soup.get_text(" ", strip=True)
    lower = text.lower()
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    canonical_tag = soup.find("link", rel=lambda x: x and "canonical" in x)
    images = soup.find_all("img")
    links = soup.find_all("a", href=True)
    internal = []
    external = []
    mailto_emails = []
    tel_phones = []
    social_links = []
    for link in links:
        absolute = urldefrag(urljoin(url, link["href"]))[0]
        href = link["href"].strip()
        if href.lower().startswith("mailto:"):
            email = clean_email(href)
            if email:
                mailto_emails.append(email)
            continue
        if href.lower().startswith("tel:"):
            phone = clean_phone(href)
            if phone:
                tel_phones.append(phone)
            continue
        if not absolute.startswith(("http://","https://")):
            continue
        (internal if same_domain(url, absolute) else external).append(absolute)
        platform = platform_for_url(absolute)
        if platform:
            social_links.append({
                "platform": platform,
                "url": absolute,
                "source_url": url,
            })

    issues = []
    if not title: issues.append("Titre HTML absent")
    if not meta or not meta.get("content","").strip(): issues.append("Méta-description absente")
    if len(soup.find_all("h1")) == 0: issues.append("H1 absent")
    if not soup.find("meta", attrs={"name":re.compile("^viewport$",re.I)}): issues.append("Viewport mobile absent")
    missing_alt = sum(1 for img in images if not img.get("alt","").strip())
    if missing_alt: issues.append(f"{missing_alt} image(s) sans alt")
    if len(text.split()) < 120: issues.append("Contenu textuel très court")

    found_emails = sorted(set(EMAIL_RE.findall(text) + mailto_emails))[:20]
    found_phones = sorted(set(PHONE_RE.findall(text) + tel_phones))[:20]
    contact_forms = analyze_contact_forms(soup, url)

    return {
        "url":url, "depth":depth, "http_status":response.status_code,
        "content_type":response.headers.get("content-type",""),
        "title":title,
        "meta_description":(meta.get("content","").strip() if meta else "")[:1000],
        "canonical":urljoin(url, canonical_tag.get("href","")) if canonical_tag else "",
        "h1_count":len(soup.find_all("h1")),
        "word_count":len(text.split()),
        "images_count":len(images),
        "images_without_alt":missing_alt,
        "internal_links":len(set(internal)),
        "external_links":len(set(external)),
        "has_https":url.startswith("https://"),
        "has_viewport":bool(soup.find("meta", attrs={"name":re.compile("^viewport$",re.I)})),
        "has_contact_form":bool(contact_forms),
        "has_booking":any(x in lower for x in BOOKING_TERMS),
        "has_phone":bool(found_phones),
        "has_email":bool(found_emails),
        "has_cta":any(x in lower for x in CTA_TERMS),
        "found_emails":found_emails,
        "found_phones":found_phones,
        "found_contact_forms":contact_forms[:10],
        "found_social_links":social_links[:20],
        "technologies":detect_technologies(response.text, dict(response.headers)),
        "issues":issues,
        "_internal_urls":list(dict.fromkeys(internal)),
    }

def check_links(client, urls, max_links=30):
    broken = 0
    for url in list(dict.fromkeys(urls))[:max_links]:
        try:
            r = client.head(url, timeout=6, follow_redirects=True)
            if r.status_code >= 400:
                r = client.get(url, timeout=8, follow_redirects=True)
            if r.status_code >= 400:
                broken += 1
        except httpx.HTTPError:
            broken += 1
    return broken

def crawl_site(start_url, max_pages=None, check_broken_links=True):
    start_url = normalize_url(start_url)
    max_pages = max_pages or settings.CRAWL_MAX_PAGES
    policy = RobotsPolicy(start_url).load()
    delay = policy.crawl_delay() or settings.CRAWL_DELAY_SECONDS
    queue = deque([(start_url,0)])
    for candidate in important_candidate_urls(start_url):
        queue.append((candidate, 1))
    seen = set()
    pages = []

    headers = {"User-Agent":settings.USER_AGENT, "Accept":"text/html,application/xhtml+xml"}
    with httpx.Client(headers=headers, timeout=18, follow_redirects=True) as client:
        while queue and len(pages) < max_pages:
            url, depth = queue.popleft()
            url = urldefrag(url)[0]
            if url in seen or not same_domain(start_url, url):
                continue
            seen.add(url)
            if not policy.allowed(url):
                continue
            started = time.perf_counter()
            try:
                response = client.get(url)
                elapsed = round((time.perf_counter()-started)*1000)
                if response.status_code >= 400 or "text/html" not in response.headers.get("content-type",""):
                    continue
                data = analyze_html(str(response.url), response, depth)
                data["response_ms"] = elapsed
                data["broken_links"] = (
                    check_links(client, data["_internal_urls"], max_links=15)
                    if check_broken_links
                    else 0
                )
                child_urls = sorted(
                    data["_internal_urls"],
                    key=lambda child: (not is_important_url(child), len(child)),
                )
                for child in child_urls:
                    if child not in seen and depth < 3:
                        queue.append((child, depth+1))
                data.pop("_internal_urls", None)
                pages.append(data)
                time.sleep(delay)
            except httpx.HTTPError:
                continue

    return {
        "robots_url":policy.robots_url,
        "robots_allowed":policy.allowed(start_url),
        "pages":pages,
    }

def summarize_pages(pages):
    titles = [p["title"] for p in pages if p["title"]]
    title_counts = Counter(titles)
    technologies = sorted(set(x for p in pages for x in p["technologies"]))
    issues = []
    missing_titles = sum(1 for p in pages if not p["title"])
    missing_descriptions = sum(1 for p in pages if not p["meta_description"])
    missing_h1 = sum(1 for p in pages if p["h1_count"] == 0)
    broken_links = sum(p["broken_links"] for p in pages)
    alt = sum(p["images_without_alt"] for p in pages)
    duplicate_titles = sum(v-1 for v in title_counts.values() if v > 1)
    avg_response = round(sum(p["response_ms"] for p in pages) / max(1,len(pages)))

    if missing_titles: issues.append(f"{missing_titles} page(s) sans titre")
    if missing_descriptions: issues.append(f"{missing_descriptions} page(s) sans méta-description")
    if missing_h1: issues.append(f"{missing_h1} page(s) sans H1")
    if duplicate_titles: issues.append(f"{duplicate_titles} titre(s) dupliqué(s)")
    if broken_links: issues.append(f"{broken_links} lien(s) cassé(s)")
    if alt: issues.append(f"{alt} image(s) sans alt")
    if avg_response > 2000: issues.append("Temps de réponse moyen élevé")

    recommendations = []
    if missing_titles or duplicate_titles: recommendations.append("Créer un titre unique pour chaque page.")
    if missing_descriptions: recommendations.append("Rédiger les méta-descriptions manquantes.")
    if broken_links: recommendations.append("Corriger ou rediriger les liens cassés.")
    if alt: recommendations.append("Ajouter des textes alternatifs descriptifs aux images.")
    if avg_response > 2000: recommendations.append("Optimiser le serveur, les images et les scripts.")

    return {
        "pages_crawled":len(pages),
        "broken_links":broken_links,
        "duplicate_titles":duplicate_titles,
        "missing_titles":missing_titles,
        "missing_descriptions":missing_descriptions,
        "missing_h1":missing_h1,
        "images_without_alt":alt,
        "average_response_ms":avg_response,
        "technologies":technologies,
        "issues":issues,
        "recommendations":recommendations,
    }
