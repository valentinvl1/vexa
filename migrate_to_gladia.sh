#!/bin/bash

# Script de migration de WhisperLive vers Gladia
# Usage: ./migrate_to_gladia.sh [GLADIA_API_KEY]

set -e

echo "🚀 Migration de WhisperLive vers Gladia..."

# Vérifier si la clé API est fournie
if [ -z "$1" ]; then
    echo "❌ Erreur: Clé API Gladia requise"
    echo "Usage: ./migrate_to_gladia.sh YOUR_GLADIA_API_KEY"
    echo ""
    echo "Vous pouvez obtenir une clé API sur https://gladia.io"
    exit 1
fi

GLADIA_API_KEY=$1

echo "✅ Clé API Gladia fournie"

# Vérifier si Docker est installé
if ! command -v docker &> /dev/null; then
    echo "❌ Erreur: Docker n'est pas installé"
    exit 1
fi

# Vérifier si Docker Compose est installé
if ! command -v docker-compose &> /dev/null; then
    echo "❌ Erreur: Docker Compose n'est pas installé"
    exit 1
fi

echo "✅ Docker et Docker Compose détectés"

# Arrêter les services WhisperLive existants
echo "🛑 Arrêt des services WhisperLive..."
docker-compose stop whisperlive whisperlive-cpu 2>/dev/null || true

# Créer le fichier .env s'il n'existe pas
if [ ! -f .env ]; then
    echo "📝 Création du fichier .env..."
    cp env-example.gladia .env
fi

# Mettre à jour la clé API dans le fichier .env
echo "🔑 Configuration de la clé API Gladia..."
if grep -q "GLADIA_API_KEY=" .env; then
    # Mettre à jour la ligne existante
    sed -i.bak "s/GLADIA_API_KEY=.*/GLADIA_API_KEY=$GLADIA_API_KEY/" .env
else
    # Ajouter la ligne
    echo "GLADIA_API_KEY=$GLADIA_API_KEY" >> .env
fi

echo "✅ Clé API configurée dans .env"

# Construire le nouveau service Gladia
echo "🔨 Construction du service Gladia..."
docker-compose build gladia-transcription

# Démarrer le nouveau service
echo "🚀 Démarrage du service Gladia..."
docker-compose up -d gladia-transcription

# Vérifier que le service démarre correctement
echo "⏳ Attente du démarrage du service..."
sleep 10

# Vérifier l'état du service
if docker-compose ps gladia-transcription | grep -q "Up"; then
    echo "✅ Service Gladia démarré avec succès"
else
    echo "❌ Erreur: Le service Gladia n'a pas démarré correctement"
    echo "Vérifiez les logs avec: docker-compose logs gladia-transcription"
    exit 1
fi

# Vérifier la connectivité Redis
echo "🔍 Vérification de la connectivité Redis..."
if docker-compose exec gladia-transcription python -c "
import redis
import os
try:
    r = redis.from_url(os.getenv('REDIS_STREAM_URL', 'redis://redis:6379/0'))
    r.ping()
    print('✅ Connexion Redis OK')
except Exception as e:
    print(f'❌ Erreur Redis: {e}')
    exit(1)
" 2>/dev/null; then
    echo "✅ Connectivité Redis vérifiée"
else
    echo "❌ Erreur de connectivité Redis"
    exit 1
fi

# Démarrer les autres services
echo "🔄 Démarrage des autres services..."
docker-compose up -d

echo ""
echo "🎉 Migration terminée avec succès!"
echo ""
echo "📋 Résumé des changements:"
echo "  ✅ WhisperLive remplacé par Gladia"
echo "  ✅ Détection de silence automatique (60s)"
echo "  ✅ Interface WebSocket compatible"
echo "  ✅ Intégration Redis maintenue"
echo ""
echo "🔧 Prochaines étapes:"
echo "  1. Testez une transcription: docker-compose logs gladia-transcription"
echo "  2. Vérifiez les logs: docker-compose logs -f gladia-transcription"
echo "  3. Accédez au dashboard Traefik: http://localhost:8085"
echo ""
echo "⚠️  Important:"
echo "  - La clé API Gladia est configurée dans .env"
echo "  - Les coûts Gladia s'appliquent selon votre utilisation"
echo "  - Le service s'arrête automatiquement après 1 minute de silence"
echo ""
echo "🆘 En cas de problème:"
echo "  - Logs: docker-compose logs gladia-transcription"
echo "  - Redémarrage: docker-compose restart gladia-transcription"
echo "  - Rollback: docker-compose up -d whisperlive" 
