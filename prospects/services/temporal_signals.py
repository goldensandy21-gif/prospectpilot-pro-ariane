"""Mission 7E — Temporal Signal Intelligence : classe les faits datés réels
trouvés sur le site propre du prospect (structured_data.find_dated_content(),
JSON-LD Article/BlogPosting/JobPosting/NewsArticle avec une date explicite)
vers les `field_name` que `RecentActivitySignalCollector`
(signal_collectors.py) lit déjà. Aucune date fabriquée : uniquement des dates
explicitement déclarées par le site lui-même, jamais `collected_at`.

FIT != INTENT : une page carrière qui existe depuis des années est un fait de
maturité (FIT), pas un signal temporel. Seule une offre/actualité RÉCEMMENT
DATÉE devient candidate à l'Intent — et seulement si elle mentionne un
domaine pertinent (growth/marketing/acquisition/digital/CRO...)."""

GROWTH_KEYWORDS = [
    "growth", "marketing", "acquisition", "cro", "conversion", "digital",
    "e-commerce", "ecommerce", "commercial", "sales", "seo", "sea", "ads",
    "traffic", "trafic",
]
# Audit correctif round 2, §5 — des phrases spécifiques à un évènement
# stratégique RÉEL, jamais un mot isolé ("acquisition", "investissement")
# que n'importe quel article marketing générique emploie couramment
# ("5 stratégies d'acquisition client", "vos investissements marketing").
# Une actualité qui ne matche aucune de ces phrases reste FIT/activité au
# mieux (dated_content_published), jamais un signal Intent.
ACQUISITION_NEWS_KEYWORDS = [
    "rachète", "rachete", "racheté", "rachetee", "rachetée", "rachat de",
    "fusion",
    "levée de fonds", "lève des fonds", "leve des fonds", "levee de fonds",
    "investissement reçu", "investissement recu", "reçoit un investissement",
    "recoit un investissement", "nouveau financement", "tour de financement",
    "partenariat stratégique", "partenariat strategique",
]


def _mentions(headline, keywords):
    lowered = (headline or "").lower()
    return any(keyword in lowered for keyword in keywords)


def classify_dated_fact(fact):
    """Retourne le field_name ProspectEvidence approprié pour un fait daté
    (dict de structured_data.find_dated_content()), ou None si ce fait ne
    correspond à aucune catégorie suivie — mieux vaut ne rien signaler qu'un
    signal mal étiqueté ou hors-sujet (ex. une actualité RH générique)."""
    content_type = fact.get("content_type", "")
    headline = fact.get("headline", "")

    if content_type == "jobposting" and _mentions(headline, GROWTH_KEYWORDS):
        return "job_posting_growth"
    if content_type == "newsarticle" and _mentions(headline, ACQUISITION_NEWS_KEYWORDS):
        return "news_acquisition"
    if content_type in ("article", "blogposting") and _mentions(headline, GROWTH_KEYWORDS):
        return "dated_content_published"
    return None
