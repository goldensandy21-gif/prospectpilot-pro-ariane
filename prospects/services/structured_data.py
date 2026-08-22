"""Mission 7 (Web Deep Discovery) — lecture de données structurées (JSON-LD
schema.org + balises meta de date) sur une page déjà téléchargée. Utilisé par
`people_extraction.py` (personnes) et `crawler.py`/`temporal_signals.py`
(faits datés). Ne fait aucune requête réseau — travaille sur un `BeautifulSoup`
déjà construit par l'appelant, jamais une deuxième récupération de page."""
import json
import re

PERSON_TYPES = {"person"}
ORGANIZATION_TYPES = {"organization", "corporation", "localbusiness"}
DATED_CONTENT_TYPES = {
    "article", "blogposting", "newsarticle", "jobposting", "socialmediaposting",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _type_names(node):
    return {str(t).strip().lower() for t in _as_list(node.get("@type"))}


def extract_json_ld_blocks(soup):
    """Renvoie la liste à plat de tous les objets JSON-LD trouvés sur la page
    (déplie les listes et les `@graph`). Ne lève jamais — un bloc JSON invalide
    est simplement ignoré."""
    blocks = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(tag.string or tag.get_text() or "null")
        except (ValueError, TypeError):
            continue
        for item in _as_list(payload):
            if not isinstance(item, dict):
                continue
            if "@graph" in item and isinstance(item["@graph"], list):
                blocks.extend(n for n in item["@graph"] if isinstance(n, dict))
            else:
                blocks.append(item)
    return blocks


def find_persons(json_ld_blocks):
    """Personnes déclarées explicitement en schema.org Person — directement,
    ou imbriquées sous `employee`/`founder`/`member` d'une Organization. Ne
    fabrique jamais un nom : seules les entrées avec un `name` non vide et un
    `@type: Person` explicite sont retenues."""
    found = []

    def _collect_person(node):
        if not isinstance(node, dict) or "person" not in _type_names(node):
            return
        name = str(node.get("name") or "").strip()
        if not name:
            return
        found.append({
            "full_name": name,
            "job_title": str(node.get("jobTitle") or "").strip(),
            "profile_url": str(node.get("url") or node.get("sameAs") or "").strip()
            if not isinstance(node.get("sameAs"), list) else str((node.get("sameAs") or [""])[0]),
            "method": "json_ld_person",
        })

    for block in json_ld_blocks:
        _collect_person(block)
        if _type_names(block) & ORGANIZATION_TYPES:
            for key in ("employee", "founder", "member"):
                for member in _as_list(block.get(key)):
                    _collect_person(member)

    # Dédoublonnage simple par nom (une même personne peut apparaître deux
    # fois si elle est à la fois `founder` et `employee` sur la même page).
    seen = set()
    unique = []
    for person in found:
        key = person["full_name"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(person)
    return unique


def find_dated_content(json_ld_blocks):
    """Faits datés fiables (Article/BlogPosting/NewsArticle/JobPosting) —
    uniquement si le JSON-LD fournit une date explicite au format ISO
    (datePublished/datePosted/dateCreated). Ne renvoie jamais une date
    approximative ou déduite."""
    facts = []
    for block in json_ld_blocks:
        types = _type_names(block)
        if not (types & DATED_CONTENT_TYPES):
            continue
        for field in ("datePublished", "datePosted", "dateCreated"):
            value = str(block.get(field) or "").strip()
            if value and DATE_RE.match(value):
                facts.append({
                    "content_type": next(iter(types & DATED_CONTENT_TYPES)),
                    "date_field": field,
                    "date": value[:10],
                    "headline": str(block.get("headline") or block.get("title") or "").strip(),
                })
                break
    return facts


def find_meta_published_time(soup):
    """Repli sur les balises <meta> de date d'article quand il n'y a pas de
    JSON-LD (article:published_time, meta name="date"/"pubdate"). Renvoie une
    date ISO (YYYY-MM-DD) ou "" — jamais une valeur non datée."""
    for attrs in (
        {"property": "article:published_time"},
        {"name": "date"},
        {"name": "pubdate"},
        {"name": "publish-date"},
    ):
        tag = soup.find("meta", attrs=attrs)
        content = (tag.get("content") or "").strip() if tag else ""
        if content and DATE_RE.match(content):
            return content[:10]
    return ""
