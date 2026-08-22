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

## Séquences multicanal (section 11)

`EmailStep` porte maintenant `channel` (email / linkedin_connect /
linkedin_message) et `advance_condition` (always / linkedin_accepted), en
plus de `delay_days` (inchangé, continue de porter le délai depuis l'étape
précédente, quel que soit le canal). `CampaignProspect.current_step`
mémorise l'étape dont l'action a déjà été exécutée. `ContactLog` gagne
`campaign_prospect`/`email_step` (même principe que `EmailSend`), pour
savoir si une étape LinkedIn a déjà été exécutée pour une campagne donnée.
Aucun second modèle Campaign, aucune seconde table de séquence.

`services/campaign_sequencing.py::advance_campaign_prospect()` est le seul
point d'entrée : verrouille la ligne (`select_for_update`), vérifie d'abord
les conditions d'arrêt (réponse, conversion, désinscription/opposition,
DNC, client déjà payant), puis exécute AU PLUS UNE étape par appel — un
prospect ne peut donc jamais recevoir deux actions au même moment, et
rappeler la fonction avant que l'étape suivante ne soit prête ne fait rien
de plus qu'un `waiting`. Une invitation LinkedIn refusée/expirée ne bloque
jamais la séquence : l'étape "message" est sautée, l'étape suivante (email)
respecte son propre délai. `run_campaign_sequences(campaign)` fait avancer
tous les `CampaignProspect` éligibles d'une campagne.

## Garde-fous de personnalisation des messages (section 16)

`services/message_guardrails.py` : aucune génération libre de texte.
`SAFE_PHRASE_TEMPLATES` est un dictionnaire fermé `signal_type -> phrase
pré-approuvée`, seule source de vérité — un `signal_type` non répertorié ne
produit AUCUNE phrase (silence plutôt qu'invention). Chaque phrase reste au
niveau du fait observé, y compris pour les signaux INTENT (ex. "votre site
propose un formulaire de contact", jamais "vous cherchez à convertir plus").
`assert_no_overclaiming(text)` détecte les formulations d'intention non
prouvée ("vous cherchez", "vous voulez acheter"...) et sert à la fois de
garde-fou de test et de vérification appelable avant tout envoi réel.

Cas explicitement interdit et testé : un signal `analytics_detected` (FIT —
Google Analytics détecté) ne peut jamais produire une phrase mentionnant une
intention d'"analyse comportementale" (qui serait le signal, différent,
`behaviour_analytics_detected`). Le message LinkedIn généré par
`campaign_sequencing.py::_build_linkedin_message()` passe par cette même
fonction — pas de personnalisation ad hoc en dehors du dictionnaire fermé.

## Alertes (section 15)

Nouveau modèle `Alert` (models/acquisition.py) — justifié : aucun modèle
existant ne représente une NOTIFICATION pour l'utilisateur commercial
(`ProspectSignal` décrit un fait chez le prospect, `EngagementEvent` une
action du prospect ; `Alert` est la couche "ceci mérite l'attention
maintenant", une entité distincte des deux). Migration 0011 : simple
`CREATE TABLE`, aucune table existante modifiée — vérifiée sur Postgres 18
réel.

`services/alerts.py` — trois points d'entrée, câblés aux points de
persistance canoniques (jamais un job périodique qui rescanne tout et
spamme) :
- `check_signal_alerts()`, appelé depuis `persist_signals()` (le seul point
  d'écriture de `ProspectSignal`, quel que soit le pipeline d'origine) :
  alerte sur un signal `intent`/`engagement` réellement NOUVEAU et fort
  (impact ≥ 7), et sur une réactivation (≥ 30 jours sans signal ni
  engagement, basé sur `observed_at`, jamais `detected_at`).
- `check_intent_threshold_alert()`, appelé depuis
  `recompute_acquisition_scores()` : alerte uniquement sur une MONTÉE vers
  un niveau IN MARKET actionnable ("probable"/"forte") — jamais sur une
  baisse, jamais si le niveau ne change pas.
- `check_engagement_alert()`, appelé depuis `predictneed_webhook.py` après
  chaque nouvel `EngagementEvent` réel.

Dédoublonnage garanti par une contrainte unique
`(prospect, alert_type, dedup_key)` — `get_or_create` ne peut jamais créer
deux fois la même alerte pour le même événement.

## Analytics (section 17)

`services/signal_analytics.py` — aucune pseudo-IA qui réajuste des
pondérations automatiquement : uniquement des comptages/taux robustes,
réutilisant exclusivement `ProspectSignal`/`ContactLog`/`EngagementEvent`/
`ConversionEvent`/`RevenueAttribution` et `EmailStep.channel` (bloc C). Sept
fonctions : `signal_to_reply_counts`, `signal_to_click_counts`,
`signal_to_signup_counts`, `signal_to_client_counts` (les quatre partagent
un même helper générique `_signal_type_counts_for`, jamais quatre formules
différentes), `conversion_rate_by_channel`, `conversion_rate_by_intent_band`
(réutilise `IN_MARKET_LEVELS`, aucun second découpage de bandes),
`mrr_by_signal_type` et `mrr_by_channel` (réutilise
`RevenueAttribution.email_step.channel`). L'attribution MRR par signal est
volontairement multi-attribution (un prospect avec plusieurs signal_type
distincts contribue son MRR à chacun) mais ne compte jamais deux fois le
même signal_type pour un même prospect. Une future optimisation
automatique des poids pourra se construire sur ces données réelles — pas
avant.

## Interface (section 14)

Aucun nouveau menu principal — `base.html` inchangé (Dashboard | Trouver des
prospects | Prospects | Campagnes | Résultats | Réglages). Tout est intégré
dans les deux pages existantes :

- **Liste Prospects** (`templates/prospects/list.html`,
  `views.py::prospect_list`) : colonnes FIT / INTENT / ENGAGEMENT / Priorité
  / Dernier signal / Âge / Email / LinkedIn / Action recommandée. Filtres
  ajoutés : Intent minimum, Engagement minimum, signal &lt; 7j/30j, LinkedIn
  disponible, e-mail disponible, In Market (réutilise les mêmes bandes que
  `in_market_status.py`), Action recommandée (post-filtre Python, la NBA
  n'étant pas un champ stocké).
- **Fiche prospect** (`templates/prospects/detail.html`,
  `views.py::prospect_detail`) : nouvelle carte "Pourquoi contacter cette
  entreprise maintenant ?" — FIT/INTENT/ENGAGEMENT/PRIORITÉ, statut IN
  MARKET (phrase toujours hedgée), raisons INTENT/ENGAGEMENT dépliables,
  dernier signal avec âge et source, décideur identifié, action recommandée
  avec sa raison, lien LinkedIn si disponible. La timeline (déjà présente)
  affiche désormais aussi les signaux/étapes LinkedIn/recalculs de score du
  bloc précédent.

Vérifié visuellement en navigateur avec 3 prospects fixtures locales
(A/B/C, mêmes profils que les tests E2E de la section 20, supprimées après
vérification) : la liste et la fiche différencient clairement A ("Aucun
signal récent" / NURTURE), B (Intent 63, "Intention probable" /
LINKEDIN_CONNECT) et C (Intent 63 + Engagement 45, priorité maximale). Les
filtres `intent_min` et `nba` ont été testés en conditions réelles dans le
navigateur (2 puis 1 prospect affiché(s), comme attendu).

## Parcours métier complet (section 20)

`test_mission6_full_business_journey_e2e.py` : un seul prospect suivi de
bout en bout — Trouver → enrichir → signaux (SignalCollector) → Fit/Intent
→ sélection → campagne multicanal → LinkedIn (`MockLinkedInProvider`,
invitation → acceptation → message) → e-mail (backend de test Django,
jamais de SMTP réel) → engagement PredictNeed (webhook réel) → conversion
→ revenu (`RevenueAttribution`) → arrêt automatique de la séquence une fois
le prospect client payant. Vérifie la cohérence à chaque étape plutôt que
seulement l'état final.

## État d'avancement de la Mission 6

Réalisé et testé (289 tests, migrations 0007-0011 vérifiées individuellement
ET en une seule chaîne continue 0006→0011 sur Postgres 18 réel avec 80
prospects/160 signaux/16 contacts déjà en place — zéro perte de données,
zéro erreur) : consolidation ProspectSignal (dédoublonnage par empreinte),
fraîcheur canonique, scores INTENT/ENGAGEMENT, statut IN MARKET NOW, Next
Best Action structurée, orchestration LinkedIn (provider manuel/mock),
timeline étendue, architecture SignalCollector, séquences multicanal
Campaign/CampaignProspect, garde-fous de personnalisation des messages,
alertes, analytics par signal/canal/bande d'Intent, interface Prospects/
fiche prospect (vérifiée en navigateur), parcours métier bout-en-bout
complet, tests de non-régression et de protection des données.

Blocs A à H (dans l'ordre demandé) tous complétés et testés. Reste hors
scope de cette session : intégration LinkedIn via une véritable API
autorisée (le provider `manual`/`mock` reste la seule implémentation, par
design — aucune automatisation réelle n'a jamais été demandée), et
l'optimisation automatique des pondérations à partir de l'historique réel
(explicitement reportée par la mission elle-même, section 17).

## Correctifs suite à audit indépendant

Un audit indépendant a identifié 8 défauts fonctionnels non couverts par
les 289 tests initiaux. Tous corrigés, avec 32 nouveaux tests (321 au
total) :

1. **Sémantique INTENT** — `CATEGORY_TO_GROUP` classait "growth"/"conversion"
   (formulaire de contact, booking, lead magnet — des caractéristiques
   STATIQUES du site) comme intent. Corrigé : ces catégories sont
   maintenant FIT par défaut. Seule "timing" reste réservée à intent, et
   uniquement pour des signaux réellement temporels créés avec
   `signal_group="intent"` explicite par leur collecteur.
2. **Fraîcheur** — `_signal()`/`persist_signals()` remplaçaient un
   `observed_at` manquant par `timezone.now()`, faisant bénéficier à tort
   du multiplicateur "très frais" un fait dont la date réelle est inconnue.
   Corrigé : un signal `intent` sans date réelle explicite garde
   `observed_at=None` ("date inconnue", poids nul) ; un signal FIT continue
   de recevoir `now()` (observation d'un état présent, sans impact sur le
   score puisque FIT n'est jamais pondéré par fraîcheur).
3. **Vrais signaux temporels** — deux nouveaux collecteurs :
   `SiteChangeSignalCollector` (technologie apparue depuis un scan
   antérieur réel, datée par comparaison) et `RecentActivitySignalCollector`
   (recrutement Growth/actualité récente, lus depuis `ProspectEvidence`
   avec une date d'événement réelle explicite — sans date, aucun signal ;
   aucun scraping LinkedIn).
4. **Migration 0007** — corrigée alors qu'elle n'avait jamais été déployée :
   n'importe plus `prospects.services.signals` (mapping et fingerprint
   figés en copie locale dans la migration) ; `source_kind` historique
   distingue maintenant "technology" (catégories analytics/advertising/
   crm/competitor/acquisition) de "website" (le reste). Rechaîne complète
   0006→0011 revérifiée sur Postgres 18 réel avec 40 prospects/60 signaux
   d'origine mixte — zéro perte, zéro signal historique classé intent.
5. **Garde-fous de campagne restaurés** — `advance_campaign_prospect()`
   vérifie maintenant `campaign.is_sendable` en tout premier (aucune action
   sur une campagne brouillon/non validée), `daily_send_limit`/`total_limit`
   (tous canaux confondus), et la politique domaine/jour existante
   (`campaign_sending.py::_domain`) pour l'étape e-mail. Tests de
   contournement volontaire inclus (campagne draft, active-mais-non-validée,
   appels répétés, limites dépassées).
6. **Garde-fous appliqués aux e-mails** — `AgentBrief.detected_need`
   présentait `product.target_problem` (générique) comme un besoin DÉTECTÉ
   chez le prospect. Corrigé : signaux intent réellement observés cités
   nommément si présents, sinon le problème générique est explicitement
   étiqueté comme tel ("Aucun signal spécifique confirmé..."). Testé sur
   l'e-mail RÉELLEMENT rendu (`render_predictneed_email`), pas seulement
   sur le service qui le construit.
7. **Priorisation** — `predictneed_acquisition_score` ("Priorité")
   ignorait INTENT/ENGAGEMENT. Corrigé en étendant la formule pondérée
   existante (`DEFAULT_ICP_WEIGHTS` + `ICPProfile.effective_weights()`) :
   intent (15%) et engagement (10%) sont désormais des composantes du même
   score canonique — pas un cinquième score concurrent. Un ICPProfile
   existant sans ces clés en hérite automatiquement (rétrocompatible, sans
   migration). Tri par défaut de la liste Prospects corrigé pour utiliser
   explicitement `-predictneed_acquisition_score` (il retombait auparavant
   sur `Prospect.Meta.ordering`, basé sur l'ancien `priority_score`
   technique jamais mis à jour par PredictNeed IA).
8. **A/B/C refaits** — A a maintenant un site aussi mature que B (mêmes
   signaux FIT issus du quick scan) mais aucun événement temporel réel :
   Intent reste à 0, statut "Aucun signal récent", jamais "probable"/
   "forte". B ajoute un véritable événement daté (recrutement Growth via
   `RecentActivitySignalCollector`) : Intent clairement supérieur à A. C
   ajoute l'engagement PredictNeed réel : Engagement nettement supérieur à
   B. Ce test est la preuve que le moteur distingue désormais maturité
   (FIT) et intention actuelle (INTENT).

## Correctifs suite à audit indépendant, round 2

Un second audit a confirmé la majorité des correctifs du round 1 mais
trouvé 8 points supplémentaires. Tous corrigés, 18 nouveaux tests (346 au
total, dont 1 `TransactionTestCase` PostgreSQL) :

1. **Fraîcheur INTENT** — `signal_effective_impact()` retombait encore sur
   `detected_at` quand `observed_at` était `None`, y compris pour un signal
   intent. Corrigé : pour `signal_group="intent"`, `observed_at` est la
   SEULE date acceptée, jamais de repli. `compute_intent_score()` corrigé
   en profondeur : la base de 20 points ne s'applique plus dès qu'une ligne
   intent existe, mais seulement s'il existe au moins un signal à impact
   actuel non nul.
2. **Site Change** — `SiteChangeSignalCollector` concluait qu'une
   technologie était nouvelle simplement parce qu'une AUTRE technologie du
   prospect était ancienne, sans jamais prouver son absence lors d'un scan
   antérieur. Entièrement repensé : compare les deux `SiteAuditSummary` les
   plus récents d'un prospect (instantané réel et daté, déjà existant —
   aucun deuxième système de snapshots créé) ; absente du précédent ET
   présente dans l'actuel, sinon aucun signal.
3. **Alimentation réelle** — `RecentActivitySignalCollector` n'avait aucune
   source réelle et a été retiré de `DEFAULT_COLLECTORS` (documenté
   honnêtement comme dormant, candidate réelle nommée pour une prochaine
   session : API publique France Travail). `SiteChangeSignalCollector`,
   lui, est réellement branché dans le workflow de production
   (`tasks.py::audit_site_task`, juste après chaque `SiteAuditSummary`) —
   testé avec la tâche Celery réelle (mockée seulement pour le crawl HTTP).
4. **Recalcul temps réel** — `persist_signals()` et le webhook PredictNeed
   appellent maintenant `score_prospect()` (intent/engagement ET "Priorité"
   canonique) dès qu'un signal ou événement d'engagement réel apparaît.
   Lecture seule sur l'existant : aucune boucle, aucune écriture chez
   PredictNeed IA.
5. **Concurrence campagne** — `advance_campaign_prospect()` verrouille
   désormais explicitement `Campaign` (`select_for_update(of=("self",))`,
   nécessaire car `Campaign.sequence` est une FK nullable — PostgreSQL
   refuse `FOR UPDATE` sur le côté nullable d'un outer join sans cette
   restriction, confirmé par une erreur réelle avant correctif), en plus de
   `CampaignProspect` : les quotas `daily_send_limit`/`total_limit` sont
   globaux à la campagne, leur vérification doit donc être sérialisée à ce
   niveau. Prouvé par un `TransactionTestCase` à deux threads/connexions
   réels sur Postgres 18 (5/5 exécutions stables).
6. **Échec provider LinkedIn** — `ContactLog.OUTCOMES` gagne
   `invitation_failed`/`message_failed`, jamais convertis silencieusement
   en "préparé" (y compris pour un statut provider inconnu, traité
   fail-safe). Une étape en échec n'avance jamais `current_step` — reste
   rejouable (retry) et visible pour une intervention humaine.
7. **Poids ICP** — nouvelle migration explicite et déterministe (jamais une
   fusion+renormalisation silencieuse à la lecture) : les profils déjà
   personnalisés voient leurs 5 poids historiques ramenés à 75% du total en
   conservant leurs proportions relatives exactes, intent=15/engagement=10
   complètent les 25% restants. Les profils jamais personnalisés ne sont
   pas touchés. `seed_data.py` corrigé pour écrire directement les
   nouveaux poids.
8. **Migrations depuis la vraie baseline production** — chaîne complète
   0004→0013 (la baseline production connue avant Mission 5) testée en une
   seule fois sur Postgres 18 réel, avec des données représentatives sur
   les 9 modèles demandés (Prospect, PublicEmail, PublicPhone,
   PublicContactForm, PublicSocialLink, ProspectSignal, ProspectTechnology,
   SearchCandidate, ContactPerson) : PKs identiques avant/après, zéro perte
   sur aucune table.

## Correctifs suite à audit indépendant, round 3

Un troisième audit a confirmé le round 2 mais trouvé 7 points
supplémentaires, tous corrigés (23 nouveaux tests, 363 au total) :

1. **Ordre transactionnel du webhook** — `score_prospect()` déplacé en tout
   dernier dans `process_predictneed_event()` (après predictneed_stage/
   CampaignProspect/ConversionEvent/RevenueAttribution) : sinon
   `_hard_exclusion()` ne voyait pas encore `predictneed_stage="paying"`
   lors d'un `subscription_activated`. Testé sur les 4 valeurs exactes
   (stage/excluded/score/grade), pas seulement Engagement.
2. **subscription_cancelled** — nouvel état `"churned"` (Prospect et
   CampaignProspect, réutilisation symétrique de `STAGE_MAP`) : plus
   hard-exclu (candidat légitime à une reconquête), séquence active
   arrêtée, NBA dédiée (`NURTURE` win-back), `RevenueAttribution`
   historique jamais touché.
3. **Échec e-mail** — `_execute_step` ne fait plus jamais avancer l'étape
   aveuglément : `sent`→avance, `failed`→n'avance pas (retry), `suppressed`
   →arrêt séquence + DNC, `blocked`→n'avance pas, raison exploitable.
4. **Site Change strict** — ne compare que des `CrawlRun status="done"`,
   même `start_url`, couverture comparable (`pages_crawled`, ratio ≥ 50%).
   Appel déplacé après la finalisation réussie du `CrawlRun` dans
   `audit_site_task` (sinon `pages_crawled` valait encore 0 au moment de
   l'appel).
5. **Dernier tri legacy supprimé** — `scheduled_refresh_top_prospects()`
   trie sur `predictneed_acquisition_score` ; `Prospect.Meta.ordering`
   corrigé à la racine. Tous les autres usages de `priority_score` audités
   et classés purement legacy (admin, exports, ancien pipeline technique) —
   aucun n'influence plus de décision commerciale.
6. **Migration 0013 réécrite** — 3 cas explicites (anciens défauts exacts →
   nouveaux défauts exacts ; profil personnalisé → proportions relatives
   dans 75% via la méthode du plus grand reste, somme exactement 100 ;
   vide → laissé vide). Vérifié sur Postgres 18 réel avec 3 profils réels
   couvrant les 3 cas.
7. **Migrations depuis 0004** — rechaîne complète sur Postgres 18 réel,
   9 modèles + 3 ICPProfile (cas A/B/C), PKs identiques, `manage.py check`/
   `makemigrations --check` propres, tests de concurrence toujours verts.
