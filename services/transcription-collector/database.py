import os
import logging
import sqlalchemy
from databases import Database

# Configure logging
logger = logging.getLogger("transcription_collector.database")

# Récupération de l'URL de la base de données depuis les variables d'environnement
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.error("DATABASE_URL environment variable not set")
    raise RuntimeError("DATABASE_URL must be defined")

# Instance de la base de données
# Conversion vers format asyncpg si ce n'est pas déjà le cas
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

database = Database(DATABASE_URL)

# SQLAlchemy metadata object (à utiliser si nécessaire pour des migrations ou introspection)
metadata = sqlalchemy.MetaData()

# Nom de la table des transcriptions (utilisé dans main.py pour les requêtes)
TRANSCRIPTIONS_TABLE_NAME = "transcriptions"

async def connect_db() -> bool:
    """
    Connexion à la base PostgreSQL.
    Retourne True si la connexion est établie, sinon False.
    """
    logger.info(f"Connecting to PostgreSQL with URL {DATABASE_URL}")
    try:
        await database.connect()
        logger.info("Database connection established")
        return True
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return False

async def disconnect_db():
    """
    Déconnexion de la base PostgreSQL si connectée.
    """
    if database.is_connected:
        await database.disconnect()
        logger.info("Database connection closed")
