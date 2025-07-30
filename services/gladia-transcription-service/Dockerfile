FROM python:3.11-slim

WORKDIR /app

# Installation des dépendances système
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copie des fichiers de dépendances
COPY requirements.txt .

# Installation des dépendances Python
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code source
COPY main.py .

# Exposition des ports
EXPOSE 9090  # WebSocket
EXPOSE 9091  # Health check

# Commande de démarrage
CMD ["python", "main.py"] 
