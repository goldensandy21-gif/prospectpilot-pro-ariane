"""ETAPE 8 — Quick scan commercial.

Répond vite à une question simple : "ce site mérite-t-il une analyse commerciale
complète pour PredictNeed ?" Contrairement à crawler.crawl_site (audit technique
complet, jusqu'à CRAWL_MAX_PAGES pages en profondeur), le quick scan ne visite
qu'une poignée de pages commerciales clés et respecte robots.txt.
"""
from urllib.parse import urljoin, urlparse
import re

import httpx
from bs4 import BeautifulSoup
from django.conf import settings

from .crawler import (
    EMAIL_RE, PHONE_RE, analyze_contact_forms, clean_email, clean_phone,
    normalize_url, platform_for_url, same_domain,
)
from .robots import RobotsPolicy
from .technology import detect_technologies, detect_technologies_detailed

COMMERCIAL_PATH_TERMS = [
    "services", "offres", "offre", "produits", "produit", "tarif", "tarifs",
    "prix", "pricing", "contact", "devis", "reservation", "réservation",
    "inscription", "formation", "formations", "a-propos", "apropos",
    "qui-sommes-nous", "mentions-legales", "mentions-légales",
]

BUSINESS_TYPE_SIGNALS = {
    "formation": ["formation", "certifiant", "qualiopi", "stagiaire", "session de formation"],
    "conseil": ["cabinet de conseil", "conseil en", "accompagnement stratégique", "consultant"],
    "agence": ["agence web", "agence digitale", "agence marketing", "agence de communication"],
    "e_commerce": ["ajouter au panier", "livraison", "mon panier", "paiement sécurisé"],
    "immobilier": ["annonce immobilière", "bien à vendre", "estimation immobilière"],
    "service_b2b": ["nos clients", "solutions pour entreprises", "b2b", "devis gratuit"],
}

RESERVATION_TERMS = ["réserver", "reserver", "prendre rendez-vous", "prendre rdv", "booking", "calendly"]
NEWSLETTER_TERMS = ["newsletter", "s'abonner", "recevoir nos actualités"]
CHAT_TERMS = ["chat en direct", "discutons", "besoin d'aide", "widget de chat"]
CART_TERMS = ["panier", "ajouter au panier", "cart"]
PAYMENT_TERMS = ["paiement sécurisé", "stripe", "carte bancaire", "checkout"]
ACCOUNT_TERMS = ["mon compte", "se connecter", "créer un compte", "espace client"]
LEAD_MAGNET_TERMS = ["télécharger le guide", "livre blanc", "recevoir le guide", "ebook gratuit"]


def _candidate_urls(base_url):
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    return [base_url] + [urljoin(root, f"/{term}") for term in COMMERCIAL_PATH_TERMS]


def _detect_business_type(lower_text):
    scores = {}
    for business_type, terms in BUSINESS_TYPE_SIGNALS.items():
        hits = sum(1 for term in terms if term in lower_text)
        if hits:
            scores[business_type] = hits
    if not scores:
        return "vitrine"
    return max(scores, key=scores.get)


def quick_scan_site(url, max_pages=None):
    """Analyse commerciale légère. Retourne un dict prêt à stocker sur SearchCandidate.quick_scan_data."""
    url = normalize_url(url)
    max_pages = max_pages or getattr(settings, "ACQUISITION_QUICK_SCAN_PAGES", 5)
    policy = RobotsPolicy(url).load()

    result = {
        "pages_checked": 0,
        "business_type": "inconnu",
        "has_contact_form": False,
        "has_quote_request": False,
        "has_booking": False,
        "has_signup": False,
        "has_cart": False,
        "has_payment": False,
        "has_user_account": False,
        "has_newsletter": False,
        "has_chat": False,
        "has_pricing_page": False,
        "has_services_page": False,
        "has_landing_pages": False,
        "has_lead_magnet": False,
        "detected_ctas": [],
        "technologies": [],
        "technologies_detailed": [],
        "found_emails": [],
        "found_phones": [],
        "found_social_links": [],
        "pages": [],
        "worth_full_analysis": False,
        "error": "",
    }

    if not policy.allowed(url):
        result["error"] = "robots.txt interdit l'accès à ce site."
        return result

    candidates = _candidate_urls(url)[:max_pages]
    headers = {"User-Agent": settings.USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    aggregated_text = []
    technologies = set()
    technologies_detailed = {}
    found_emails = []
    found_phones = []
    found_social_links = []

    with httpx.Client(headers=headers, timeout=12, follow_redirects=True) as client:
        for page_url in candidates:
            if not same_domain(url, page_url) or not policy.allowed(page_url):
                continue
            try:
                response = client.get(page_url)
            except httpx.HTTPError:
                continue
            if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", ""):
                continue

            result["pages_checked"] += 1
            soup = BeautifulSoup(response.text, "lxml")
            text = soup.get_text(" ", strip=True)
            lower = text.lower()
            aggregated_text.append(lower)
            technologies.update(detect_technologies(response.text, dict(response.headers)))
            for tech in detect_technologies_detailed(response.text, dict(response.headers), source_url=page_url):
                technologies_detailed.setdefault(tech["technology"], tech)

            for match in EMAIL_RE.findall(text):
                found_emails.append(match.strip().lower())
            for link in soup.find_all("a", href=True):
                href = link["href"].strip()
                if href.lower().startswith("mailto:"):
                    email = clean_email(href)
                    if email:
                        found_emails.append(email)
                elif href.lower().startswith("tel:"):
                    phone = clean_phone(href)
                    if phone:
                        found_phones.append(phone)
                else:
                    platform = platform_for_url(href)
                    if platform:
                        found_social_links.append({"platform": platform, "url": href, "source_url": page_url})
            for match in PHONE_RE.findall(text):
                found_phones.append(match.strip())

            forms = analyze_contact_forms(soup, page_url)
            if forms:
                result["has_contact_form"] = True
                if any(f["has_email_field"] for f in forms):
                    result["has_quote_request"] = result["has_quote_request"] or "devis" in lower

            path = urlparse(page_url).path.lower()
            if any(term in path or term in lower for term in ["tarif", "prix", "pricing"]):
                result["has_pricing_page"] = True
            if any(term in path for term in ["services", "offre", "offres", "produit", "produits"]):
                result["has_services_page"] = True
            if any(term in lower for term in RESERVATION_TERMS):
                result["has_booking"] = True
            if any(term in lower for term in ["inscription", "s'inscrire", "je m'inscris"]):
                result["has_signup"] = True
            if any(term in lower for term in CART_TERMS):
                result["has_cart"] = True
            if any(term in lower for term in PAYMENT_TERMS):
                result["has_payment"] = True
            if any(term in lower for term in ACCOUNT_TERMS):
                result["has_user_account"] = True
            if any(term in lower for term in NEWSLETTER_TERMS):
                result["has_newsletter"] = True
            if any(term in lower for term in CHAT_TERMS):
                result["has_chat"] = True
            if any(term in lower for term in LEAD_MAGNET_TERMS):
                result["has_lead_magnet"] = True

            result["pages"].append({"url": page_url, "path": path})

    full_text = " ".join(aggregated_text)
    result["business_type"] = _detect_business_type(full_text)
    result["technologies"] = sorted(technologies)
    result["technologies_detailed"] = list(technologies_detailed.values())
    result["found_emails"] = list(dict.fromkeys(e for e in found_emails if e))
    result["found_phones"] = list(dict.fromkeys(p for p in found_phones if p))
    result["found_social_links"] = list({link["url"]: link for link in found_social_links}.values())
    result["has_landing_pages"] = result["pages_checked"] >= 2 and result["has_pricing_page"] and result["has_services_page"]

    conversion_signals = sum([
        result["has_contact_form"], result["has_booking"], result["has_signup"],
        result["has_pricing_page"], result["has_services_page"],
    ])
    result["worth_full_analysis"] = result["pages_checked"] > 0 and conversion_signals >= 2

    return result
