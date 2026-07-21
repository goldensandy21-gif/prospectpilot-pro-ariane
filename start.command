#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Le fichier .env vient d'être créé. Ouvrez-le et renseignez votre adresse OVH avant les envois d'e-mails."
fi
docker compose down --remove-orphans
docker compose up --build -d
echo ""
echo "ProspectPilot Pro V4 est lancé : http://127.0.0.1:8000"
echo "Utilisateur initial : ariane"
echo "Mot de passe initial : ChangeMe123!"
