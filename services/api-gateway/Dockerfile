FROM python:3.10-slim

WORKDIR /app

# Installer la lib partagée
COPY ./libs/shared-models /app/libs/shared-models
RUN pip install --no-cache-dir /app/libs/shared-models

# Copier le code de l'API gateway
COPY ./services/api-gateway/ /app/

# Installer les dépendances
RUN pip install --no-cache-dir -r requirements.txt

# Commande : utilise la variable d'environnement $PORT fournie par Railway
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
