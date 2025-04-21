#recreate bd
docker compose exec admin-api python /app/app/scripts/recreate_db.py

#rebuild vexa-bot
make build-bot-image