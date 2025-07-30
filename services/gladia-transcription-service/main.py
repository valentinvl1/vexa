import os
import time
import threading
import json
import logging
import asyncio
import websocket
import numpy as np
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import redis
import uuid
import requests
from websockets.sync.server import serve
from websockets.exceptions import ConnectionClosed
import http.server
import socketserver

# Ajout FastAPI pour health check
from fastapi import FastAPI
import uvicorn

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gladia_transcription")

# Configuration Redis
REDIS_URL = os.getenv("REDIS_STREAM_URL", "redis://localhost:6379/0")
REDIS_STREAM_KEY = os.getenv("REDIS_STREAM_KEY", "transcription_segments")
REDIS_SPEAKER_EVENTS_STREAM_KEY = os.getenv("REDIS_SPEAKER_EVENTS_RELATIVE_STREAM_KEY", "speaker_events_relative")

# Configuration Gladia
GLADIA_API_KEY = os.getenv("GLADIA_API_KEY")
GLADIA_API_URL = "https://api.gladia.io/audio/text/audio-transcription/"

# Configuration de détection de silence
SILENCE_THRESHOLD_SECONDS = 60  # 1 minute de silence
VAD_THRESHOLD = 0.5
SAMPLE_RATE = 16000

class VoiceActivityDetector:
    """Détecteur d'activité vocale simple basé sur l'énergie du signal"""
    
    def __init__(self, threshold=0.01, frame_duration=0.5):
        self.threshold = threshold
        self.frame_duration = frame_duration
        self.samples_per_frame = int(SAMPLE_RATE * frame_duration)
        
    def __call__(self, audio_frame):
        """Détermine si l'audio contient de la voix basé sur l'énergie RMS"""
        if len(audio_frame) == 0:
            return False
            
        # Calcul de l'énergie RMS
        rms = np.sqrt(np.mean(audio_frame**2))
        return rms > self.threshold

class TranscriptionCollectorClient:
    """Client pour publier les transcriptions vers Redis"""
    
    def __init__(self, redis_stream_url=None):
        self.redis_url = redis_stream_url or REDIS_URL
        self.redis_client = None
        self.is_connected = False
        self.connection_lock = threading.Lock()
        self.connection_thread = None
        self.stop_requested = False
        
        self.stream_key = REDIS_STREAM_KEY
        self.speaker_events_stream_key = REDIS_SPEAKER_EVENTS_STREAM_KEY
        self.session_starts_published = set()
        
        self.connect()
    
    def connect(self):
        """Connexion à Redis dans un thread séparé avec reconnexion automatique"""
        with self.connection_lock:
            if self.connection_thread and self.connection_thread.is_alive():
                logger.info("Thread de connexion déjà en cours.")
                return
                
            self.stop_requested = False
            self.connection_thread = threading.Thread(
                target=self._connection_worker,
                daemon=True
            )
            self.connection_thread.start()
    
    def _connection_worker(self):
        """Worker de connexion Redis avec reconnexion automatique"""
        while not self.stop_requested:
            try:
                logger.info(f"Tentative de connexion Redis: {self.redis_url}")
                self.redis_client = redis.from_url(self.redis_url)
                self.redis_client.ping()
                self.is_connected = True
                logger.info("Connexion Redis établie.")
                
                # Attendre jusqu'à ce qu'une déconnexion soit demandée
                while not self.stop_requested and self.is_connected:
                    try:
                        time.sleep(1)
                        # Ping périodique pour vérifier la connexion
                        self.redis_client.ping()
                    except redis.exceptions.RedisError:
                        logger.warning("Connexion Redis perdue, tentative de reconnexion...")
                        self.is_connected = False
                        break
                        
            except Exception as e:
                logger.error(f"Erreur de connexion Redis: {e}")
                self.is_connected = False
                
            if not self.stop_requested:
                logger.info("Attente avant reconnexion Redis...")
                time.sleep(5)
    
    def disconnect(self):
        """Déconnexion de Redis"""
        self.stop_requested = True
        if self.redis_client:
            try:
                self.redis_client.close()
            except:
                pass
        self.is_connected = False
        logger.info("Déconnexion Redis.")
    
    def publish_session_start_event(self, token, platform, meeting_id, session_uid):
        """Publie un événement de début de session"""
        if not self.is_connected or session_uid in self.session_starts_published:
            return
            
        timestamp_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "type": "session_start",
            "token": token,
            "platform": platform,
            "meeting_id": meeting_id,
            "uid": session_uid,
            "start_timestamp": timestamp_iso
        }
        
        try:
            self.redis_client.xadd(self.stream_key, payload)
            self.session_starts_published.add(session_uid)
            logger.info(f"Événement session_start publié pour {session_uid}")
        except Exception as e:
            logger.error(f"Erreur lors de la publication session_start: {e}")
    
    def publish_speaker_event(self, event_data: dict):
        """Publie un événement de changement de locuteur"""
        if not self.is_connected:
            return
            
        try:
            self.redis_client.xadd(self.speaker_events_stream_key, event_data)
            logger.debug(f"Événement speaker publié: {event_data}")
        except Exception as e:
            logger.error(f"Erreur lors de la publication speaker_event: {e}")
    
    def publish_session_end_event(self, token, platform, meeting_id, session_uid):
        """Publie un événement de fin de session"""
        if not self.is_connected:
            return
            
        timestamp_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "type": "session_end",
            "token": token,
            "platform": platform,
            "meeting_id": meeting_id,
            "uid": session_uid,
            "end_timestamp": timestamp_iso
        }
        
        try:
            self.redis_client.xadd(self.stream_key, payload)
            logger.info(f"Événement session_end publié pour {session_uid}")
        except Exception as e:
            logger.error(f"Erreur lors de la publication session_end: {e}")
    
    def send_transcription(self, token, platform, meeting_id, segments, session_uid=None):
        """Envoie les segments de transcription vers Redis"""
        if not self.is_connected:
            return
            
        timestamp_iso = datetime.now(timezone.utc).isoformat()
        
        for segment in segments:
            payload = {
                "type": "transcription_segment",
                "token": token,
                "platform": platform,
                "meeting_id": meeting_id,
                "uid": session_uid,
                "segment": json.dumps(segment),
                "timestamp": timestamp_iso
            }
            
            try:
                self.redis_client.xadd(self.stream_key, payload)
                logger.debug(f"Segment de transcription publié: {segment.get('text', '')[:50]}...")
            except Exception as e:
                logger.error(f"Erreur lors de la publication du segment: {e}")

class ClientManager:
    """Gestionnaire des clients WebSocket"""
    
    def __init__(self, max_clients=4, max_connection_time=3600):
        self.clients = {}
        self.max_clients = max_clients
        self.max_connection_time = max_connection_time
        self.lock = threading.Lock()
    
    def add_client(self, websocket, client):
        with self.lock:
            if len(self.clients) >= self.max_clients:
                return False
            self.clients[websocket] = {
                'client': client,
                'start_time': time.time()
            }
            return True
    
    def get_client(self, websocket):
        with self.lock:
            return self.clients.get(websocket, {}).get('client')
    
    def remove_client(self, websocket):
        with self.lock:
            if websocket in self.clients:
                del self.clients[websocket]
    
    def get_wait_time(self):
        with self.lock:
            if len(self.clients) >= self.max_clients:
                return 30
            return 0
    
    def is_server_full(self, websocket, options):
        with self.lock:
            return len(self.clients) >= self.max_clients
    
    def is_client_timeout(self, websocket):
        with self.lock:
            if websocket not in self.clients:
                return True
            client_info = self.clients[websocket]
            return time.time() - client_info['start_time'] > self.max_connection_time

class GladiaTranscriptionClient:
    """Client pour l'API de transcription Gladia"""
    
    def __init__(self, websocket, language="fr", task="transcribe", client_uid=None,
                 platform=None, meeting_url=None, token=None, meeting_id=None,
                 collector_client_ref=None):
        
        self.websocket = websocket
        self.language = language
        self.task = task
        self.client_uid = client_uid or str(uuid.uuid4())
        self.platform = platform
        self.meeting_url = meeting_url
        self.token = token
        self.meeting_id = meeting_id
        self.collector_client = collector_client_ref
        
        # Audio buffer
        self.audio_buffer = []
        self.buffer_duration = 5.0  # 5 secondes de buffer
        self.sample_rate = SAMPLE_RATE
        
        # Détection de silence
        self.vad = VoiceActivityDetector(threshold=VAD_THRESHOLD)
        self.last_speech_time = time.time()
        self.silence_start_time = None
        self.is_silent = False
        
        # État de la session
        self.session_uid = str(uuid.uuid4())
        self.is_active = True
        
        # Publier l'événement de début de session
        if self.collector_client:
            self.collector_client.publish_session_start_event(
                self.token, self.platform, self.meeting_id, self.session_uid
            )
    
    def add_audio_frame(self, audio_frame):
        """Ajoute un frame audio au buffer"""
        if not self.is_active:
            return
            
        self.audio_buffer.extend(audio_frame)
        
        # Vérifier l'activité vocale
        has_speech = self.vad(np.array(audio_frame))
        
        if has_speech:
            self.last_speech_time = time.time()
            if self.is_silent:
                self.is_silent = False
                self.silence_start_time = None
                logger.debug("Détection de parole reprise")
        else:
            if not self.is_silent:
                self.is_silent = True
                self.silence_start_time = time.time()
                logger.debug("Début de silence détecté")
            
            # Vérifier si le silence dépasse le seuil
            if (self.silence_start_time and 
                time.time() - self.silence_start_time > SILENCE_THRESHOLD_SECONDS):
                logger.info(f"Silence de {SILENCE_THRESHOLD_SECONDS}s détecté, arrêt de la transcription")
                self.stop_transcription()
                return
        
        # Traiter le buffer si suffisamment de données
        buffer_duration = len(self.audio_buffer) / self.sample_rate
        if buffer_duration >= self.buffer_duration:
            self.process_audio_buffer()
    
    def process_audio_buffer(self):
        """Traite le buffer audio avec l'API Gladia"""
        if not self.audio_buffer or not self.is_active:
            return
            
        try:
            # Convertir en bytes pour l'API
            audio_data = np.array(self.audio_buffer, dtype=np.float32)
            audio_bytes = audio_data.tobytes()
            
            # Préparer la requête pour Gladia
            headers = {
                'x-gladia-key': GLADIA_API_KEY,
                'Content-Type': 'application/octet-stream'
            }
            
            params = {
                'language_behaviour': 'automatic single language',
                'transcription_hint': '',
                'user_agent': 'vexa-transcription-service'
            }
            
            # Envoyer à l'API Gladia
            response = requests.post(
                GLADIA_API_URL,
                headers=headers,
                params=params,
                data=audio_bytes,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                self.handle_transcription_result(result)
            else:
                logger.error(f"Erreur API Gladia: {response.status_code} - {response.text}")
                
        except Exception as e:
            logger.error(f"Erreur lors du traitement audio: {e}")
        finally:
            # Vider le buffer
            self.audio_buffer = []
    
    def handle_transcription_result(self, result):
        """Traite le résultat de transcription de Gladia"""
        try:
            if 'prediction' in result and result['prediction']:
                segments = []
                
                for prediction in result['prediction']:
                    if 'transcription' in prediction and prediction['transcription'].strip():
                        segment = {
                            'start': prediction.get('time_begin', 0),
                            'end': prediction.get('time_end', 0),
                            'text': prediction['transcription'].strip(),
                            'language': prediction.get('language', self.language),
                            'confidence': prediction.get('confidence', 0.0)
                        }
                        segments.append(segment)
                
                if segments:
                    # Envoyer au client WebSocket
                    self.send_transcription_to_client(segments)
                    
                    # Publier vers Redis
                    if self.collector_client:
                        self.collector_client.send_transcription(
                            self.token, self.platform, self.meeting_id, 
                            segments, self.session_uid
                        )
                        
        except Exception as e:
            logger.error(f"Erreur lors du traitement du résultat: {e}")
    
    def send_transcription_to_client(self, segments):
        """Envoie la transcription au client WebSocket"""
        try:
            message = {
                'type': 'transcription',
                'segments': segments,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }
            self.websocket.send(json.dumps(message))
        except Exception as e:
            logger.error(f"Erreur lors de l'envoi au client: {e}")
    
    def stop_transcription(self):
        """Arrête la transcription"""
        self.is_active = False
        
        # Traiter le buffer restant
        if self.audio_buffer:
            self.process_audio_buffer()
        
        # Publier l'événement de fin de session
        if self.collector_client:
            self.collector_client.publish_session_end_event(
                self.token, self.platform, self.meeting_id, self.session_uid
            )
        
        # Fermer la connexion WebSocket
        try:
            self.websocket.close()
        except:
            pass
        
        logger.info(f"Transcription arrêtée pour {self.session_uid}")
    
    def cleanup(self):
        """Nettoie les ressources"""
        self.stop_transcription()

class GladiaTranscriptionServer:
    """Serveur de transcription utilisant l'API Gladia"""
    
    def __init__(self):
        self.client_manager = ClientManager()
        self.collector_client = TranscriptionCollectorClient()
        self.is_healthy = False
        self.health_server = None
    
    def handle_new_connection(self, websocket):
        """Gère une nouvelle connexion WebSocket"""
        try:
            # Attendre les options de configuration
            options_message = websocket.recv()
            options = json.loads(options_message)
            
            # Créer le client de transcription
            client = GladiaTranscriptionClient(
                websocket=websocket,
                language=options.get('language', 'fr'),
                task=options.get('task', 'transcribe'),
                client_uid=options.get('client_uid'),
                platform=options.get('platform'),
                meeting_url=options.get('meeting_url'),
                token=options.get('token'),
                meeting_id=options.get('meeting_id'),
                collector_client_ref=self.collector_client
            )
            
            # Ajouter au gestionnaire
            if not self.client_manager.add_client(websocket, client):
                websocket.send(json.dumps({'error': 'Serveur plein'}))
                websocket.close()
                return
            
            # Envoyer confirmation de connexion
            websocket.send(json.dumps({'status': 'connected', 'client_uid': client.client_uid}))
            
            # Traiter les messages audio
            self.process_audio_messages(websocket, client)
            
        except Exception as e:
            logger.error(f"Erreur lors de la gestion de la connexion: {e}")
            try:
                websocket.close()
            except:
                pass
    
    def process_audio_messages(self, websocket, client):
        """Traite les messages audio du client"""
        try:
            while client.is_active:
                message = websocket.recv()
                
                if isinstance(message, bytes):
                    # Message audio binaire
                    audio_data = np.frombuffer(message, dtype=np.float32)
                    client.add_audio_frame(audio_data)
                else:
                    # Message de contrôle JSON
                    try:
                        control_data = json.loads(message)
                        self.handle_control_message(websocket, control_data, client)
                    except json.JSONDecodeError:
                        logger.warning(f"Message JSON invalide reçu: {message}")
                        
        except ConnectionClosed:
            logger.info("Connexion WebSocket fermée par le client")
        except Exception as e:
            logger.error(f"Erreur lors du traitement des messages: {e}")
        finally:
            client.cleanup()
            self.client_manager.remove_client(websocket)
    
    def handle_control_message(self, websocket, message, client):
        """Gère les messages de contrôle"""
        msg_type = message.get('type')
        
        if msg_type == 'disconnect':
            client.stop_transcription()
        elif msg_type == 'language':
            client.language = message.get('language', 'fr')
        elif msg_type == 'ping':
            websocket.send(json.dumps({'type': 'pong'}))
        else:
            logger.warning(f"Type de message de contrôle inconnu: {msg_type}")

# FastAPI app pour /health
app = FastAPI()

@app.get("/health")
def health():
    return "OK"

if __name__ == "__main__":
    if not GLADIA_API_KEY:
        logger.error("GLADIA_API_KEY non définie dans les variables d'environnement")
        exit(1)

    # Lancer FastAPI (pour /health) dans un thread séparé
    def run_api():
        uvicorn.run("main:app", host="0.0.0.0", port=9090, log_level="info")
    import threading
    threading.Thread(target=run_api, daemon=True).start()

    # Lancer le serveur WebSocket (sur le même port ou un autre si besoin)
    server = GladiaTranscriptionServer()
    server.run() 
