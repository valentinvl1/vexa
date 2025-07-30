# Déploiement Vexa sur Railway avec Gladia

Ce guide explique comment déployer Vexa sur Railway en utilisant le service de transcription Gladia au lieu de WhisperLive.

## Prérequis

1. **Compte Railway** : Créez un compte sur [railway.app](https://railway.app)
2. **Clé API Gladia** : Obtenez une clé API sur [gladia.io](https://gladia.io)
3. **Repository Git** : Votre code Vexa doit être dans un repository Git

## Configuration Railway

### 1. Variables d'environnement

Configurez les variables d'environnement suivantes dans Railway :

#### Variables obligatoires :
- `GLADIA_API_KEY` : Votre clé API Gladia
- `REDIS_URL` : URL de votre instance Redis (Railway peut fournir Redis)

#### Variables optionnelles :
- `REDIS_STREAM_KEY` : Clé du stream de transcription (défaut: `transcription_segments`)
- `REDIS_SPEAKER_EVENTS_RELATIVE_STREAM_KEY` : Clé du stream des événements de locuteur (défaut: `speaker_events_relative`)
- `SILENCE_THRESHOLD_SECONDS` : Seuil de silence en secondes (défaut: `60`)
- `VAD_THRESHOLD` : Seuil de détection d'activité vocale (défaut: `0.5`)
- `LOG_LEVEL` : Niveau de log (défaut: `INFO`)

### 2. Services Railway

#### Service principal (Gladia Transcription)
- **Nom** : `gladia-transcription`
- **Dockerfile** : `services/gladia-transcription-service/Dockerfile`
- **Port** : `9090` (WebSocket), `9091` (Health check)
- **Variables** : Toutes les variables d'environnement ci-dessus

#### Service Redis (optionnel)
Si Railway ne fournit pas Redis, vous pouvez utiliser un service Redis externe ou configurer Redis sur Railway.

## Déploiement

### Méthode 1 : Via l'interface Railway

1. **Connectez votre repository** :
   - Allez sur Railway Dashboard
   - Cliquez sur "New Project"
   - Sélectionnez "Deploy from GitHub repo"
   - Choisissez votre repository Vexa

2. **Configurez le service** :
   - Railway détectera automatiquement le `railway.json`
   - Configurez les variables d'environnement
   - Déployez le service

3. **Vérifiez le déploiement** :
   - Consultez les logs pour vérifier le démarrage
   - Testez l'endpoint de santé : `https://your-app.railway.app/health`

### Méthode 2 : Via CLI Railway

```bash
# Installer Railway CLI
npm install -g @railway/cli

# Se connecter à Railway
railway login

# Initialiser le projet
railway init

# Configurer les variables d'environnement
railway variables set GLADIA_API_KEY=your_gladia_api_key
railway variables set REDIS_URL=your_redis_url

# Déployer
railway up
```

## Configuration des autres services

### Bot Manager
Le bot manager doit être configuré pour utiliser l'URL du service Gladia :

```bash
railway variables set WHISPER_LIVE_URL=wss://your-gladia-service.railway.app/ws
```

### API Gateway
Configurez l'API Gateway pour pointer vers les services appropriés :

```bash
railway variables set GLADIA_TRANSCRIPTION_URL=https://your-gladia-service.railway.app
```

## Monitoring et logs

### Logs Railway
```bash
# Voir les logs en temps réel
railway logs

# Voir les logs d'un service spécifique
railway logs --service gladia-transcription
```

### Health Check
L'endpoint de santé est disponible sur `/health` :
```bash
curl https://your-app.railway.app/health
```

### Métriques
Railway fournit des métriques automatiques :
- Utilisation CPU/Mémoire
- Requêtes par minute
- Temps de réponse

## Avantages du déploiement Railway

1. **Scalabilité automatique** : Railway ajuste automatiquement les ressources
2. **Gestion des secrets** : Variables d'environnement sécurisées
3. **Monitoring intégré** : Logs et métriques automatiques
4. **Déploiement continu** : Mise à jour automatique depuis Git
5. **HTTPS automatique** : Certificats SSL gratuits

## Coûts

### Railway
- **Plan gratuit** : $5 de crédit/mois
- **Plan Pro** : $20/mois pour plus de ressources

### Gladia
- **Plan gratuit** : 100 minutes/mois
- **Plan payant** : À partir de $0.10/minute

## Dépannage

### Problèmes courants

1. **Service ne démarre pas** :
   ```bash
   railway logs --service gladia-transcription
   ```

2. **Erreur de connexion Redis** :
   - Vérifiez `REDIS_URL`
   - Assurez-vous que Redis est accessible

3. **Erreur API Gladia** :
   - Vérifiez `GLADIA_API_KEY`
   - Consultez les quotas Gladia

4. **Health check échoue** :
   - Vérifiez les logs du service
   - Testez manuellement l'endpoint `/health`

### Commandes utiles

```bash
# Redémarrer le service
railway service restart gladia-transcription

# Voir les variables d'environnement
railway variables

# Ouvrir un shell dans le conteneur
railway shell gladia-transcription

# Voir les métriques
railway metrics
```

## Migration depuis WhisperLive

Si vous migrez depuis WhisperLive :

1. **Arrêtez les services WhisperLive** :
   ```bash
   docker-compose stop whisperlive whisperlive-cpu
   ```

2. **Configurez Gladia** :
   - Obtenez une clé API Gladia
   - Configurez les variables d'environnement

3. **Déployez sur Railway** :
   - Suivez les étapes de déploiement ci-dessus

4. **Testez la migration** :
   - Vérifiez que les transcriptions fonctionnent
   - Testez la détection de silence

## Support

- **Railway** : [docs.railway.app](https://docs.railway.app)
- **Gladia** : [docs.gladia.io](https://docs.gladia.io)
- **Vexa** : Consultez le README principal du projet 
