from urllib.parse import urlparse
import httpx

COLLECTIONS_URL = "https://index.commoncrawl.org/collinfo.json"

def latest_index():
    with httpx.Client(timeout=20) as client:
        r = client.get(COLLECTIONS_URL)
        r.raise_for_status()
        collections = r.json()
    return collections[0]["id"]

def query_domain_captures(domain, limit=100):
    index = latest_index()
    endpoint = f"https://index.commoncrawl.org/{index}-index"
    params = {
        "url": f"{domain}/*",
        "output":"json",
        "matchType":"prefix",
        "filter":"status:200",
        "collapse":"urlkey",
    }
    rows = []
    with httpx.Client(timeout=45) as client:
        r = client.get(endpoint, params=params)
        if r.status_code == 404:
            return {"index":index,"rows":[]}
        r.raise_for_status()
        for line in r.text.splitlines()[:limit]:
            try:
                rows.append(__import__("json").loads(line))
            except Exception:
                pass
    return {"index":index,"rows":rows}

def open_web_presence(url):
    domain = urlparse(url).netloc.removeprefix("www.")
    data = query_domain_captures(domain)
    urls = [row.get("url") for row in data["rows"] if row.get("url")]
    return {
        "target_domain":domain,
        "capture_count":len(urls),
        "discovered_urls":urls[:100],
        "referring_domains":[],
        "notes":"Common Crawl fournit ici les captures et la présence du domaine. Une liste exhaustive de backlinks nécessite le traitement des graphes Web Common Crawl ou d'un index spécialisé.",
    }
