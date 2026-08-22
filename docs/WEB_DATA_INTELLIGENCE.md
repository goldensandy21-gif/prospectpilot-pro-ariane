# Web Data Intelligence — audit avant code (Mission 7, Phase B)

Ce document liste, pour chaque brique nécessaire à Mission 7B–7E, ce qui existe déjà dans
`prospectpilot-pro-ariane`, pour décider quoi réutiliser/étendre. Règle absolue : **aucune
nouvelle structure parallèle** à `Prospect`/`ContactPerson`/`PublicEmail`/`PublicPhone`/
`PublicSocialLink`/`ProspectEvidence`/`EnrichmentSource`/`EnrichmentRun`/`ProspectSignal`/
`SearchCandidate` — uniquement des extensions.

Légende : ✅ existe · 🟡 existe partiellement · ⛔ manque · 🗄️ legacy (séparé, ne pas mélanger) ·
➕ doit être étendue.

## 1. Crawler — `prospects/services/crawler.py`

✅ **Existe** : `crawl_site()` fait un BFS respectueux de `robots.txt` (`RobotsPolicy`), profondeur
≤3, `max_pages` (défaut 12), délai entre requêtes (`CRAWL_DELAY_SECONDS`/`Crawl-delay`), extraction
email/téléphone/réseaux sociaux/formulaires/technologies par page (`analyze_html`). `IMPORTANT_PATHS`
couvre déjà `/contact`, `/mentions-legales`, `/a-propos`, `/equipe`, `/carrieres`.

➕ **À étendre** : `IMPORTANT_PATHS` ne couvre pas blog/actualités/presse/pages auteurs/sitemap.
Aucune lecture de `sitemap.xml` (donc pas de `lastmod` exploitable). `same_domain()` est naïf
(compare seulement après suppression de `www.`, pas de vraie comparaison eTLD+1 malgré `tldextract`
déjà présent en dépendance).

🟡 **Limite connue** : `robots.py` *fail open* si `robots.txt` est injoignable (traité comme
"tout est autorisé") — accepté tel quel pour cette mission (comportement pré-existant, hors
scope), mais documenté.

## 2. `quick_scan` — `prospects/services/quick_scan.py`

✅ **Existe** : version allégée du crawler (page d'accueil + jusqu'à `max_pages`, défaut 5),
priorise déjà les vraies pages internes trouvées sur l'accueil qui matchent `is_important_url()`
(contact/mentions-légales/à-propos/équipe) **avant** les URLs devinées commerciales
(tarifs/devis/services). Réutilise les mêmes regex que `crawler.py` (aucune duplication
d'extraction). Respecte `robots.txt` en amont (bloque tout le scan si interdit).

➕ **À étendre** : même liste de chemins prioritaires que le crawler à enrichir (blog/presse/
carrières déjà couvert/pages auteurs).

## 3. Enrichment — `prospects/services/enrichment.py`, `EnrichmentEngine`

✅ **Existe** : état `EnrichmentRun` (`queued/running/done/failed`) réellement modélisé et utilisé
(`running→done|failed`) ; `EnrichmentSource` avec `source_type`/`api_key_env` ; sources exécutées
dans l'ordre (`PublicRegistrySource`, `CompanyWebsiteSource`, `CommonCrawlSource`), confiance
calculée par source ; `store_evidence()` est le point d'écriture unique vers `ProspectEvidence`
(dédup par confiance sur `(prospect, field_name, normalized_value)`).

🗄️ **Legacy/stub** : `dropcontact`/`apollo`/`kaspr`/`lemlist` (`ExternalB2BProviderSource`) ne font
strictement **aucun appel réseau** — juste une preuve placeholder si la clé d'env existe. Ne pas y
toucher dans cette mission (hors scope, nécessiterait de vrais contrats fournisseurs).

➕ **À étendre** : ajouter de nouvelles sources (people/emails/signaux temporels) comme de
**nouvelles classes de source** dans `SOURCE_CLASSES`, jamais comme un système parallèle.
`store_evidence()` ne sait dispatcher que vers email/téléphone/formulaire/réseau social — il n'a
pas d'équivalent `store_person()` pour créer un `ContactPerson` autrement qu'en inférant un nom
depuis un e-mail personnel (`split_person_from_email`).

## 4. Common Crawl — `prospects/services/commoncrawl.py`

✅ **Existe** : `open_web_presence()` interroge l'index CDX Common Crawl (captures same-domain),
alimente `BacklinkSnapshot` (vue "Résultats") et une preuve `ProspectEvidence` générique (confiance
fixe 45). `referring_domains` est **toujours vide** — documenté comme non implémenté (nécessiterait
le graphe WAT/WARC, hors scope).

⛔ **Hors scope Mission 7** : ne pas construire un vrai graphe de backlinks — non demandé.

## 5. Site discovery — `prospects/services/site_discovery.py`

✅ **Existe** : `discover_official_site()` est déjà **basé sur des preuves réelles** (résolution DNS,
tokens du nom d'entreprise dans le titre/texte, ville, SIREN/SIRET trouvés sur la page, vocabulaire
de mentions légales) avec un score de confiance 0–100 et `needs_manual_review`. Rien à reconstruire.

## 6. ProspectEvidence — `prospects/models/core.py`

✅ **Existe et suffit** : `field_name` libre, `value_type` (choices), `confidence_score`,
`verification_status` (choices : `unverified/format_valid/deliverability_unknown/verified/invalid/
blocked/conflict`), `source_url`, `raw_payload` (JSON), `collected_at`/`last_checked_at`. C'est
exactement le contrat "URL source + date + confiance + méthode" demandé par la mission — déjà là.

➕ **À étendre** : `verification_status` doit gagner deux valeurs pour distinguer proprement les
niveaux Email Finder demandés : `public_source_confirmed` (email A) et `domain_mx_valid` (email C).
`pattern_inferred` doit aussi être ajouté (email B). Migration additive (choices uniquement, comme
les rounds précédents).

## 7. PublicEmail — `prospects/models/core.py`

🟡 **Existe partiellement** : `email_type`/`confidence_score`/`source_type`/`discovery_method`
existent, mais `verification_status` est un `CharField` **sans `choices=`** (contrairement à
`ProspectEvidence.VERIFICATION` que `ContactPerson` réutilise déjà). Trois implémentations
différentes de `classify_email_type()` existent (`enrichment.py`, `tasks.py`, `acquisition_pipeline.py`)
— dette déjà présente, **hors scope de correction complète ici** (risque de régression sur 3 call
sites existants non demandé par la mission), mais aucune quatrième copie ne sera ajoutée.

⛔ **Manque** : aucune vérification MX nulle part sur le chemin `PublicEmail` (le seul MX-check du
repo, `deliverability.py`, sert à diagnostiquer les domaines d'**envoi** de campagne, jamais les
emails de prospects). Aucune inférence de pattern de domaine.

➕ **À étendre** : `verification_status` gagne les mêmes choices que `ProspectEvidence.VERIFICATION`
(migration additive, valeurs existantes déjà compatibles). Nouveau module `email_intelligence.py`
ajoutant les niveaux A/B/C sans dupliquer `verify_email()` existant (l'appelle en interne).

## 8. ContactPerson — `prospects/models/core.py`

🟡 **Existe partiellement** : le modèle a déjà tous les champs nécessaires (`job_title`,
`profile_url`, `confidence_score`, `verification_status` réutilisant `ProspectEvidence.VERIFICATION`,
`raw_payload`), mais **le seul point de création dans tout le repo** est l'inférence de nom depuis un
email personnel (confiance plafonnée à 75, jamais de `job_title`).

⛔ **Manque totalement** : aucune extraction de personnes depuis les pages équipe/à propos ; aucun
parsing JSON-LD/schema.org (recherche exhaustive : zéro résultat pour `json-?ld|schema\.org|
microdata` dans tout `prospects/`) alors que `crawler.py`/`quick_scan.py` visitent déjà ces pages
sans les exploiter pour des personnes.

➕ **À construire** (Phase C) : nouveau service `people_extraction.py`, nouvelle classe de source
`TeamPageSource` dans `EnrichmentEngine.SOURCE_CLASSES`, et une nouvelle méthode
`EnrichmentEngine.store_person()` (miroir de `store_email()`) — toujours vers le modèle
`ContactPerson` existant, jamais un nouveau modèle.

## 9. PublicSocialLink — `prospects/models/core.py`

✅ **Existe et fonctionne déjà** : `platform_for_url()` détecte déjà les liens LinkedIn/Facebook/
Instagram/X trouvés sur les pages publiques de l'entreprise (jamais scrapés depuis LinkedIn
lui-même) et les persiste via 5 points d'écriture cohérents. `SocialPresenceSignalCollector` les
transforme déjà en signal FIT/contactabilité.

🟡 **Limite** : pas de `confidence_score` sur ce modèle (contrairement à `PublicEmail`/`PublicPhone`) ;
seulement 4 plateformes reconnues. Non bloquant pour Mission 7, pas de changement nécessaire.

## 10. SignalCollector — `prospects/services/signal_collectors.py`

✅ **Existe, architecture saine** : classe abstraite `SignalCollector`, 6 collecteurs concrets,
`DEFAULT_COLLECTORS` (liste manuelle, pas de registre dynamique — volontaire et suffisant), dédup
par empreinte unique via `persist_signals()`/`signal_fingerprint()` (`signals.py`) — un seul
algorithme d'empreinte dans tout le repo.

🟡 **`RecentActivitySignalCollector` dormant** : lit déjà `ProspectEvidence` filtrée sur
`field_name__in=["job_posting_growth","news_acquisition"]` avec exigence stricte d'une vraie
`raw_payload["event_date"]` (jamais de repli sur `collected_at`) — **exactement le contrat qu'une
vraie source temporelle doit remplir**. Volontairement exclu de `DEFAULT_COLLECTORS` tant qu'aucune
source réelle ne l'alimente.

➕ **À étendre** (Phase E) : élargir la liste `field_name__in` acceptée par ce collecteur pour couvrir
les nouveaux faits datés du site propre (ex. `career_page_new_posting`, `dated_content_published`),
puis l'ajouter à `DEFAULT_COLLECTORS` une fois qu'une vraie source écrit ces preuves.

## 11. SearchCandidate — `prospects/models/acquisition.py`

✅ **Existe, rien à changer** : étape technique pré-`Prospect` du funnel (`pending→...→converted`).
Reste tel quel — Mission 7 ne touche pas au funnel d'acquisition.

## 12. France Travail

⛔ **N'existe pas** : aucune référence dans le repo (recherche exhaustive) hormis un commentaire
documentant explicitement cette lacune dans `RecentActivitySignalCollector`. **Aucun identifiant
(`FRANCE_TRAVAIL_CLIENT_ID`/`SECRET`) n'est disponible dans cet environnement.**

➕ **Décision (conforme à la consigne mission)** : construire l'adaptateur (`france_travail.py`)
avec le contrat OAuth2 client-credentials correct (même schéma que `GOOGLE_OAUTH_CLIENT_ID/SECRET`
dans `config/settings.py`), entièrement **testable en mockant le HTTP**, mais **ne jamais l'appeler
en production tant que les identifiants ne sont pas configurés** — la source se déclare simplement
`enabled=False`/absente tant que les variables d'environnement manquent. Documenté comme
« préparé, dormant » dans le rapport final, pas comme actif.

## 13. Robots / rate limiting / backoff

✅ **Existe et suffit pour le scope actuel** : `robots.txt` respecté de façon cohérente dans
`crawler.py`/`quick_scan.py` via la même classe `RobotsPolicy` (pas de duplication). Le seul module
avec un vrai backoff exponentiel + gestion de `Retry-After` est `company_search.py` (API registre) —
suffisant pour cette mission, aucune source Web Intelligence nouvelle n'a besoin d'un débit plus
élevé que l'existant.

➕ **À réutiliser tel quel** : toute nouvelle requête HTTP (sitemap, pages équipe, France Travail)
passe par `httpx.Client(timeout=...)` avec le même style que le reste du fichier, respecte
`RobotsPolicy` quand elle cible le site du prospect, et applique un délai/cache identique à celui
déjà en place (`ACQUISITION_SITE_CACHE_HOURS`, `ACQUISITION_DOMAIN_DELAY_SECONDS`) pour ne jamais
rescanner un domaine plusieurs fois dans une courte fenêtre.

## 14. Celery / EnrichmentRun

✅ **Existe et suffit** : `EnrichmentRun` a un vrai état `queued/running/done/failed` ; les tâches
lourdes (`enrich_prospect_task`, `audit_site_task`, `discover_site_task`) passent déjà par Celery
avec retry (`autoretry_for`, `retry_backoff`). Toute nouvelle source lourde (people/emails/signaux
temporels) s'exécute **dans ces mêmes tâches existantes**, jamais de nouveau système de file
d'attente.

---

## Décisions Phase B–E (ce qui sera construit, et où)

| Besoin | Nouveau modèle ? | Où ça vit |
|---|---|---|
| Pages blog/presse/auteurs/sitemap | Non | `crawler.py`/`quick_scan.py` (`IMPORTANT_PATHS` étendu) + nouvelle fonction `sitemap_urls()` dans `crawler.py` |
| Extraction de personnes | Non | Nouveau `people_extraction.py` + `EnrichmentEngine.store_person()` + `ContactPerson` existant |
| Email pattern (niveau B) | Non | Nouveau `email_intelligence.py`, preuve stockée dans `ProspectEvidence` existant |
| Vérification email (niveau C, MX) | Non | `email_intelligence.py`, écrit `verification_status` sur `PublicEmail`/`ProspectEvidence` existants |
| Signaux temporels site propre | Non | Extraction dans `crawler.py`, preuve `ProspectEvidence`, lue par `RecentActivitySignalCollector` existant (liste `field_name` élargie) |
| France Travail | Non | Nouveau `france_travail.py` (adaptateur dormant), même contrat `ProspectEvidence`/collecteur ci-dessus |
| Interface Hunter (Entreprises/Personnes/Web Intelligence) | Non | Nouvelles vues Django lisant `Prospect`/`ContactPerson`/`ProspectEvidence` existants, sous `Trouver des prospects` |

Interdiction respectée : aucun `WebProspect`, `LinkedInProspect`, `HunterLead`, `PeopleLead`, ou
équivalent n'est créé.

---

## Bilan final (Phase G)

Implémenté et vérifié (phases B–F, commits séparés `mission7-web-data-intelligence`) :

- **Web Deep Discovery** : `IMPORTANT_PATHS`/`IMPORTANT_PAGE_TERMS` étendus (blog/actualités/
  presse/auteurs/collaborateurs) ; `sitemap_urls()` (avec `lastmod`, un seul niveau d'index,
  robots.txt respecté) ; `structured_data.py` (JSON-LD schema.org). **Réellement actif** dans le
  crawler et le quick scan (donc dans l'enrichissement et l'audit de site).
- **People Discovery** : `people_extraction.py` (schema.org Person + heuristique stricte sur page
  équipe reconnue) → `ContactPerson` via `EnrichmentEngine.store_person()`. **Réellement actif**
  (`company_website` fait partie de `DEFAULT_SOURCE_KEYS`).
- **Email Finder** : niveau A (`public_source_confirmed`), niveau B (`pattern_inferred`, ≥2
  exemples confirmés exigés, jamais écrit dans `ContactPerson.email`/`PublicEmail`), niveau C
  (`domain_mx_valid`, action explicite, jamais confondu avec "vérifié"). Niveaux A/B **actifs**
  automatiquement dans `enrich_prospect()` ; niveau C **actif** via un bouton explicite.
- **Signaux temporels** : `temporal_signals.py` classe les faits datés réels (JSON-LD, repli meta)
  vers `job_posting_growth`/`news_acquisition`/`dated_content_published`. `RecentActivitySignalCollector`
  **réellement actif** (rejoint `DEFAULT_COLLECTORS`) — testé A/B/C : sans date réelle, aucun
  Intent ; avec une date réelle, Intent avec `observed_at` exact.
- **France Travail** : adaptateur `france_travail.py` construit et testé (HTTP entièrement mocké),
  **dormant** — aucun identifiant `FRANCE_TRAVAIL_CLIENT_ID`/`SECRET` disponible dans cet
  environnement, `is_configured()==False`, aucune requête réseau tant que non configuré.
- **Interface Hunter** : `/web-intelligence/` (Entreprises/Personnes/Web Intelligence), intégrée au
  menu existant "Trouver des prospects", bulk (Ajouter aux Prospects / Enrichir / Ajouter à une
  campagne) qui passe systématiquement par les chemins déjà validés.

Vérification finale (Phase G) : 474 tests (382 hérités de mission 6 + 92 nouveaux mission 7),
`manage.py check` et `makemigrations --check` propres, migration 0015 (choices uniquement, seule
migration de cette mission) rejouée sur PostgreSQL 18 réel avec des données réalistes sur les 13
modèles listés en section 17 de la mission — PKs et comptages strictement identiques avant/après,
`format_valid` existant intact, nouveaux statuts (`public_source_confirmed`/`domain_mx_valid`)
utilisables en écriture/lecture réelles. Suite complète (474 tests) rejouée une seconde fois
directement sur PostgreSQL 18 (et non SQLite) — y compris le test de concurrence de campagne
(mission 6), qui ne peut s'exécuter que sur Postgres réel : tout est vert.
