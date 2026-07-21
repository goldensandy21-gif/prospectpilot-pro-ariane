# Installation sur Mac — aucune table à créer manuellement

## 1. Décompresser le ZIP
Placez le dossier `prospectpilot-pro-v4` sur le Bureau puis ouvrez-le dans VS Code.

## 2. Créer le fichier de configuration
Dans le terminal du dossier :

```bash
cp .env.example .env
```

Ouvrez `.env` et remplacez uniquement les informations personnelles : adresse OVH, mot de passe de la boîte e-mail et URL produit si nécessaire.

## 3. Lancer toute l'application

```bash
docker compose up --build -d
```

Cette seule commande :
- démarre PostgreSQL ;
- démarre Redis ;
- applique toutes les migrations déjà fournies ;
- crée toutes les tables ;
- crée le compte initial si nécessaire ;
- crée les modèles d'e-mail ProspectPilot Pro ;
- démarre Django, Celery Worker et Celery Beat.

Vous ne devez pas lancer `makemigrations` et vous ne devez créer aucune table à la main.

## 4. Ouvrir l'application

`http://127.0.0.1:8000`

Compte initial :
- utilisateur : `ariane`
- mot de passe : `ChangeMe123!`

Modifiez ensuite ce mot de passe depuis l'administration Django.

## Commandes utiles

État des services :
```bash
docker compose ps
```

Journaux de Django :
```bash
docker compose logs web --tail=100
```

Journaux des tâches :
```bash
docker compose logs worker --tail=100
```

Arrêter :
```bash
docker compose down
```

Redémarrer :
```bash
docker compose up -d
```
