# Deploiement ProspectPilot Pro avec GitHub et Fly

Ce projet est l'application ProspectPilot Pro. Il doit etre deploye dans sa propre application Fly.

## 1. Ce qui doit aller sur GitHub

Le depot GitHub doit contenir le code de ProspectPilot Pro :

- `Dockerfile`
- `fly.toml`
- `requirements.txt`
- `manage.py`
- `config/`
- `prospects/`
- `templates/`
- `static/`
- `.env.example`
- `.gitignore`
- `.dockerignore`

Le depot GitHub ne doit pas contenir :

- `.env`
- mots de passe
- backups SQL
- fichiers `.backup-*`
- base locale `db.sqlite3`
- dossier `media/`
- dossier `staticfiles/`

Ces exclusions sont deja configurees dans `.gitignore` et `.dockerignore`.

## 2. Nom de l'application Fly

Le fichier `fly.toml` utilise ce nom par defaut :

```text
prospectpilot-pro-ariane
```

Si Fly indique que ce nom est deja pris, choisis un autre nom, par exemple :

```text
prospectpilot-pro-ariane-2026
```

Dans ce cas, il faut aussi remplacer la premiere ligne de `fly.toml` :

```toml
app = "prospectpilot-pro-ariane-2026"
```

## 3. Lancer depuis GitHub dans Fly

Dans Fly :

1. Choisir `Launch app from GitHub`.
2. Selectionner le depot GitHub de ProspectPilot Pro.
3. Creer une nouvelle application Fly.
4. Ne pas selectionner une application Fly existante : creer une nouvelle application pour ProspectPilot Pro.
5. Region conseillee : `cdg` / Paris.
6. Ajouter PostgreSQL.
7. Ajouter Redis ou une base Redis Upstash.

## 4. Secrets Fly obligatoires

Dans Fly, ajouter ces secrets pour l'application ProspectPilot Pro.

Remplace `prospectpilot-pro-ariane.fly.dev` par l'URL Fly obtenue si le nom change.

```env
DJANGO_SECRET_KEY=une-cle-longue-et-aleatoire
DEBUG=False
ALLOWED_HOSTS=prospectpilot-pro-ariane.fly.dev
CSRF_TRUSTED_ORIGINS=https://prospectpilot-pro-ariane.fly.dev
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_HSTS_SECONDS=3600
SECURE_HSTS_INCLUDE_SUBDOMAINS=False
SECURE_HSTS_PRELOAD=False
PUBLIC_BASE_URL=https://prospectpilot-pro-ariane.fly.dev
PRODUCT_URL=https://prospectpilot-pro-ariane.fly.dev
```

Ajouter aussi les secrets e-mail :

```env
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=ssl0.ovh.net
EMAIL_PORT=465
EMAIL_USE_SSL=True
EMAIL_USE_TLS=False
EMAIL_HOST_USER=votre-adresse-email
EMAIL_HOST_PASSWORD=votre-mot-de-passe-ou-cle-smtp
DEFAULT_FROM_EMAIL=votre-adresse-email
EMAIL_SENDER_NAME=Ariane - ProspectPilot Pro
CONTACT_EMAIL=votre-adresse-email
COMPANY_NAME=ProspectPilot Pro
COMPANY_POSTAL_ADDRESS=Lyon, France
EMAIL_BATCH_LIMIT=20
```

PostgreSQL et Redis doivent fournir :

```env
DATABASE_URL=...
REDIS_URL=...
```

Si Fly cree PostgreSQL/Redis automatiquement, ces valeurs peuvent etre ajoutees automatiquement.

## 5. Processus Fly deja prevus

`fly.toml` declare trois processus :

- `web` : Django + migrations + initialisation + Gunicorn.
- `worker` : Celery Worker pour les scans et enrichissements en arriere-plan.
- `beat` : Celery Beat pour les taches planifiees.

Apres deploiement, il faut verifier que les trois processus tournent.

## 6. Verification apres deploiement

Dans Fly ou dans le terminal :

```bash
fly status -a prospectpilot-pro-ariane
fly logs -a prospectpilot-pro-ariane
fly ssh console -a prospectpilot-pro-ariane -C "python manage.py check --deploy"
```

Puis ouvrir :

```text
https://prospectpilot-pro-ariane.fly.dev
```

## 7. Points a verifier avant publication commerciale

- changer le mot de passe admin ;
- tester l'envoi e-mail reel ;
- tester un scan en arriere-plan ;
- verifier les logs du worker Celery ;
- configurer des sauvegardes PostgreSQL ;
- ajouter les pages mentions legales / confidentialite / RGPD.
