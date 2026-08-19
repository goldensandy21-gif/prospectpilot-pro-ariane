# Architecture du moteur d'acquisition ProspectPilot

Ce document explique comment ProspectPilot trouve, qualifie, priorise et contacte des
entreprises pour le compte de PredictNeed IA — et où se trouve chaque brique dans le
code. Il est écrit pour être compréhensible sans lire le code.

Écrit avant la Mission 6 (Signal Intelligence + Intent + LinkedIn), pour servir de
carte avant toute nouvelle construction — règle absolue de la mission : **ne jamais
créer une deuxième version de quelque chose qui existe déjà**.

---

## 1. Le parcours en une phrase

```
Recherche → Qualification → Signaux → Scoring → Prospects → Campagnes → Engagement → Conversion
```

- **Recherche** : on interroge le registre officiel des entreprises (API Recherche
  d'Entreprises) selon des critères (secteur, taille, zone).
- **Qualification** : on élimine tout de suite ce qui ne correspond pas à l'ICP
  (le profil-cible du produit).
- **Signaux** : pour les entreprises qui restent, on regarde leur site web, leurs
  outils techniques, leurs coordonnées publiques — et on note ce qu'on observe comme
  autant de "signaux" horodatés et sourcés.
- **Scoring** : les signaux sont combinés en scores (aujourd'hui : un score global
  PredictNeed 0-100 + un grade A/B/C/D).
- **Prospects** : l'utilisatrice choisit explicitement quelles entreprises rejoignent
  la vraie liste de travail (rien n'y entre automatiquement).
- **Campagnes** : on regroupe des Prospects sélectionnés dans une campagne, avec un
  produit, un ICP et une séquence d'e-mails.
- **Engagement** : chaque interaction (clic, visite PredictNeed, simulateur,
  inscription...) est enregistrée.
- **Conversion** : quand PredictNeed confirme un client payant, le revenu (MRR) est
  attribué à la campagne, au signal, à l'e-mail d'origine.

---

## 2. Cartographie détaillée (avant Mission 6)

Légende : **EXISTE** / **EXISTE PARTIELLEMENT** / **ABSENTE** / **LEGACY À NE PAS UTILISER**

| Fonctionnalité | État | Où | Détail |
|---|---|---|---|
| Modèle de signal | **EXISTE** | `prospects/models/acquisition.py::ProspectSignal` | `signal_type`, `category`, `label`, `value`, `source_url`, `evidence`, `confidence`, `score_impact`, `positive`, `detected_at`, `last_checked_at`. C'est la table canonique — Mission 6 doit l'étendre, jamais la dupliquer. |
| Extraction de signaux | **EXISTE PARTIELLEMENT** | `prospects/services/signals.py` | `build_signals_from_technologies()` et `build_signals_from_quick_scan()` produisent des signaux à partir des technologies détectées et du quick scan. Couvre analytics/publicité/CRM/conversion. Ne couvre pas encore : changements d'entreprise, activité LinkedIn/réseaux sociaux, réutilisation d'EngagementEvent comme source de signal. |
| Persistance des signaux | **EXISTE, mais dédup insuffisante** | `signals.py::persist_signals()` | `ProspectSignal.objects.update_or_create(prospect, signal_type)` — un signal du même **type** écrase toujours le précédent, même si c'est un événement réellement différent dans le temps (ex. recrutement Growth en janvier vs campagne Growth en août). Section 3 de la mission : corriger. |
| Provenance / preuves | **EXISTE** | `ProspectSignal.source_url`, `.evidence`, `.confidence` ; `ProspectEvidence` (preuves génériques multi-champs) | Chaque signal a déjà une preuve et une URL source. Rien à dupliquer. |
| Scoring | **EXISTE** | `prospects/services/predictneed_scoring.py::score_prospect()` | Combine `icp_fit_score`, `need_score`, `acquisition_maturity_score`, `contactability_score`, `timing_score` (pondérés par `ICPProfile.weights`) en `predictneed_acquisition_score` + `predictneed_grade` (A/B/C/D). **`need_score` et `timing_score` sont déjà des embryons d'INTENT** — ils lisent `ProspectSignal.category in ["conversion","growth"]` et `category="timing"`. Mission 6 doit les faire évoluer vers un `intent_score` explicite et pondéré par fraîcheur, pas les dupliquer. |
| Fraîcheur / timing des signaux | **ABSENTE** | — | Aucune notion de poids qui diminue avec l'âge du signal. `timing_score` existe comme sous-score mais ne dépend pas de la date. À construire (section 4/mission). |
| Engagement | **EXISTE** | `EngagementEvent` + `prospects/services/tracking.py`, `campaign_click` (vue) | Déjà un modèle dédié, déjà utilisé pour les clics de campagne et les événements PredictNeed (webhook HMAC). Rien à recréer — juste à agréger en `engagement_score`. |
| Timeline commerciale | **EXISTE** | `prospects/services/commercial_timeline.py::build_prospect_timeline()` | Construit une liste chronologique à partir de `Prospect.created_at`, `SearchCandidate`, `CrawlRun`, `PublicEmail`, `EmailSend`, `EngagementEvent`, `ConversionEvent`. Une seule fonction, un seul format — à étendre (signal détecté, LinkedIn), jamais dupliquer. |
| Recommandation commerciale (next best action) | **EXISTE PARTIELLEMENT** | `AgentBrief.next_best_action` (champ), calculé dans `prospects/services/agent_brief.py::generate_agent_brief()` | Aujourd'hui : simple table `{grade: phrase libre}` (`"A": "Contacter avec un e-mail personnalisé..."`). Pas de code d'action structuré (WAIT/LINKEDIN_CONNECT/...), pas de raison/confiance/signal déclencheur explicites. Le champ et l'endroit où l'écrire existent déjà — à faire évoluer, pas à dupliquer. |
| URL LinkedIn entreprise | **EXISTE** | `PublicSocialLink(platform="linkedin")` | Déjà détecté automatiquement lors du quick scan (`platform_for_url` dans `crawler.py` reconnaît linkedin.com). Base de la Mission 6 pour "entreprise LinkedIn". |
| URL LinkedIn décideur | **EXISTE** | `ContactPerson.profile_url` | Champ générique déjà présent, déjà prévu pour ça. Pas de nouveau modèle nécessaire. |
| Contacts / décideurs | **EXISTE** | `ContactPerson` | `full_name`, `job_title`, `email`, `phone`, `profile_url`, `confidence_score`, `verification_status`. Utilisé aujourd'hui surtout pour l'e-mail nominatif ; peu peuplé en volume réel (1 seul enregistrement en production à ce jour). |
| Journal de contact LinkedIn | **EXISTE PARTIELLEMENT** | `ContactLog(channel="linkedin", outcome=...)` | Le choix `channel="linkedin"` existe déjà dans le modèle, mais rien ne l'utilise encore. Pas de machine à états (invitation préparée/envoyée/acceptée/refusée, message préparé/envoyé). À étendre pour la Mission 6, jamais recréer un journal parallèle. |
| Campagnes | **EXISTE, mono-canal e-mail uniquement** | `Campaign`, `CampaignProspect`, `EmailSequence/EmailStep/EmailVariant`, `prospects/services/campaign_sending.py` | Aucune notion de canal (email vs LinkedIn), aucune notion d'attente/condition/stop autre que le statut. Mission 6 doit étendre `Campaign`/`CampaignProspect`, jamais créer un deuxième moteur de campagne. |
| Réponses | **EXISTE (léger)** | `response_board` (vue), `ContactLog.outcome="replied"` | Tableau de bord simple des réponses manuelles. Pas de détection automatique de réponse LinkedIn (normal : aucune intégration LinkedIn encore branchée). |
| Conversion | **EXISTE** | `ConversionEvent` | Alimenté par le webhook HMAC PredictNeed → ProspectPilot (signup/paying/cancelled). Rien à changer structurellement. |
| Attribution de revenu | **EXISTE** | `RevenueAttribution` | Un-à-un avec `ConversionEvent`, capture MRR/valeur d'abonnement, ICP, séquence, variant e-mail. Déjà prêt pour mesurer la performance par signal si on y ajoute un lien vers le signal déclencheur (Mission 6, section 17). |
| Tâches Celery / planification | **EXISTE** | `prospects/tasks.py`, `django_celery_beat` (DatabaseScheduler) | Infrastructure de tâches planifiées déjà en place et déjà utilisée (`scheduled_refresh_top_prospects`, `run_scheduled_search_preset_task`). Réutilisable telle quelle pour les alertes (section 15) — pas besoin d'un nouveau système de planification. |
| Alertes | **ABSENTE** | — | Aucun modèle, aucune notification. À construire en s'appuyant sur Celery Beat existant. |
| Dashboard | **EXISTE** | `prospects/views.py::dashboard`, `templates/prospects/dashboard.html` | Cockpit déjà basé sur les champs PredictNeed (`predictneed_acquisition_score`, `predictneed_grade`, `predictneed_stage`, `outbound_eligible`) depuis la Mission 5. Prêt à recevoir FIT/INTENT/ENGAGEMENT sans refonte. |
| Liste Prospects | **EXISTE** | `prospects/views.py::prospect_list`, `templates/prospects/list.html` | Filtres rapides déjà en place (grade, avec e-mail, statut). Prêt à recevoir de nouvelles colonnes/filtres sans refonte. |
| Fiche Prospect | **EXISTE** | `prospects/views.py::prospect_detail`, `templates/prospects/detail.html` | Section "Pourquoi prospecter cette entreprise ?" déjà présente avec score, sous-scores, signaux, AgentBrief. Prête à accueillir FIT/INTENT/ENGAGEMENT et la timeline étendue. |
| Moteur e-mail legacy ProspectPilot | **LEGACY À NE PAS UTILISER** | `prospects/services/emailing.py`, `prospects/views.py::email_preview/email_send` | Conservé uniquement pour compatibilité historique (Mission 5, partie 2) — déplacé en bas de la fiche Prospect sous "Outils hérités". Ne jamais y raccrocher une nouvelle fonctionnalité. |
| Score legacy `priority_score` | **LEGACY À NE PAS UTILISER** | `Prospect.priority_score` | Remplacé par `predictneed_acquisition_score` comme système principal depuis la Mission 5. Toujours en base pour compatibilité, ne plus l'utiliser comme référence. |

---

## 3. Décisions de conception pour la Mission 6 (pas de nouvelle couche)

- **FIT** = expose clairement `Prospect.icp_fit_score` (déjà calculé par
  `compute_icp_fit_score()`), pas un nouveau champ.
- **INTENT** = nouveau champ `Prospect.intent_score`, calculé par un nouveau service
  canonique (`services/intent_scoring.py`) qui lit les `ProspectSignal` du groupe
  `intent`, pondérés par fraîcheur — remplace la logique implicite de `need_score`/
  `timing_score` pour ce rôle précis, sans supprimer ces deux champs (compatibilité
  du score PredictNeed existant).
- **ENGAGEMENT** = nouveau champ `Prospect.engagement_score`, calculé à partir des
  `EngagementEvent` existants — aucun nouveau modèle d'événement.
- **PRIORITÉ** = `Prospect.predictneed_acquisition_score` reste le score canonique
  affiché comme "Priorité" — pas de cinquième score concurrent. Sa formule peut
  évoluer pour intégrer intent_score/engagement_score dans une mission ultérieure ;
  la Mission 6 ne la modifie pas pour limiter le risque de régression sur les scores
  déjà en production.
- **Next Best Action** = extension de `agent_brief.py`, toujours écrit dans
  `AgentBrief.next_best_action`, avec un nouveau service qui retourne un code
  structuré (WAIT/WATCH/LINKEDIN_CONNECT/...) + raison + confiance + signal
  déclencheur, au lieu d'une simple phrase par grade.
- **LinkedIn** = aucun nouveau modèle de contact. `PublicSocialLink` porte
  l'entreprise, `ContactPerson.profile_url` porte le décideur, `ContactLog` est
  étendu (nouveaux `outcome` LinkedIn) pour journaliser l'orchestration.
- **Multicanal** = `CampaignProspect` est étendu (pas un deuxième `Campaign`) avec
  l'état de séquence courant.

---

## 4. Comment comprendre ProspectPilot en 5 minutes

```
   TROUVER
      |
      v
 QUALIFIER / FIT        <- ICP, secteur, taille, localisation, technologies
      |
      v
 DETECTER LES SIGNAUX    <- site, technologies, réseaux sociaux, engagement
      |
      v
    INTENT               <- signaux récents pertinents, pondérés par fraîcheur
      |
      v
   ENGAGEMENT             <- clics, visites PredictNeed, simulateur, inscription
      |
      v
   PRIORISER              <- score global + grade, "next best action"
      |
      v
 EMAIL / LINKEDIN         <- campagne multicanal, une action à la fois
      |
      v
  PREDICTNEED             <- le prospect utilise réellement le produit
      |
      v
 CLIENT / MRR             <- conversion + revenu attribué
```

**Où trouver chaque chose dans l'interface :**
- **Dashboard** : vue d'ensemble — combien de prospects retenus, prêts à contacter,
  clients, MRR.
- **Trouver des prospects** : lancer une recherche (registre officiel) ou une
  recherche manuelle.
- **Prospects** : la vraie liste de travail — uniquement les entreprises
  explicitement sélectionnées, avec FIT/INTENT/ENGAGEMENT/Priorité et l'action
  recommandée.
- **Campagnes** : regrouper des prospects sélectionnés et les faire avancer dans une
  séquence (e-mail, bientôt LinkedIn).
- **Résultats** : ce qui a été gagné — clics, inscriptions, clients, revenu, par
  campagne/ICP/grade.
- **Réglages** : identité e-mail, Search Console, import.

## Formules FIT / INTENT / ENGAGEMENT (implémentées)

**FIT** = `Prospect.icp_fit_score` (inchangé, `services/predictneed_scoring.py`).
Structure de l'entreprise : secteur, NAF, taille, localisation, ICP. Aucun
nouveau champ, aucune nouvelle formule.

**INTENT** (`services/intent_scoring.py`, écrit dans `Prospect.intent_score`) :
```
score = 20 (base)
      + somme des impacts ACTUELS des signaux signal_group="intent"
        (impact actuel = score_impact brut × multiplicateur de fraîcheur,
         voir services/signal_freshness.py)
      + bonus de répétition si ≥ 2 signaux récents (< 7 jours) :
        min(20, (nb_signaux_récents - 1) × 4)
score = clip(score, 0, 100)
```
Ne lit JAMAIS les signaux `signal_group="fit"` (un outil analytics détecté
reste un indice de maturité, jamais une intention d'achat à lui seul).

**Fraîcheur** (`services/signal_freshness.py`, fonction unique
`signal_freshness()`) : 0-3j → ×1.0 ("très frais"), 4-7j → ×0.75 ("frais"),
8-30j → ×0.5 ("récent"), 31-90j → ×0.2 ("ancien"), au-delà → ×0.0
("obsolète", la ligne reste en base mais ne pèse plus).

**ENGAGEMENT** (`services/engagement_scoring.py`, écrit dans
`Prospect.engagement_score`) :
```
score = somme, pour chaque EngagementEvent, de :
        poids_du_type_évènement × multiplicateur_de_fraîcheur(occurred_at)
```
Poids : link_clicked 10, product_visited 15, simulator_started 20,
simulator_completed 30, signup_started 25, signup_completed 40,
checkout_started 45, subscription_activated 60, subscription_cancelled -30.
`email_sent`/`email_failed` exclus (action sortante, pas un engagement du
prospect). Aucun événement enregistré → score 0 (jamais une valeur neutre
inventée).

**PRIORITÉ** = `Prospect.predictneed_acquisition_score` (inchangé, affiché
"Priorité"). FIT/INTENT/ENGAGEMENT sont ses composantes explicables, pas un
cinquième score concurrent.

**IN MARKET NOW** (`services/in_market_status.py`) : statut calculé à partir
d'`intent_score` (0-19 aucun signal, 20-39 faibles, 40-59 émergents, 60-79
probable, 80-100 forte), toujours formulé au conditionnel ("Signaux
compatibles avec une intention d'achat probable."), jamais une affirmation
absolue.

**NEXT BEST ACTION** (`services/next_best_action.py`, écrit dans le champ
existant `AgentBrief.next_best_action`) : arbre déterministe — exclusions/
opt-out → STOP ; réponse en attente de suivi → FOLLOW_UP ; contact très
récent sans réponse → WAIT (délai de politesse) ; puis selon `intent_score`
et les canaux disponibles (LinkedIn via `ContactPerson.profile_url` /
`PublicSocialLink`, e-mail via `PublicEmail`) → LINKEDIN_CONNECT /
LINKEDIN_MESSAGE / EMAIL / WATCH ; sinon NURTURE (bon fit, pas d'intent) ou
WAIT.

**LinkedIn** (`services/linkedin_provider.py` + `linkedin_orchestration.py`) :
provider par défaut `ManualLinkedInProvider` — prépare le contenu, n'envoie
jamais réellement, aucun bot Selenium/Playwright. `MockLinkedInProvider`
réservé aux tests. Tout persisté dans `ContactLog(channel="linkedin")` avec
les états `invitation_prepared/sent/accepted/declined`, `message_prepared`.

## Architecture SignalCollector (section 8)

`services/signal_collectors.py` — interface commune `SignalCollector.collect(prospect)`,
qui renvoie des `ProspectSignal` non sauvegardés, tous persistés par
`run_signal_collectors()` via `persist_signals()` (donc avec la même
déduplication par empreinte que tout le reste). Quatre collecteurs, pas
quinze : deux encapsulent des fonctions déjà existantes sans nouvelle
logique (`TechnologySignalCollector`, `QuickScanSignalCollector`), deux sont
réellement nouveaux parce que la donnée existait déjà mais ne produisait
encore aucun signal :

- `SocialPresenceSignalCollector` — une présence sociale confirmée
  (`PublicSocialLink`) devient un signal de contactabilité/FIT. Câblé dans
  `acquisition_pipeline.py::_finalize_candidate`, juste après la création
  des `PublicSocialLink`.
- `DecisionMakerSignalCollector` — un décideur identifié avec un intitulé de
  poste pertinent (`ContactPerson.job_title`) devient un signal de FIT,
  jamais d'INTENT (on ne sait pas si ce contact vient d'être nommé). Testé
  et prêt, mais **pas encore câblé en production** : aucun code actuel ne
  renseigne `ContactPerson.job_title` (vérifié par recherche exhaustive dans
  `enrichment.py`/`tasks.py`/`views.py`) — le câbler maintenant produirait un
  collecteur mort. À relier le jour où une source alimente ce champ.

Délibérément exclus : `ProspectEvidence` en tant que collecteur générique
(bruiterait avec des doublons de PublicEmail/PublicPhone/ContactPerson déjà
couverts), et `SearchConsoleMetric`/PageSpeed (pipeline d'audit technique
historique, séparé du parcours unifié Mission 5, `SearchConsoleMetric` n'a
même pas de clé étrangère vers `Prospect`).

## État d'avancement de la Mission 6

Réalisé et testé (214 tests, migrations 0007-0009 vérifiées sur Postgres 18
réel) : consolidation ProspectSignal (dédoublonnage par empreinte), fraîcheur
canonique, scores INTENT/ENGAGEMENT, statut IN MARKET NOW, Next Best Action
structurée, orchestration LinkedIn (provider manuel/mock), timeline étendue,
architecture SignalCollector, tests de non-régression et de protection des
données.

Restant (non commencé à ce stade) : séquences multicanal sur
Campaign/CampaignProspect (section 11), garde-fous de génération de message
(section 16), alertes Celery (section 15), tableaux d'analytics
(section 17), mise à jour des templates Prospects/fiche prospect (section
14), tests UX en navigateur et scénario métier bout-en-bout complet avec
vérification visuelle (section 20).
