import re
from bs4 import BeautifulSoup

TECH_RULES = {
    "WordPress": [r"/wp-content/", r"/wp-includes/", r'name=["\']generator["\'][^>]*WordPress'],
    "Shopify": [r"cdn\.shopify\.com", r"Shopify\.theme"],
    "Wix": [r"wixstatic\.com", r"_wix_browser_sess"],
    "Squarespace": [r"static1\.squarespace\.com"],
    "Webflow": [r"webflow\.com", r"data-wf-page"],
    "Bootstrap": [r"bootstrap(?:\.min)?\.css", r"bootstrap(?:\.bundle)?"],
    "Tailwind CSS": [r"tailwind"],
    "React": [r"react(?:\.production)?\.min\.js", r"__NEXT_DATA__"],
    "Next.js": [r"/_next/", r"__NEXT_DATA__"],
    "Vue.js": [r"vue(?:\.global|\.runtime|\.min)?\.js", r"data-v-"],
    "Angular": [r"ng-version", r"angular(?:\.min)?\.js"],
    "jQuery": [r"jquery(?:-|\.)"],
    "Google Analytics": [r"googletagmanager\.com/gtag", r"google-analytics\.com"],
    "Google Tag Manager": [r"googletagmanager\.com/gtm\.js"],
    "HubSpot": [r"js\.hs-scripts\.com", r"hubspot"],
    "Stripe": [r"js\.stripe\.com"],
    "Calendly": [r"calendly\.com"],
}

def detect_technologies(html: str, headers: dict | None = None) -> list[str]:
    haystack = html.lower()
    found = []
    for name, patterns in TECH_RULES.items():
        if any(re.search(pattern.lower(), haystack, re.I) for pattern in patterns):
            found.append(name)
    server = (headers or {}).get("server", "")
    powered = (headers or {}).get("x-powered-by", "")
    if server:
        found.append(f"Serveur: {server}")
    if powered:
        found.append(f"X-Powered-By: {powered}")
    return sorted(set(found))
