#!/usr/bin/env python3
"""
Script de test pour le service de transcription Gladia
Usage: python test_gladia_service.py [URL_WEBSOCKET]
"""

import asyncio
import websockets
import json
import numpy as np
import sys
import time
from datetime import datetime

def generate_test_audio(duration=5.0, sample_rate=16000):
    """Génère un signal audio de test (sinusoïde)"""
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    # Générer un signal sinusoïdal à 440 Hz (note La)
    audio = 0.1 * np.sin(2 * np.pi * 440 * t)
    return audio.astype(np.float32)

async def test_gladia_service(websocket_url="ws://localhost:9090"):
    """Test du service Gladia"""
    print(f"🔗 Connexion au service Gladia: {websocket_url}")
    
    try:
        async with websockets.connect(websocket_url) as websocket:
            print("✅ Connexion WebSocket établie")
            
            # Envoyer la configuration initiale
            config = {
                "language": "fr",
                "task": "transcribe",
                "platform": "test",
                "meeting_url": "https://test.meeting.com",
                "token": "test_token",
                "meeting_id": "test_meeting_123"
            }
            
            print(f"📤 Envoi de la configuration: {config}")
            await websocket.send(json.dumps(config))
            
            # Attendre la confirmation de connexion
            response = await websocket.recv()
            response_data = json.loads(response)
            print(f"📥 Réponse reçue: {response_data}")
            
            if response_data.get('status') != 'connected':
                print("❌ Erreur: Connexion non confirmée")
                return False
            
            print("✅ Configuration acceptée")
            
            # Générer et envoyer de l'audio de test
            print("🎵 Génération d'audio de test...")
            test_audio = generate_test_audio(duration=3.0)
            
            # Envoyer l'audio par chunks
            chunk_size = 16000  # 1 seconde d'audio
            for i in range(0, len(test_audio), chunk_size):
                chunk = test_audio[i:i + chunk_size]
                await websocket.send(chunk.tobytes())
                print(f"📤 Audio chunk {i//chunk_size + 1} envoyé ({len(chunk)} échantillons)")
                await asyncio.sleep(0.1)  # Petite pause entre les chunks
            
            # Attendre les transcriptions
            print("⏳ Attente des transcriptions...")
            timeout = 30  # 30 secondes de timeout
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                try:
                    # Utiliser un timeout pour la réception
                    response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    response_data = json.loads(response)
                    
                    if response_data.get('type') == 'transcription':
                        segments = response_data.get('segments', [])
                        print(f"📝 Transcription reçue ({len(segments)} segments):")
                        for segment in segments:
                            print(f"  - {segment.get('start', 0):.1f}s - {segment.get('end', 0):.1f}s: {segment.get('text', '')}")
                    
                    elif response_data.get('type') == 'pong':
                        print("🏓 Pong reçu")
                    
                    else:
                        print(f"📥 Message reçu: {response_data}")
                        
                except asyncio.TimeoutError:
                    print("⏰ Timeout en attente de réponse...")
                    break
            
            # Envoyer un message de déconnexion
            disconnect_msg = {"type": "disconnect"}
            await websocket.send(json.dumps(disconnect_msg))
            print("👋 Message de déconnexion envoyé")
            
            return True
            
    except websockets.exceptions.ConnectionRefused:
        print(f"❌ Erreur: Impossible de se connecter à {websocket_url}")
        print("   Vérifiez que le service Gladia est démarré")
        return False
        
    except Exception as e:
        print(f"❌ Erreur lors du test: {e}")
        return False

async def test_health_endpoint(base_url="http://localhost:9091"):
    """Test de l'endpoint de santé"""
    import aiohttp
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/health") as response:
                if response.status == 200:
                    print(f"✅ Health check OK: {await response.text()}")
                    return True
                else:
                    print(f"❌ Health check échoué: {response.status} - {await response.text()}")
                    return False
    except Exception as e:
        print(f"❌ Erreur lors du health check: {e}")
        return False

async def main():
    """Fonction principale"""
    print("🧪 Test du service de transcription Gladia")
    print("=" * 50)
    
    # Récupérer l'URL depuis les arguments
    websocket_url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:9090"
    base_url = websocket_url.replace("ws://", "http://").replace("wss://", "https://")
    health_url = base_url.replace(":9090", ":9091")
    
    # Test de l'endpoint de santé
    print("\n🏥 Test de l'endpoint de santé...")
    health_ok = await test_health_endpoint(health_url)
    
    if not health_ok:
        print("⚠️  Health check échoué, mais continuation du test WebSocket...")
    
    # Test du service WebSocket
    print("\n🔌 Test du service WebSocket...")
    websocket_ok = await test_gladia_service(websocket_url)
    
    # Résumé
    print("\n" + "=" * 50)
    print("📊 Résumé des tests:")
    print(f"  Health Check: {'✅ OK' if health_ok else '❌ ÉCHEC'}")
    print(f"  WebSocket: {'✅ OK' if websocket_ok else '❌ ÉCHEC'}")
    
    if health_ok and websocket_ok:
        print("\n🎉 Tous les tests sont passés avec succès!")
        return 0
    else:
        print("\n⚠️  Certains tests ont échoué. Vérifiez la configuration.")
        return 1

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n⏹️  Test interrompu par l'utilisateur")
        sys.exit(1) 
