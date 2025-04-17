# Vexa System Design

This document outlines the architecture of the Vexa application based on its Docker Compose configuration and recent code analysis.

## Overview

Vexa is a multi-service application orchestrated using Docker Compose. It primarily provides real-time transcription capabilities, alongside administrative functions and bot management. Key technologies include Python (FastAPI for backend services), Whisper (for transcription via WhisperLive), Redis (for messaging via Streams and temporary storage via Hashes/Sets), PostgreSQL (for persistent storage), and Traefik (as a reverse proxy/load balancer).

## Services

The system is composed of the following services:

### 1. `api-gateway`

*   **Purpose**: Acts as the primary entry point for external API requests. It routes incoming traffic to the appropriate backend services (`bot-manager`, `transcription-collector`, `admin-api`) and forwards necessary authentication headers.
*   **Technology**: FastAPI (Python).
*   **Build**: `services/api-gateway/Dockerfile`
*   **Ports**: Host `8056` -> Container `8000`
*   **Routing Logic**:
    *   Uses a generic `forward_request` helper.
    *   `POST /bots`, `DELETE /bots/...`: Forwards to `bot-manager`.
    *   `GET /meetings`, `GET /transcripts/...`: Forwards to `transcription-collector`.
    *   `/admin/*` (all methods): Forwards to `admin-api`.
*   **Authentication Handling**:
    *   Expects `X-API-Key` header for non-admin routes.
    *   Forwards `X-API-Key` to `bot-manager` and `transcription-collector`.
    *   Expects `X-Admin-API-Key` header for `/admin/*` routes.
    *   Forwards `X-Admin-API-Key` to `admin-api`.
*   **Dependencies**: `admin-api`, `bot-manager`, `transcription-collector`
*   **Key Config**: Relies on environment variables (`ADMIN_API_URL`, `BOT_MANAGER_URL`, `TRANSCRIPTION_COLLECTOR_URL`) to know the addresses of downstream services.
*   **Network**: `vexa_default`
*   **Other**: Provides OpenAPI (Swagger) documentation for the exposed API endpoints.

### 2. `admin-api`

*   **Purpose**: Provides administrative API endpoints for managing users and their API tokens (`X-API-Key`).
*   **Technology**: FastAPI (Python).
*   **Build**: `services/admin-api/Dockerfile`
*   **Ports**: Host `8057` -> Container `8001`
*   **Authentication**: Requires a static `ADMIN_API_TOKEN` (set via environment variable, likely sourced from `.env` file) sent in the `X-Admin-API-Key` header for all its endpoints (`/admin/*`).
*   **API Endpoints** (prefixed with `/admin`):
    *   `POST /users`: Create a new user.
    *   `GET /users`: List users.
    *   `POST /users/{user_id}/tokens`: Generate a new API token for a specific user.
    *   *(TODOs in code suggest potential future endpoints for getting/deleting users and tokens)*.
*   **Database Interaction**: Interacts directly with PostgreSQL (`shared_models`) to manage `User` and `APIToken` records.
*   **Dependencies**: `redis` (started - *Note: Usage not apparent in `main.py`*), `postgres` (healthy).
*   **Key Config**: Connects to `redis` and `postgres`. Uses `.env` file for configuration (including the `ADMIN_API_TOKEN`).
*   **Network**: `vexa_default`

### 3. `bot-manager`

*   **Purpose**: Provides an API to manage the lifecycle (start, stop) of `vexa-bot` container instances based on user requests. Acts as a controller for the headless browser bots.
*   **Build**: `services/bot-manager/Dockerfile`
*   **API Endpoints**: Exposes `/bots` (POST to start, DELETE to stop by platform/native ID).
*   **Authentication**: Authenticates incoming API requests using user tokens (via `auth.py`, likely interacting with `admin-api` or DB).
*   **Database Interaction**: Uses PostgreSQL (`shared_models`) to:
    *   Track meeting state (`Meeting` table).
    *   Prevent duplicate active bot sessions for the same user/platform/native ID.
    *   Store the `bot_container_id` and status (`requested`, `active`, `stopped`, `error`).
*   **Docker Interaction**: Uses `docker-py` (via `docker_utils.py`) to communicate with the host's Docker daemon (mounted `/var/run/docker.sock`):
    *   Starts `vexa-bot` containers (`BOT_IMAGE=vexa-bot:latest`) upon valid POST requests.
    *   Passes necessary context (internal meeting ID, native meeting ID, platform, constructed meeting URL, user token, bot name) to the bot container, likely via environment variables.
    *   Stops the corresponding bot container upon valid DELETE requests.
*   **Dependencies**: `redis` (started - *Note: Usage unclear from `main.py`, might be `redis_utils.py` or legacy*), `postgres` (healthy), Docker daemon access.
*   **Key Config**:
    *   Uses `BOT_IMAGE=vexa-bot:latest` to identify the bot image.
    *   Connects to `redis` and `postgres`.
    *   Requires access to the host's Docker daemon via `/var/run/docker.sock` (mounted volume) and `DOCKER_HOST` environment variable.
    *   Specifies `DOCKER_NETWORK=vexa_vexa_default` for the managed bot containers.
*   **Network**: `vexa_default`

### 3a. `vexa-bot` (Managed Container Image)

*   **Purpose**: Headless browser automation bot designed to join online meetings (e.g., Google Meet, Zoom - specific platforms depend on implementation). Likely captures audio/video streams for transcription.
*   **Technology**: Node.js/TypeScript application using the Playwright framework.
*   **Execution**: Runs within a container based on `services/vexa-bot/core/Dockerfile`. Uses Xvfb (virtual framebuffer) to run the browser headlessly. Includes PulseAudio and FFmpeg, indicating audio processing capabilities.
*   **Control**: Launched and stopped by the `bot-manager` service. Receives meeting context, user token, and IDs from `bot-manager` upon startup.
*   **Interaction**: Connects to the specified meeting URL using Playwright. Specific actions within the meeting (joining, audio handling) are defined in its TypeScript source code (`services/vexa-bot/core/src`).

### 4. `whisperlive`

*   **Purpose**: Performs real-time audio transcription using the `faster-whisper` backend. Publishes transcription segments to a Redis Stream.
*   **Build**: `services/WhisperLive/Dockerfile.project`
*   **Ports**: Exposes `9090` (service) and `9091` (healthcheck) internally. Accessible externally via `traefik` on host port `9090`.
*   **Dependencies**: `transcription-collector` (started), `redis` (implicitly, for stream publishing)
*   **Deployment**: Configured for 3 replicas, potentially utilizing GPU resources (device ID '3'). *Note on GPU Contention: Assigning the same specific `device_ids: ['3']` to all replicas will likely cause resource contention if '3' refers to a single physical GPU. Standard practice would involve letting the scheduler assign available GPUs (e.g., omitting `device_ids` or using `count: all`) or ensuring distinct GPU assignments if running on a multi-GPU node. This configuration should be reviewed.*
*   **Key Config**:
    *   Uses Redis Streams (`REDIS_STREAM_URL`, `REDIS_STREAM_NAME=transcription_segments`) for output.
    *   Loads a specific `faster-whisper` model from a mounted cache/model directory.
    *   Configurable VAD threshold and language detection segments via environment variables.
*   **Healthcheck**: `http://localhost:9091/health`
*   **Network**: `vexa_default`, `whispernet`
*   **Traefik Integration**: Uses labels for service discovery by `traefik`, routing traffic from `whisperlive.localhost` and handling WebSocket upgrade headers (though Redis Streams are the primary communication method indicated by env vars).

### 5. `traefik`

*   **Purpose**: Modern reverse proxy and load balancer. Handles incoming HTTP requests, service discovery (via Docker labels), and routing to backend services, particularly `whisperlive`.
*   **Image**: `traefik:v2.10`
*   **Ports**: Host `9090` -> Container `80` (service traffic), Host `8085` -> Container `8080` (Traefik dashboard)
*   **Dependencies**: None explicit, but relies on other services running and being labeled correctly for discovery.
*   **Key Config**:
    *   Uses Docker provider for service discovery (`--providers.docker=true`).
    *   Requires explicit labels on services to expose them (`--providers.docker.exposedbydefault=false`).
    *   Listens on port `80` internally (`--entrypoints.web.address=:80`).
    *   Configured via `traefik.toml` (mounted volume).
*   **Network**: `vexa_default`, `whispernet`

### 6. `transcription-collector`

*   **Purpose**: Consumes transcription segments from the Redis Stream published by `whisperlive`. It validates messages, processes segments, stores them temporarily in Redis (Hashes for segments, Sets for active meeting tracking), acknowledges messages on the stream, and uses a background task to persist immutable segments to PostgreSQL. Also provides API endpoints for retrieving meetings and transcripts.
*   **Technology**: FastAPI (Python).
*   **Build**: `services/transcription-collector/Dockerfile`
*   **Ports**: Host `8123` -> Container `8000`
*   **Dependencies**: `redis` (started), `postgres` (healthy)
*   **Key Config**:
    *   Connects to `postgres` for database interactions (user/meeting lookup, persistent storage).
    *   Connects to `redis` to consume from the `REDIS_STREAM_NAME` (`transcription_segments`) stream using `REDIS_CONSUMER_GROUP` (`collector_group`) and `CONSUMER_NAME` (`collector-main`).
    *   Uses Redis Hashes (`meeting:{id}:segments`) and Sets (`active_meetings`).
    *   Configurable parameters for stream reading (`REDIS_STREAM_READ_COUNT`, `REDIS_STREAM_BLOCK_MS`), stale message handling (`PENDING_MSG_TIMEOUT_MS`), background task interval (`BACKGROUND_TASK_INTERVAL`), segment immutability threshold (`IMMUTABILITY_THRESHOLD`), and Redis segment TTL (`REDIS_SEGMENT_TTL`).
    *   *(Note: `REDIS_CLEANUP_THRESHOLD` mentioned in `docker-compose.yml` does not appear to be used in the current `main.py`)*.
*   **Network**: `vexa_default`

### 7. `redis`

*   **Purpose**: In-memory data store used for:
    *   Message Brokering: Via Redis Streams (`transcription_segments`) for `whisperlive` -> `transcription-collector`.
    *   Temporary Segment Storage: Via Redis Hashes (`meeting:{id}:segments`) used by `transcription-collector`.
    *   Active Meeting Tracking: Via Redis Sets (`active_meetings`) used by `transcription-collector`.
    *   Caching/Session Management (Potentially by other services).
*   **Image**: `redis:7.0-alpine`
*   **Persistence**: Uses append-only file (`--appendonly yes`) stored in the `redis-data` volume.
*   **Network**: `vexa_default`

### 8. `postgres`

*   **Purpose**: Relational database backend for persistent storage used by `admin-api`, `bot-manager`, and `transcription-collector`.
*   **Image**: `postgres:15-alpine`
*   **Ports**: Host `5438` -> Container `5432`
*   **Persistence**: Data stored in the `postgres-data` volume.
*   **Healthcheck**: `pg_isready` ensures database readiness.
*   **Network**: `vexa_default`

## Networking

*   **`vexa_default`**: A bridge network providing the primary communication channel for most services.
*   **`whispernet`**: A separate bridge network connecting only `whisperlive` and `traefik`. This is likely intended to isolate the potentially high-volume traffic between the load balancer (`traefik`) and the transcription service (`whisperlive`) from the main network, or possibly to simplify Traefik's service discovery and routing for `whisperlive`.

## Volumes

*   **`redis-data`**: Named volume for persisting Redis data.
*   **`postgres-data`**: Named volume for persisting PostgreSQL data.
*   **`/var/run/docker.sock` (Mount)**: Host Docker socket mounted into `bot-manager` to allow Docker operations.
*   **`./hub` (Mount)**: Host directory mounted into `whisperlive` for Hugging Face model cache.
*   **`./services/WhisperLive/models` (Mount)**: Host directory mounted into `whisperlive` for local models.
*   **`./traefik.toml` (Mount)**: Host configuration file mounted into `traefik`.

## Configuration Files

*   **`.env`**: This file is explicitly loaded by the `admin-api` service. It **must contain the `ADMIN_API_TOKEN`** used for authenticating administrative requests. It may also contain other sensitive configurations or override environment variables defined elsewhere (though only `admin-api` is explicitly configured to load it).
*   **`traefik.toml`**: Configuration file for the `traefik` service, mounted read-only. Defines entrypoints, providers, etc.
*   **`filter_config.py`**: Optional Python file used by `transcription-collector` (specifically `filters.py`) to load custom filtering rules, patterns, and stopwords for the background persistence task.

## Data Flow (Transcription Example)

**WhisperLive Publishing:**

1.  Audio data is sent to the system, likely hitting `traefik` first (listening on host port 9090).
2.  `traefik` load balances and forwards the request to one of the `whisperlive` service replicas (listening internally on port 9090).
3.  The `whisperlive` replica processes the audio, performs transcription using `faster-whisper`, potentially applying VAD.
4.  *(Data validation steps previously noted in `whisperlive` code were removed/simplified)*. `whisperlive` prepares a payload containing segments and metadata (platform, meeting ID, token, etc.).
5.  The entire data dictionary is serialized to a JSON string.
6.  This JSON string is published as the value of a field named `payload` to the `transcription_segments` Redis Stream using the `XADD` command on the `redis` service.

**Transcription Collector Processing:**

1.  The `transcription-collector` service runs two primary background tasks upon startup: `consume_redis_stream` and `process_redis_to_postgres`.
2.  **`consume_redis_stream` Task:**
    *   Connects to the `redis` service and enters a loop, blocking on `XREADGROUP` to read new messages from the `transcription_segments` stream using its consumer group (`collector_group`) and name (`collector-main`).
    *   Upon receiving messages, it iterates through them.
    *   For each message, it calls the `process_stream_message` helper function.
3.  **`process_stream_message` Helper:**
    *   Parses the JSON `payload` from the stream message data.
    *   Validates required fields (`platform`, `meeting_id`, `token`, `segments`).
    *   Authenticates the `token` and looks up the `User` and internal `Meeting.id` via PostgreSQL.
    *   Iterates through the raw `segments` array:
        *   Validates segment structure and time formats.
        *   Formats segment data (start time as key, JSON string of text/end_time/language/updated_at as value).
    *   If valid segments exist:
        *   Executes a Redis **pipeline** (`transaction=True`):
            *   `SADD active_meetings {internal_meeting_id}`: Adds the meeting ID to the set of active meetings.
            *   `EXPIRE meeting:{internal_meeting_id}:segments {TTL}`: Sets/updates the TTL on the hash key (may return `False` if the key doesn't exist yet, which is handled).
            *   `HSET meeting:{internal_meeting_id}:segments {start_time1} {segment_json1} ...`: Stores/overwrites the processed segments in the Redis Hash.
        *   Checks the pipeline results: Only considers it a failure if any command returned `None`. A `False` return from `EXPIRE` is accepted.
    *   Returns `True` if processing was successful (or if it was a non-recoverable data error), `False` if a potentially recoverable error occurred (e.g., Redis connection issue, critical pipeline failure).
4.  **Back in `consume_redis_stream` Task:**
    *   If `process_stream_message` returned `True`, the message ID is added to a list for acknowledgment.
    *   After processing a batch of messages, it calls `XACK` on the `redis` service to acknowledge all successfully processed messages in that batch, removing them from the pending list for the consumer group.
5.  **`claim_stale_messages` Function (Run at startup and potentially periodically):**
    *   Uses `XPENDING` with an `idle` time (`PENDING_MSG_TIMEOUT_MS`) to find messages that were delivered but not acknowledged within the timeout.
    *   Uses `XCLAIM` to take ownership of these stale messages.
    *   Processes claimed messages using the same `process_stream_message` helper.
    *   Acknowledges successfully processed claimed messages using `XACK`.
6.  **`process_redis_to_postgres` Background Task:**
    *   Runs periodically (every `BACKGROUND_TASK_INTERVAL` seconds).
    *   Gets active meeting IDs from the `active_meetings` Redis Set (`SMEMBERS`).
    *   For each meeting ID:
        *   Retrieves all segments from the corresponding Redis Hash (`HGETALL meeting:{id}:segments`).
        *   If the hash is empty, removes the ID from `active_meetings` set (`SREM`).
        *   Iterates through the segments in the hash:
            *   Checks the `updated_at` timestamp within the segment's JSON data.
            *   If `updated_at` is older than `datetime.utcnow() - IMMUTABILITY_THRESHOLD`, the segment is considered immutable.
            *   Immutable segments are passed through a filtering process (`TranscriptionFilter` in `filters.py`):
                *   Filters based on minimum character length, regex patterns (including optional `filter_config.py` patterns), minimum real words (excluding stopwords, potentially from `filter_config.py`), and optional custom functions from `filter_config.py`.
            *   Segments passing filters are marked for deletion from the Redis Hash and added to a batch for PostgreSQL insertion.
    *   If the batch contains segments:
        *   Inserts the batch of `Transcription` objects into the PostgreSQL database within a transaction.
        *   Upon successful commit, deletes the corresponding processed segments from their Redis Hashes using `HDEL`.

**Conclusion:**

The data pipeline from `whisperlive` to `transcription-collector` now correctly uses Redis Streams. `transcription-collector` efficiently consumes these streams, temporarily stores data in Redis Hashes/Sets, acknowledges messages, and uses a background task to filter and persist immutable segments to PostgreSQL. The WebSocket code previously observed in `transcription-collector` appears unused by this core transcription pipeline.

## Future Findings

*(This section can be updated as more details about the system are discovered)* 