"""Mission 6, section 8 — Architecture SignalCollector.

Règle absolue : `SignalCollector -> format normalisé -> ProspectSignal`.
Aucune deuxième table de signaux, jamais.

Avant d'écrire un nouveau collecteur, la donnée a été vérifiée comme non
déjà exploitée (audit fait avant ce module) :
- `ProspectTechnology` / `SearchCandidate.quick_scan_data` -> DÉJÀ des
  signaux, via `build_signals_from_technologies`/`build_signals_from_quick_scan`
  (services/signals.py) — repris ici tels quels, juste encapsulés dans
  l'interface commune.
- `PublicSocialLink` (présence LinkedIn/réseaux) -> collectée par
  `EnrichmentEngine`/le pipeline d'acquisition, mais ne produisait AUCUN
  signal avant ce module : `SocialPresenceSignalCollector`.
- `ContactPerson` (décideur identifié) -> collecté par `EnrichmentEngine`,
  ne produisait AUCUN signal avant ce module : `DecisionMakerSignalCollector`.
- `ProspectEvidence` (registre générique de faits) -> délibérément PAS
  transformé en collecteur générique : elle contiendrait une ligne par fait
  d'enrichissement (adresse, email, téléphone...), ce qui produirait un
  volume de signaux bruyant et redondant avec PublicEmail/PublicPhone/
  ContactPerson eux-mêmes déjà couverts. Seuls les faits qui ont un sens
  commercial direct (présence sociale, décideur) sont transformés ici.
- `SearchConsoleMetric`/PageSpeed (`services/search_console.py`,
  `services/pagespeed.py`) -> DÉLIBÉRÉMENT exclus : ils appartiennent au
  pipeline d'audit technique historique (`audit_site_task`), séparé du
  parcours Recherche → Sélection → Prospects unifié en Mission 5, et
  `SearchConsoleMetric` n'a même pas de clé étrangère vers `Prospect`.
- `SiteAuditSummary.technologies` -> réutilisé de façon ciblée par
  `SiteChangeSignalCollector` (correctif d'audit, round 2) : c'est la seule
  donnée déjà existante qui donne deux VRAIS instantanés datés comparables
  d'un même prospect (un par "Audit multi-pages"). Rien d'autre de
  `SiteAuditSummary` (scores performance/SEO...) n'est exploité ici.

Aucun collecteur ci-dessous n'effectue d'appel réseau lui-même : chacun lit
des données DÉJÀ collectées et déjà persistées (technologies, quick scan,
liens sociaux, contacts). `timeout_seconds`/`rate_limit_per_minute` restent
déclarés sur la classe de base pour qu'un futur collecteur qui interrogerait
une source distante (ex. open_web) s'intègre au même contrat sans revoir
l'architecture — ils ne sont pas appliqués ici faute d'E/S réelle.

Correctif d'audit (post-Mission 6) — deux collecteurs réellement temporels,
datés par preuve et jamais par déduction :
- `SiteChangeSignalCollector` : une technologie apparue depuis un scan
  antérieur (comparaison réelle, pas une première détection).
- `RecentActivitySignalCollector` : recrutement Growth/Marketing/CRO ou
  actualité récente, lus depuis `ProspectEvidence` avec une date d'événement
  réelle explicite — sans date réelle, aucun signal (prêt pour une future
  source d'enrichissement, sans scraping LinkedIn).
"""
from abc import ABC, abstractmethod
from datetime import datetime

from django.utils import timezone

from .signals import _signal


class SignalCollector(ABC):
    name = "abstract"
    source_kind = "manual"
    timeout_seconds = 10
    rate_limit_per_minute = None

    @abstractmethod
    def collect(self, prospect):
        """Renvoie une liste de ProspectSignal non sauvegardés (via `_signal`
        de services/signals.py). Ne doit jamais lever pour une donnée
        absente — renvoyer une liste vide."""
        raise NotImplementedError


class TechnologySignalCollector(SignalCollector):
    """Encapsule build_signals_from_technologies (déjà existant) dans
    l'interface commune — aucune nouvelle logique de détection."""
    name = "technology"
    source_kind = "technology"

    def collect(self, prospect):
        from .signals import build_signals_from_technologies

        technologies = [
            {"technology": t.technology, "category": t.category}
            for t in prospect.technologies.filter(is_active=True)
        ]
        if not technologies:
            return []
        return build_signals_from_technologies(prospect, technologies, site_url=prospect.website)


class QuickScanSignalCollector(SignalCollector):
    """Encapsule build_signals_from_quick_scan à partir du dernier quick scan
    connu pour ce prospect (SearchCandidate.quick_scan_data), sans relancer
    de scan — aucun appel réseau."""
    name = "quick_scan"
    source_kind = "website"

    def collect(self, prospect):
        from .signals import build_signals_from_quick_scan

        candidate = prospect.search_candidate_origins.exclude(quick_scan_data={}).order_by("-updated_at").first()
        if not candidate:
            return []
        return build_signals_from_quick_scan(prospect, candidate.quick_scan_data, site_url=prospect.website)


class SocialPresenceSignalCollector(SignalCollector):
    """Nouvelle source : une présence sociale confirmée (PublicSocialLink)
    n'était traduite en aucun signal jusqu'ici. Indicateur de contactabilité/
    maturité (FIT), jamais d'intention — voir CATEGORY_TO_GROUP."""
    name = "social_presence"
    source_kind = "social"

    def collect(self, prospect):
        signals = []
        for link in prospect.social_links.filter(is_active=True):
            signals.append(_signal(
                prospect, f"social_presence_{link.platform}", "contactability",
                f"Présence {link.get_platform_display()} confirmée",
                value=link.url, source_url=link.source_url or link.url,
                evidence=f"Lien {link.get_platform_display()} découvert : {link.url}",
                confidence=75, score_impact=3, positive=True, source_kind=self.source_kind,
            ))
        return signals


class DecisionMakerSignalCollector(SignalCollector):
    """Nouvelle source : un décideur identifiable (ContactPerson) n'était
    traduit en aucun signal jusqu'ici. Reste un indicateur de contactabilité
    (FIT) — on ne sait pas si ce contact vient d'être nommé ou existe depuis
    des années, donc on n'en fait jamais un signal d'INTENT (mission 6,
    section 5 : ne jamais surinterpréter)."""
    name = "decision_maker"
    source_kind = "website"

    def collect(self, prospect):
        from .predictneed_scoring import RELEVANT_ROLES

        signals = []
        for contact in prospect.contact_people.filter(is_active=True).exclude(job_title=""):
            title = contact.job_title.lower()
            if not any(role in title for role in RELEVANT_ROLES):
                continue
            signals.append(_signal(
                prospect, "decision_maker_identified", "contactability",
                f"Décideur identifié : {contact.job_title}",
                value=contact.full_name, source_url=contact.source_url or contact.profile_url,
                evidence=f"{contact.full_name or 'Contact'} — {contact.job_title}",
                confidence=contact.confidence_score, score_impact=5, positive=True,
                source_kind=self.source_kind,
            ))
        return signals


class SiteChangeSignalCollector(SignalCollector):
    """Correctif d'audit (round 2) : la version précédente concluait qu'une
    technologie était "nouvelle" simplement parce qu'une AUTRE technologie du
    même prospect était ancienne — ça ne prouve pas l'ABSENCE de cette
    technologie lors d'un scan antérieur (elle a pu être présente mais mal
    détectée, ou ajoutée par un changement de notre propre logique de
    détection, pas du site du prospect).

    Corrigé : réutilise `SiteAuditSummary.technologies` (JSONField déjà
    existant, une vraie photographie datée par `crawl_run`/`created_at` à
    chaque "Audit multi-pages") — aucun deuxième système de snapshots créé.
    Compare les DEUX derniers `SiteAuditSummary` du même prospect : une
    technologie n'est "nouvelle" QUE si elle est ABSENTE du snapshot
    précédent ET présente dans le snapshot actuel — une preuve à deux états
    comparables, pas une déduction. S'il n'existe pas deux audits distincts
    pour ce prospect, aucun signal (silence plutôt qu'invention).

    Correctif d'audit (round 3) — comparaison stricte : ne compare que des
    audits FIABLES. Un `CrawlRun` en échec (`status != "done"`) ne prouve
    rien (le crawl peut s'être arrêté avant d'avoir vu la page qui porte la
    technologie) — de tels audits sont exclus de la comparaison, jamais
    utilisés comme preuve d'absence ou de présence. Les deux audits doivent
    aussi porter sur le même site (`start_url` identique) et avoir une
    couverture suffisamment comparable (`pages_crawled`, au moins
    `MIN_COVERAGE_RATIO` du plus grand des deux) — comparer un scan de 2
    pages à un scan de 40 pages ne prouve rien non plus. Hors de ces
    conditions : aucun signal, jamais une confiance dégradée qui laisserait
    croire à une preuve partielle."""
    name = "site_change"
    source_kind = "website"

    MIN_COVERAGE_RATIO = 0.5

    def collect(self, prospect):
        from ..models import SiteAuditSummary

        summaries = list(
            SiteAuditSummary.objects.filter(prospect=prospect, crawl_run__status="done")
            .select_related("crawl_run").order_by("-created_at")[:2]
        )
        if len(summaries) < 2:
            return []

        current, previous = summaries
        if current.crawl_run.start_url != previous.crawl_run.start_url:
            return []
        if not self._comparable_coverage(current.crawl_run, previous.crawl_run):
            return []

        newly_appeared = sorted(set(current.technologies or []) - set(previous.technologies or []))
        if not newly_appeared:
            return []

        signals = []
        for technology in newly_appeared:
            signals.append(_signal(
                prospect, f"site_change_{technology}", "timing",
                f"Nouvelle technologie détectée sur le site : {technology}",
                value=technology, source_url=prospect.website,
                evidence=(
                    f"Absente de l'audit du {previous.created_at:%d/%m/%Y}, "
                    f"présente dans celui du {current.created_at:%d/%m/%Y}."
                ),
                confidence=75, score_impact=8, positive=True,
                source_kind=self.source_kind, signal_group="intent", observed_at=current.created_at,
            ))
        return signals

    def _comparable_coverage(self, current_run, previous_run):
        a, b = current_run.pages_crawled, previous_run.pages_crawled
        if a == 0 or b == 0:
            return False
        return min(a, b) / max(a, b) >= self.MIN_COVERAGE_RATIO


class RecentActivitySignalCollector(SignalCollector):
    """Recrutement Growth/Marketing/CRO récent ou actualité récente liée
    acquisition/conversion/analytics.

    Lit des preuves déjà déposées dans `ProspectEvidence` (registre générique
    d'enrichissement déjà existant — aucune nouvelle table) dont
    `raw_payload["event_date"]` porte une date d'événement RÉELLE explicite
    — jamais `collected_at` (date à laquelle ProspectPilot a collecté le
    fait, pas date à laquelle le fait s'est produit). Sans date réelle
    explicite, aucun signal n'est créé : silence plutôt qu'invention.

    ÉTAT (mission 7E) — désormais ACTIF et dans `DEFAULT_COLLECTORS` : le
    site propre du prospect (`CompanyWebsiteSource`, via
    `crawler.py::_extract_temporal_events`) alimente réellement
    `job_posting_growth`/`news_acquisition`/`dated_content_published` à
    partir de dates structurées JSON-LD (schema.org JobPosting/Article/
    BlogPosting/NewsArticle) ou, à défaut, d'une balise meta de date sur une
    page carrière/actualité reconnue — jamais une date devinée. Aucune
    connexion réseau ici — ce collecteur normalise une preuve déjà
    présente, il ne va pas la chercher.

    France Travail (api.francetravail.io, offres d'emploi avec date de
    publication officielle) documenté et prêt à alimenter
    `job_posting_growth` avec une confiance encore meilleure dès que des
    identifiants seront disponibles — voir services/france_travail.py,
    actuellement dormant. Jamais de scraping LinkedIn — explicitement
    exclu par la mission."""
    name = "recent_activity"
    source_kind = "open_web"

    FIELD_LABELS = {
        "job_posting_growth": "Recrutement Growth/Marketing/CRO récent",
        "news_acquisition": "Actualité récente liée acquisition/conversion/analytics",
        "dated_content_published": "Contenu éditorial récent (growth/marketing)",
    }
    # Audit correctif §7 — un article de blog SEO/SEA/marketing générique
    # n'est PAS un signal d'intention d'achat : une agence publie ce genre de
    # contenu en continu, que son besoin PredictNeed change ou non. Seuls un
    # recrutement Growth/CRO daté ou une actualité stratégique réellement
    # pertinente restent des signaux INTENT forts. `dated_content_published`
    # devient un signal FIT/activité (maturité éditoriale), jamais Intent —
    # donc jamais compté dans intent_score (voir intent_scoring.py, qui ne
    # lit que signal_group="intent").
    FIELD_CONFIG = {
        "job_posting_growth": {"category": "timing", "signal_group": "intent", "score_impact": 10},
        "news_acquisition": {"category": "timing", "signal_group": "intent", "score_impact": 10},
        "dated_content_published": {"category": "growth", "signal_group": "fit", "score_impact": 3},
    }

    def collect(self, prospect):
        signals = []
        for evidence in prospect.evidence_items.filter(field_name__in=self.FIELD_LABELS, is_current=True):
            event_date_raw = (evidence.raw_payload or {}).get("event_date")
            if not event_date_raw:
                continue
            try:
                observed_at = datetime.fromisoformat(str(event_date_raw))
            except (ValueError, TypeError):
                continue
            if timezone.is_naive(observed_at):
                observed_at = timezone.make_aware(observed_at)

            config = self.FIELD_CONFIG[evidence.field_name]
            signals.append(_signal(
                prospect, f"activity_{evidence.field_name}", config["category"],
                self.FIELD_LABELS[evidence.field_name],
                value=evidence.value, source_url=evidence.source_url,
                evidence=evidence.notes or evidence.value,
                confidence=evidence.confidence_score, score_impact=config["score_impact"], positive=True,
                source_kind=self.source_kind, signal_group=config["signal_group"], observed_at=observed_at,
            ))
        return signals


# Mission 7E : RecentActivitySignalCollector rejoint DEFAULT_COLLECTORS —
# CompanyWebsiteSource (enrichment.py) crée désormais réellement des
# ProspectEvidence(field_name="job_posting_growth"/"news_acquisition"/
# "dated_content_published") à partir de dates structurées trouvées sur le
# site du prospect. Voir la docstring de la classe pour le détail de la
# source.
DEFAULT_COLLECTORS = [
    TechnologySignalCollector(),
    QuickScanSignalCollector(),
    SocialPresenceSignalCollector(),
    DecisionMakerSignalCollector(),
    SiteChangeSignalCollector(),
    RecentActivitySignalCollector(),
]


def run_signal_collectors(prospect, collectors=None):
    """Point d'entrée unique : exécute chaque collecteur, isole les erreurs
    (un collecteur en échec ne doit jamais faire échouer les autres ni le
    pipeline appelant), et persiste tout via persist_signals — donc avec la
    même déduplication par empreinte que le reste de l'application."""
    from .signals import persist_signals

    collectors = DEFAULT_COLLECTORS if collectors is None else collectors
    signal_objects = []
    errors = []
    for collector in collectors:
        try:
            signal_objects.extend(collector.collect(prospect))
        except Exception as exc:  # noqa: BLE001 — isolation volontaire entre collecteurs
            errors.append(f"{collector.name}: {exc}")
    saved = persist_signals(prospect, signal_objects)
    return saved, errors
