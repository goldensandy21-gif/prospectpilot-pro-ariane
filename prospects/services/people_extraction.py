"""Mission 7C — People Discovery : extraction de personnes réelles depuis les
pages publiques d'une entreprise (équipe/à-propos), sans jamais scraper
LinkedIn ni fabriquer un nom. Deux méthodes, toujours avec provenance :

1. `structured_data.find_persons()` — schema.org Person explicite (fiable).
2. `extract_people_heuristic()` — repli texte, uniquement sur les pages qui
   ressemblent à une page équipe (jamais sur une page générique), et
   uniquement quand un nom propre plausible est directement associé à un
   intitulé de poste reconnu — sinon aucune personne n'est renvoyée.

Alimente `ContactPerson` via `EnrichmentEngine.store_person()`
(`enrichment.py`) — jamais un nouveau modèle.
"""
import re

from .predictneed_scoring import RELEVANT_ROLES
from .structured_data import extract_json_ld_blocks, find_persons

# Vocabulaire de poste plus large que RELEVANT_ROLES (qui sert au scoring
# commercial) : sert seulement à repérer qu'une ligne de texte est bien un
# intitulé de poste, pas à évaluer sa pertinence commerciale.
TITLE_KEYWORDS = set(RELEVANT_ROLES) | {
    "directeur", "directrice", "responsable", "manager", "associé", "associée",
    "cofondateur", "cofondatrice", "co-fondateur", "co-fondatrice",
    "chef de projet", "chargé de", "chargée de", "coo", "cto", "cmo", "cfo",
}

_NAME_RE = re.compile(r"^[A-ZÀ-Ý][a-zà-ÿ'’-]+(?: [A-ZÀ-Ý][a-zà-ÿ'’-]+){1,2}$")
_TEAM_PAGE_TERMS = ("equipe", "équipe", "team", "collaborateurs", "a-propos", "apropos", "qui-sommes-nous", "about")


def looks_like_team_page(url):
    lowered = url.lower()
    return any(term in lowered for term in _TEAM_PAGE_TERMS)


def _looks_like_title(text):
    lowered = text.lower()
    return len(text) <= 80 and any(keyword in lowered for keyword in TITLE_KEYWORDS)


def _looks_like_name(text):
    # Un intitulé de poste ("Directrice Marketing") peut accidentellement
    # matcher le motif "2-3 mots en casse Titre" d'un nom propre — le
    # vocabulaire de poste est toujours prioritaire pour lever l'ambiguïté.
    if _looks_like_title(text):
        return False
    return bool(_NAME_RE.match(text.strip())) and len(text) <= 60


def extract_people_heuristic(soup):
    """Ne s'exécute utilement que sur une page équipe (voir looks_like_team_page,
    à vérifier par l'appelant). Associe un nom propre plausible (2-3 mots en
    casse Titre) à un intitulé de poste reconnu apparaissant dans un élément
    de texte court immédiatement adjacent (même bloc, ou frère précédent/
    suivant). Aucune association -> aucune personne renvoyée."""
    elements = soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "span", "div", "figcaption"])
    lines = []
    seen_texts = set()
    for el in elements:
        text = el.get_text(" ", strip=True)
        if not text or text in seen_texts:
            continue
        # Ignore un conteneur dont le texte est juste la concaténation de
        # plusieurs de ses enfants (bruit) : on ne garde que les feuilles
        # "courtes" qui correspondent à un nom OU un intitulé de poste isolé.
        if len(text) > 80:
            continue
        seen_texts.add(text)
        lines.append(text)

    people = []
    used_names = set()
    for i, text in enumerate(lines):
        if not _looks_like_name(text) or text.lower() in used_names:
            continue
        job_title = ""
        for neighbor in (lines[i + 1] if i + 1 < len(lines) else "", lines[i - 1] if i > 0 else ""):
            if neighbor and _looks_like_title(neighbor) and not _looks_like_name(neighbor):
                job_title = neighbor
                break
        if not job_title:
            continue
        used_names.add(text.lower())
        people.append({
            "full_name": text, "job_title": job_title, "profile_url": "",
            "method": "heuristic_team_page_text",
        })
    return people


def extract_people_from_page(url, soup):
    """Point d'entrée unique : combine schema.org (toujours actif) et
    l'heuristique texte (seulement sur une page équipe reconnue). Chaque
    résultat porte sa propre `method` pour la traçabilité (raw_payload)."""
    json_ld_people = find_persons(extract_json_ld_blocks(soup))
    if not looks_like_team_page(url):
        return json_ld_people

    heuristic_people = extract_people_heuristic(soup)
    known = {p["full_name"].strip().lower() for p in json_ld_people}
    for person in heuristic_people:
        key = person["full_name"].strip().lower()
        if key not in known:
            known.add(key)
            json_ld_people.append(person)
    return json_ld_people
