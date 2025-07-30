# Service de Transcription Gladia

Ce service remplace WhisperLive par l'API Gladia pour la transcription audio en temps réel.

## Fonctionnalités

- **Transcription en temps réel** via l'API Gladia
- **Détection automatique de silence** avec arrêt après 1 minute sans parole
- **Support WebSocket** pour la communication en temps réel
- **Intégration Redis** pour la publication des transcriptions
- **Détection d'activité vocale** basée sur l'énergie RMS du signal

## Configuration

### Variables d'environnement requises

- `GLADIA_API_KEY` : Clé API Gladia (obligatoire)
- `REDIS_STREAM_URL` : URL Redis pour les streams (défaut: redis://localhost:6379/0)
- `REDIS_STREAM_KEY` : Clé du stream de transcription (défaut: transcription_segments)
- `REDIS_SPEAKER_EVENTS_RELATIVE_STREAM_KEY` : Clé du stream des événements de locuteur (défaut: speaker_events_relative)

### Variables d'environnement optionnelles

- `SILENCE_THRESHOLD_SECONDS` : Seuil de silence en secondes (défaut: 60)
- `VAD_THRESHOLD` : Seuil de détection d'activité vocale (défaut: 0.5)

## Détection de Silence

Le service implémente une détection de silence automatique qui :

1. **Analyse l'énergie RMS** de chaque frame audio
2. **Détecte les périodes de silence** basées sur un seuil configurable
3. **Arrête automatiquement** la transcription après 1 minute de silence continu
4. **Publie un événement de fin de session** vers Redis

## API WebSocket

### Connexion

```javascript
const ws = new WebSocket('ws://localhost:9090');

// Envoyer la configuration initiale
ws.send(JSON.stringify({
  language: 'fr',
  task: 'transcribe',
  platform: 'google',
  meeting_url: 'https://meet.google.com/...',
  token: 'your_token',
  meeting_id: 'meeting_id'
}));
```

### Messages audio

Envoyez les frames audio en tant que données binaires (Float32Array) :

```javascript
const audioData = new Float32Array([...]); // Données audio
ws.send(audioData.buffer);
```

### Messages de contrôle

```javascript
// Changer la langue
ws.send(JSON.stringify({
  type: 'language',
  language: 'en'
}));

// Déconnexion
ws.send(JSON.stringify({
  type: 'disconnect'
}));

// Ping
ws.send(JSON.stringify({
  type: 'ping'
}));
```

### Réponses

Le service envoie des messages JSON avec la structure suivante :

```javascript
{
  type: 'transcription',
  segments: [
    {
      start: 0.0,
      end: 2.5,
      text: "Bonjour, comment allez-vous ?",
      language: "fr",
      confidence: 0.95
    }
  ],
  timestamp: "2024-01-01T12:00:00Z"
}
```

## Intégration avec Vexa

Le service est configuré pour fonctionner avec l'architecture Vexa existante :

- **Même interface WebSocket** que WhisperLive
- **Même format de données Redis** pour la compatibilité
- **Même configuration Traefik** pour la découverte de service

## Déploiement

### Docker

```bash
docker build -t gladia-transcription .
docker run -p 9090:9090 \
  -e GLADIA_API_KEY=your_key \
  -e REDIS_STREAM_URL=redis://redis:6379/0 \
  gladia-transcription
```

### Docker Compose

Le service est déjà configuré dans `docker-compose.yml` :

```bash
# Définir la clé API Gladia
export GLADIA_API_KEY=your_gladia_api_key

# Démarrer le service
docker-compose up gladia-transcription
```

## Migration depuis WhisperLive

1. **Obtenez une clé API Gladia** depuis [gladia.io](https://gladia.io)
2. **Configurez la variable d'environnement** `GLADIA_API_KEY`
3. **Redémarrez les services** avec `docker-compose up -d`

Le service est conçu pour être un remplacement transparent de WhisperLive, nécessitant aucune modification du code client existant.

## Avantages par rapport à WhisperLive

- **Pas de GPU requis** : Utilise l'API cloud Gladia
- **Détection de silence automatique** : Arrêt intelligent des sessions
- **Moins de maintenance** : Pas de gestion de modèles locaux
- **Scalabilité** : API cloud gérée par Gladia
- **Support multilingue** : Détection automatique de la langue

## Limitations

- **Dépendance Internet** : Nécessite une connexion à l'API Gladia
- **Coût** : Utilisation de l'API payante Gladia
- **Latence** : Légèrement plus élevée qu'une solution locale 
