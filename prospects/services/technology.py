import re
from bs4 import BeautifulSoup

# ETAPE 9 — chaque entrée est une preuve publique (regex sur le HTML/JS livré par le
# serveur), jamais une déduction. category correspond à ProspectTechnology.CATEGORIES.
TECH_RULES = {
    # --- stack technique (conservé) -----------------------------------
    "WordPress": {"category": "other", "patterns": [r"/wp-content/", r"/wp-includes/", r'name=["\']generator["\'][^>]*WordPress']},
    "Wix": {"category": "other", "patterns": [r"wixstatic\.com", r"_wix_browser_sess"]},
    "Squarespace": {"category": "other", "patterns": [r"static1\.squarespace\.com"]},
    "Webflow": {"category": "other", "patterns": [r"webflow\.com", r"data-wf-page"]},
    "Bootstrap": {"category": "other", "patterns": [r"bootstrap(?:\.min)?\.css", r"bootstrap(?:\.bundle)?"]},
    "Tailwind CSS": {"category": "other", "patterns": [r"tailwind"]},
    "React": {"category": "other", "patterns": [r"react(?:\.production)?\.min\.js", r"__NEXT_DATA__"]},
    "Next.js": {"category": "other", "patterns": [r"/_next/", r"__NEXT_DATA__"]},
    "Vue.js": {"category": "other", "patterns": [r"vue(?:\.global|\.runtime|\.min)?\.js", r"data-v-"]},
    "Angular": {"category": "other", "patterns": [r"ng-version", r"angular(?:\.min)?\.js"]},
    "jQuery": {"category": "other", "patterns": [r"jquery(?:-|\.)"]},

    # --- analytics -------------------------------------------------------
    "Google Analytics": {"category": "analytics", "patterns": [r"google-analytics\.com/analytics\.js", r"\bga\(['\"]create"]},
    "Google Analytics 4 (GA4)": {"category": "analytics", "patterns": [r"gtag/js\?id=G-", r"googletagmanager\.com/gtag/js"]},
    "Google Tag Manager": {"category": "analytics", "patterns": [r"googletagmanager\.com/gtm\.js", r"GTM-[A-Z0-9]+"]},
    "Hotjar": {"category": "behaviour_analytics", "patterns": [r"static\.hotjar\.com", r"hotjar\.com/c/hotjar-"]},
    "Microsoft Clarity": {"category": "behaviour_analytics", "patterns": [r"clarity\.ms/tag", r"\bclarity\("]},
    "Contentsquare": {"category": "behaviour_analytics", "patterns": [r"contentsquare\.net", r"\bcs\.js\b"]},

    # --- publicité ---------------------------------------------------------
    "Google Ads": {"category": "advertising", "patterns": [r"googleadservices\.com", r"AW-\d{6,}", r"google_conversion_id"]},
    "Meta Pixel": {"category": "advertising", "patterns": [r"connect\.facebook\.net/[a-z_]+/fbevents\.js", r"\bfbq\('init'"]},
    "LinkedIn Insight Tag": {"category": "advertising", "patterns": [r"snap\.licdn\.com/li\.lms-analytics", r"_linkedin_partner_id"]},
    "TikTok Pixel": {"category": "advertising", "patterns": [r"analytics\.tiktok\.com/i18n/pixel", r"\bttq\.load\("]},

    # --- CRM / marketing automation ----------------------------------------
    "HubSpot": {"category": "crm", "patterns": [r"js\.hs-scripts\.com", r"js\.hsforms\.net", r"hs-analytics"]},
    "Salesforce": {"category": "crm", "patterns": [r"force\.com", r"salesforce\.com/embeddedservice", r"pardot\.com"]},
    "Pipedrive": {"category": "crm", "patterns": [r"pipedrive\.com/leadbooster", r"leadbooster-chat\.pipedrive\.com"]},
    "Brevo": {"category": "crm", "patterns": [r"sibforms\.com", r"sendinblue\.com", r"brevo\.com"]},
    "Mailchimp": {"category": "crm", "patterns": [r"chimpstatic\.com", r"list-manage\.com"]},
    "ActiveCampaign": {"category": "crm", "patterns": [r"activehosted\.com", r"activecampaign\.com"]},
    "Klaviyo": {"category": "crm", "patterns": [r"static\.klaviyo\.com", r"klaviyo\.com/onsite"]},

    # --- prise de RDV / formulaires -----------------------------------------
    "Calendly": {"category": "scheduling", "patterns": [r"calendly\.com"]},
    "Typeform": {"category": "scheduling", "patterns": [r"embed\.typeform\.com", r"typeform\.com/to/"]},

    # --- support / chat ------------------------------------------------------
    "Intercom": {"category": "support", "patterns": [r"widget\.intercom\.io", r"intercomcdn\.com"]},
    "Crisp": {"category": "support", "patterns": [r"client\.crisp\.chat", r"\$crisp\b"]},
    "Zendesk": {"category": "support", "patterns": [r"zdassets\.com", r"zendesk\.com/embeddable"]},

    # --- e-commerce / paiement -------------------------------------------------
    "Stripe": {"category": "ecommerce", "patterns": [r"js\.stripe\.com"]},
    "Shopify": {"category": "ecommerce", "patterns": [r"cdn\.shopify\.com", r"Shopify\.theme", r"myshopify\.com"]},
    "WooCommerce": {"category": "ecommerce", "patterns": [r"woocommerce", r"wc-ajax"]},
    "Prestashop": {"category": "ecommerce", "patterns": [r"prestashop", r"/modules/ps_"]},

    # --- intelligence visiteurs B2B (concurrents potentiels) --------------------
    "Dealfront / Leadfeeder": {"category": "visitor_intelligence", "patterns": [r"leadfeeder\.com", r"dealfront\.com", r"lftracker"]},
    "Leadinfo": {"category": "visitor_intelligence", "patterns": [r"leadinfo\.com", r"leadinfo\.js"]},
}


def detect_technologies(html: str, headers: dict | None = None) -> list[str]:
    """API historique : liste de noms détectés (utilisée par crawler.py/PageAudit)."""
    haystack = html.lower()
    found = []
    for name, rule in TECH_RULES.items():
        if any(re.search(pattern.lower(), haystack, re.I) for pattern in rule["patterns"]):
            found.append(name)
    server = (headers or {}).get("server", "")
    powered = (headers or {}).get("x-powered-by", "")
    if server:
        found.append(f"Serveur: {server}")
    if powered:
        found.append(f"X-Powered-By: {powered}")
    return sorted(set(found))


def detect_technologies_detailed(html: str, headers: dict | None = None, source_url: str = "") -> list[dict]:
    """Détection avec preuve, pour alimenter ProspectTechnology.

    Chaque résultat contient le motif exact qui a matché (`evidence`), pour ne
    jamais prétendre qu'une techno est présente sans preuve (ETAPE 9)."""
    results = []
    for name, rule in TECH_RULES.items():
        for pattern in rule["patterns"]:
            match = re.search(pattern, html, re.I)
            if match:
                results.append({
                    "technology": name,
                    "category": rule["category"],
                    "source_url": source_url,
                    "evidence": f"Motif détecté : {match.group(0)[:120]!r}",
                    "confidence": 85,
                })
                break
    return results
