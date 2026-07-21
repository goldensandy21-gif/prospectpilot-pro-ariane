# ProspectPilot Pro V4

Version consolidée et autonome de l'application de prospection B2B ProspectPilot Pro.

## Base de données prête

La migration `prospects/migrations/0001_initial.py` est fournie et testée. Elle crée notamment :

- Prospect
- CrawlRun
- PageAudit
- SiteAuditSummary
- BacklinkSnapshot
- ContactLog
- Suppression
- SearchConsoleConnection
- SearchConsoleMetric
- Report
- EmailTemplate
- EmailSend
- SearchDecision

Aucune table ne doit être créée manuellement.

## Fonctions

- recherche réelle dans l'API Recherche d'entreprises ;
- filtres par activité, NAF, code postal, département, ville et effectif ;
- pagination ;
- fiche publique avant import ;
- acceptation ou refus d'une entreprise ;
- préqualification site + email + téléphone + formulaire ;
- scan de recherche affichant uniquement les entreprises avec email ou téléphone public ;
- import automatique uniquement des entreprises avec email ou téléphone public ;
- CRM et pipeline ;
- détection du site officiel ;
- crawl multi-pages avec robots.txt ;
- analyse SEO, liens cassés, technologies et CTA ;
- collecte des emails, téléphones, formulaires publics et liens sociaux ;
- conservation de la page source et de la date de collecte des coordonnées ;
- scoring technique, commercial, adéquation et priorité ;
- export CSV et Excel des prospects ;
- rapport PDF ;
- Common Crawl ;
- Search Console OAuth ;
- modèles d'e-mails ProspectPilot Pro ;
- aperçu avant envoi ;
- envoi SMTP OVH ;
- journal d'envoi ;
- désinscription et liste d'opposition ;
- PostgreSQL, Redis, Celery, Celery Beat et Docker.

## Démarrage

```bash
cp .env.example .env
docker compose up --build -d
```

Ouvrez ensuite `http://127.0.0.1:8000`.

Le service `web` lance automatiquement :

```text
python manage.py migrate --noinput
python manage.py initialize_app
gunicorn ...
```

Le compte initial n'est créé que s'il n'existe pas déjà. Son mot de passe n'est pas réinitialisé à chaque redémarrage.

## Données externes

La recherche utilise l'API publique Recherche d'entreprises. Search Console nécessite des identifiants OAuth Google gratuits et ne donne accès qu'aux propriétés autorisées par le compte Google. Common Crawl fournit un index ouvert de captures Web, mais pas un équivalent intégral des bases de backlinks commerciales.

## E-mail OVH

Le fichier `.env.example` est préconfiguré pour :

```env
EMAIL_HOST=ssl0.ovh.net
EMAIL_PORT=465
EMAIL_USE_SSL=True
EMAIL_USE_TLS=False
```

Renseignez l'adresse complète et le mot de passe de la boîte e-mail OVH. Ne transmettez jamais ce mot de passe.


## Enrichissement multi-sources

Le moteur d'enrichissement ajoute une couche modulaire au-dessus des données existantes :

- `EnrichmentSource` décrit chaque source exploitable.
- `ProspectEvidence` conserve chaque donnée collectée avec source, URL, date, statut de vérification et score de confiance.
- `ContactPerson` conserve les contacts professionnels nominatifs publiquement détectés.
- `EnrichmentRun` trace les lancements d'enrichissement.

Sources activées sans abonnement :

- API Recherche d'entreprises : identité administrative, secteur, SIREN/SIRET, adresse, effectif.
- Site officiel : emails publics, téléphones publics, formulaires, profils sociaux publics.
- Common Crawl : indices de présence dans le web ouvert.
- Import CSV/Excel utilisateur : données fournies par l'utilisateur.

Connecteurs préparés, mais nécessitant une clé API et un contrat fournisseur :

- Dropcontact
- Apollo
- Kaspr
- Lemlist

Le système ne scrape pas LinkedIn et ne contourne pas les protections de sites. Les APIs externes doivent être utilisées uniquement selon leurs conditions contractuelles et le RGPD.
