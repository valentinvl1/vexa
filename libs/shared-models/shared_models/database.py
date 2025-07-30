import os
import logging
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import create_engine  # For sync engine if needed for migrations later
from sqlalchemy.sql import text

# Import Base from models within the same package
from .models import Base 

logger = logging.getLogger("shared_models.database")

# --- Database Configuration ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    logger.info("Using DATABASE_URL from environment")
    # Convert async URL to sync version for Alembic if needed
    DATABASE_URL_SYNC = DATABASE_URL.replace("+asyncpg", "")
else:
    DB_HOST = os.environ.get("DB_HOST", "postgres")
    DB_PORT = os.environ.get("DB_PORT", "5432")
    DB_NAME = os.environ.get("DB_NAME", "vexa")
    DB_USER = os.environ.get("DB_USER", "postgres")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "postgres")
    DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    DATABASE_URL_SYNC = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# --- SQLAlchemy Async Engine & Session ---
echo_debug = os.environ.get("LOG_LEVEL", "INFO").upper() == "DEBUG"
engine = create_async_engine(
    DATABASE_URL,
    echo=echo_debug,
    pool_size=10,
    max_overflow=20
)
async_session_local = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# --- Sync Engine (For Alembic migrations) ---
sync_engine = create_engine(DATABASE_URL_SYNC)

# --- FastAPI Dependency ---
async def get_db() -> AsyncSession:
    async with async_session_local() as session:
        try:
            yield session
        finally:
            await session.close()

# --- Initialization Function ---
async def init_db():
    logger.info(f"Initializing database tables at {DATABASE_URL}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
        logger.info("Database tables checked/created successfully.")
    except Exception as e:
        logger.error(f"Error initializing database tables: {e}", exc_info=True)
        raise

# --- DANGEROUS: Recreate Function ---
async def recreate_db():
    logger.warning("!!! DANGEROUS OPERATION: Dropping and recreating all tables in database !!!")
    try:
        async with engine.begin() as conn:
            logger.warning("Dropping public schema with CASCADE...")
            await conn.execute(text("DROP SCHEMA public CASCADE;"))
            logger.warning("Public schema dropped.")
            logger.info("Recreating public schema...")
            await conn.execute(text("CREATE SCHEMA public;"))
            logger.info("Public schema recreated.")
            logger.info("Recreating all tables based on models...")
            await conn.run_sync(Base.metadata.create_all)
            logger.info("All tables recreated successfully.")
        logger.warning("!!! DANGEROUS OPERATION COMPLETE !!!")
    except Exception as e:
        logger.error(f"Error recreating database tables: {e}", exc_info=True)
        raise
