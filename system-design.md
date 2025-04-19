# Vexa System Design

This document outlines the architecture of the Vexa application based on its Docker Compose configuration and recent code analysis.

## Overview

Vexa is a multi-service application orchestrated using Docker Compose. It primarily provides real-time transcription capabilities, alongside administrative functions and bot management. Key technologies include Python (FastAPI for backend services), Whisper (for transcription via WhisperLive), Redis (for messaging via Streams and Pub/Sub, and temporary storage via Hashes/Sets), PostgreSQL (for persistent storage), and Traefik (as a reverse proxy/load balancer).

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
    *   `PUT /bots/{platform}/{native_meeting_id}/config`: Forwards to `bot-manager` for runtime reconfiguration.
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
*   **Dependencies**: `postgres` (healthy). *(Note: Redis is configured in `docker-compose.yml` but code analysis shows it is currently unused by this service)*.
*   **Key Config**: Connects to `postgres`. Uses `.env` file for configuration (including the `ADMIN_API_TOKEN`).
*   **Network**: `vexa_default`

### 3. `bot-manager`

*   **Purpose**: Provides an API to manage the lifecycle (start, stop, reconfigure) of `vexa-bot` container instances based on user requests. Acts as a controller for the headless browser bots.
*   **Build**: `services/bot-manager/Dockerfile`
*   **API Endpoints**:
    *   `POST /bots`: Start a bot.
    *   `DELETE /bots/{platform}/{native_meeting_id}`: Stop a bot.
    *   `PUT /bots/{platform}/{native_meeting_id}/config`: Update `language`/`task` for an active bot.
*   **Authentication**: Authenticates incoming API requests using user tokens sent in the `X-API-Key` header. It verifies the token against the `APIToken` table and retrieves the associated `User` from the `User` table in PostgreSQL (via `auth.py`).
*   **Database Interaction**: Uses PostgreSQL (`shared_models`) to:
    *   Track meeting state (`Meeting` table: `id`, `user_id`, `platform`, `platform_specific_id`, `status`, `bot_container_id`, `start_time`, `end_time`, etc.).
    *   Track bot sessions (`MeetingSession` table: `meeting_id`, `session_uid`, `session_start_time`). A new record is created by `bot-manager` on initial bot start and by `transcription-collector` upon receiving `session_start` events for subsequent WebSocket connections.
    *   Prevent duplicate active bot sessions for the same user/platform/native ID.
*   **Docker Interaction**: Uses `requests_unixsocket` (via `docker_utils.py`) to communicate with the host's Docker daemon (mounted `/var/run/docker.sock`):
    *   Starts `vexa-bot` containers (`BOT_IMAGE=vexa-bot:latest`) upon valid POST requests.
    *   Generates an initial unique `connectionId`, passes it (and other context like `REDIS_URL`, `WHISPER_LIVE_URL`, meeting details, token, initial language/task) via `BOT_CONFIG` environment variable to the bot container.
    *   Records the initial `connectionId` and start time in the `MeetingSession` table.
    *   Handles `DELETE /bots/...` requests by:
        1.  Finding the original `connectionId` (earliest `session_uid`) for the active meeting.
        2.  Publishing a `{"action": "leave"}` command via Redis Pub/Sub to `bot_commands:{connectionId}`.
        3.  Scheduling a background task (`_delayed_container_stop`) to forcefully stop the container via Docker API after a delay (e.g., 30s) as a fallback.
*   **Redis Interaction**: Uses Redis (`aioredis` client initialized in `main.py`) for:
    *   **Pub/Sub Commands:**
        *   On `PUT /bots/.../config`, finds the most recent *active* `Meeting` record and its *earliest* `MeetingSession` (`connectionId`), then publishes a `reconfigure` command (`{"action": "reconfigure", ...}`) to the Redis channel `bot_commands:{connectionId}`.
        *   On `DELETE /bots/...`, finds the active `Meeting` and its *earliest* `MeetingSession` (`connectionId`), then publishes a `leave` command (`{"action": "leave"}`) to the Redis channel `bot_commands:{connectionId}`.
    *   *(Celery Task Queue & Backend: Possibly used by `app/tasks/monitoring.py`, if active).*
    *   *(Distributed Locking & Mapping: `redis_utils.py` exists but its client init is commented out in `main.py`, functionality inactive).*
*   **Dependencies**: `redis` (started), `postgres` (healthy), Docker daemon access.
*   **Key Config**:
    *   Uses `BOT_IMAGE=vexa-bot:latest` to identify the bot image.
    *   Connects to `postgres`. Connects to `redis` via `aioredis` for Pub/Sub.
    *   Requires access to the host's Docker daemon via `/var/run/docker.sock` (mounted volume) and `DOCKER_HOST` environment variable.
    *   Specifies `DOCKER_NETWORK=vexa_vexa_default` for the managed bot containers.
*   **Network**: `vexa_default`

### 3a. `vexa-bot` (Managed Container Image)

*   **Purpose**: Headless browser automation bot designed to join online meetings (currently Google Meet implemented in `platforms/google.ts`). Captures audio streams for transcription and responds to runtime commands via Redis.
*   **Technology**: Node.js/TypeScript application using Playwright (`playwright-extra`) and `redis`.
*   **Execution**: Runs within a container based on `services/vexa-bot/core/Dockerfile`. Uses Xvfb (virtual framebuffer) to run the browser headlessly. Includes PulseAudio and FFmpeg.
*   **Control**: Launched and stopped by `bot-manager`. Receives meeting context, user token, initial `connectionId`, `REDIS_URL`, and `WHISPER_LIVE_URL` via the `BOT_CONFIG` environment variable.
*   **Interaction**:
    *   Connects to the specified meeting URL using Playwright.
    *   Uses Web Audio API (`AudioContext`, `MediaRecorder`, etc. within `platforms/google.ts`) to capture and mix audio streams.
    *   **Establishes WebSocket** connection(s) to `WHISPER_LIVE_URL` (`ws://whisperlive:9090`).
    *   **WebSocket Session Handling:** For *each* WebSocket connection (initial and subsequent reconnections triggered by `reconfigure` commands), it:
        *   Generates a **new unique UUID**.
        *   Sends this **new UUID** as the `uid` field in the initial JSON configuration message sent over the WebSocket (along with current `language`, `task`, `platform`, `meeting_id`, `token`, etc.).
    *   Sends captured audio chunks via WebSocket to `whisperlive`.
    *   **Redis Command Subscription:**
        *   Connects to Redis using `REDIS_URL`.
        *   Subscribes to the unique Redis Pub/Sub channel `bot_commands:{original_connectionId}` (using the `connectionId` received at startup).
        *   Listens for JSON command messages (`{"action": "reconfigure", ...}`, `{"action": "leave", ...}`).
        *   On `reconfigure`: Updates internal state (`language`/`task`) and triggers a WebSocket reconnection (which uses the new state and generates a new `uid`).
        *   On `leave`: Initiates graceful shutdown (attempts to click leave buttons in meeting, closes browser, exits process).
    *   *(The file `transcript-adapter.js` exists but appears unused).*
*   **Network**: Launched onto `vexa_vexa_default` by `bot-manager`. Needs access to `whisperlive` and `redis` (both typically on `vexa_default`).

### 4. `whisperlive`

*   **Purpose**: Performs real-time audio transcription using the `faster-whisper` backend. Receives audio via WebSocket, publishes transcription segments and session start events to a Redis Stream.
*   **Build**: `services/WhisperLive/Dockerfile.project`
*   **Ports**: Exposes `9090` (service) and `9091` (healthcheck) internally. Accessible externally via `traefik` on host port `9090`.
*   **Dependencies**: `transcription-collector` (started), `redis` (implicitly).
*   **Deployment**: Configured for 1 replica in `docker-compose.yml` (`replicas: 1`).
*   **Key Config**:
    *   Receives audio input via WebSocket on port 9090.
    *   **Accepts initial JSON configuration message** upon connection containing `language`, `task`, `uid`, `token`, `platform`, `meeting_id`, etc. (The `uid` received identifies a specific WebSocket connection instance).
    *   Uses Redis Streams (`REDIS_STREAM_URL`, `REDIS_STREAM_NAME=transcription_segments`) for outputting:
        *   `session_start` events: Published upon receiving a new WebSocket connection, containing the connection's `uid` and other metadata.
        *   `transcription` segments: Published as transcriptions are generated, associated with the connection's `uid`.
    *   Loads a specific `faster-whisper` model from a mounted cache/model directory.
    *   Configurable VAD threshold and language detection segments.
    *   *(Major Issue regarding WebSocket input vs Stream output seems resolved or misunderstood - WebSocket input is used, Stream output is used)*.
*   **Healthcheck**: `http://localhost:9091/health`
*   **Network**: `vexa_default`, `whispernet`. *(Note: `whispernet` appears redundant)*.
*   **Traefik Integration**: Uses labels for service discovery by `traefik`, routing traffic from `whisperlive.localhost`.

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
*   **Network**: `vexa_default`, `whispernet`. *(Note: `whispernet` appears redundant)*.

### 6. `transcription-collector`

*   **Purpose**: Consumes events (`session_start`, `transcription`) from the `transcription_segments` Redis Stream. Processes transcription segments, stores them temporarily in Redis Hashes, persists them to PostgreSQL, and handles session records. Provides API endpoints for retrieving meetings and transcripts.
*   **Technology**: FastAPI (Python).
*   **Build**: `services/transcription-collector/Dockerfile`
*   **Ports**: Host `8123` -> Container `8000`
*   **Dependencies**: `redis` (started), `postgres` (healthy)
*   **Key Config**:
    *   Connects to `postgres` for database interactions.
    *   Connects to `redis` to consume from the `REDIS_STREAM_NAME` stream using `REDIS_CONSUMER_GROUP`.
    *   **Stream Processing:**
        *   Processes `session_start` events: Looks up the corresponding `Meeting`. Creates a new `MeetingSession` record in PostgreSQL using the `uid` and `start_timestamp` from the event if one doesn't already exist for that specific `meeting_id` and `uid`. Updates the timestamp if it does exist.
        *   Processes `transcription` events: Looks up user/meeting, stores segments in Redis Hash (`meeting:{id}:segments`), adds meeting ID to `active_meetings` Set.
    *   Background task (`process_redis_to_postgres`) filters and moves immutable segments from Redis Hashes to the `Transcription` table in PostgreSQL.
    *   Configurable parameters for stream reading, stale message handling, background task interval, segment immutability, and Redis TTL.
    *   *(Note: `REDIS_CLEANUP_THRESHOLD` defined in `docker-compose.yml` is confirmed to be unused in the code)*.
*   **Network**: `vexa_default`

### 7. `redis`

*   **Purpose**: In-memory data store used for:
    *   **Message Brokering (Streams):** `transcription_segments` stream (`whisperlive` -> `transcription-collector`) carrying `session_start` and `transcription` events.
    *   **Message Brokering (Pub/Sub):** `bot_commands:{connectionId}` channels (`bot-manager` -> `vexa-bot`) carrying `reconfigure` and `leave` commands.
    *   Temporary Segment Storage: Redis Hashes (`meeting:{id}:segments`) used by `transcription-collector`.
    *   Active Meeting Tracking: Redis Sets (`active_meetings`) used by `transcription-collector`.
    *   *(Caching/Session Management: Potentially by other services).*
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
*   **`vexa_vexa_default`**: A network seemingly created implicitly by Docker Compose, onto which `bot-manager` launches `vexa-bot` containers. Connectivity exists between this network and `vexa_default` allowing bots to reach `whisperlive` and `redis`.
*   **`whispernet`**: A separate bridge network connecting only `whisperlive` and `traefik`. **This network appears redundant** as communication can occur over `vexa_default`. Simplifying by removing it is recommended.

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
*   **`filter_config.py`**: Optional Python file confirmed to exist in `services/transcription-collector/`. Used by `filters.py` (via dynamic import) to load custom filtering rules (regex patterns, stopwords, length/word counts, custom functions). If absent, defaults are used.

## Data Schemas (Pydantic)

Based on imports in `transcription-collector` (likely defined in `shared_models/schemas.py`):

*   **`Platform` (Enum)**: Defines the supported meeting platforms (e.g., `google_meet`, `zoom`). Used in API paths (`/transcripts/{platform}/...`) and internal meeting records.
*   **`TranscriptionSegment`**: Represents a single segment of a transcription. Key fields likely include:
    *   `start_time` (float)
    *   `end_time` (float)
    *   `text` (str)
    *   `language` (Optional[str])
    Used as the primary unit of transcription data within responses and internal processing.
*   **`MeetingResponse`**: Represents the metadata for a meeting record, likely mirroring the `Meeting` database model. Used within `MeetingListResponse` and embedded in `TranscriptionResponse`.
*   **`MeetingListResponse`**: Response model for the `/meetings` endpoint. Contains a list of `MeetingResponse` objects.
*   **`TranscriptionResponse`**: The main response model for the `/transcripts/...` endpoint. It likely inherits fields from `MeetingResponse` and adds a `segments` field containing a list of `TranscriptionSegment` objects.
*   **`HealthResponse`**: Response model for the `/health` endpoint. Contains status fields for the service itself, Redis, and the database.
*   **`ErrorResponse`**: (Imported but potentially unused or used implicitly by FastAPI) Standard structure for returning error details.
*   **`WhisperLiveData`**: (Imported but not directly used for stream parsing) Represents the expected structure of the *entire payload* coming from WhisperLive *before* it's processed segment by segment by the collector. Might contain fields like `uid`, `platform`, `meeting_id`, `token`, and a list of raw segment dictionaries.

## Data Flow (Transcription & Reconfiguration Example)

**Initial Bot Start & Transcription:**

1.  `bot-manager` receives `POST /bots`, generates unique `connectionId` (e.g., `conn_A`), starts `vexa-bot` container passing `conn_A`, `REDIS_URL` etc. Records `conn_A` in `MeetingSession` table (Session A).
2.  `vexa-bot` starts, connects to Redis, subscribes to `bot_commands:conn_A`.
3.  `vexa-bot` joins meeting, establishes *first* WebSocket connection to `whisperlive`.
4.  `vexa-bot` generates *new UUID* (e.g., `uid_1`), sends initial WebSocket config message with `uid: uid_1`, language, task, etc.
5.  `whisperlive` receives connection, publishes `session_start` event with `uid: uid_1` to Redis Stream.
6.  `transcription-collector` consumes `session_start` event, finds no session with `uid: uid_1`, creates new `MeetingSession` record (Session B) with `uid: uid_1`.
7.  `vexa-bot` sends audio via WebSocket. `whisperlive` processes it, publishes `transcription` events with `uid: uid_1` to Redis Stream.
8.  `transcription-collector` consumes `transcription` events, validates token/meeting using `uid_1` metadata (implicitly via lookup), stores segments in Redis Hash associated with the meeting.

**Runtime Reconfiguration:**

1.  User sends `PUT /bots/.../config` with `{"language": "ru"}`.
2.  `api-gateway` forwards to `bot-manager`.
3.  `bot-manager` finds the active `Meeting` record, then finds the *earliest* `MeetingSession` record associated with it, retrieving `session_uid = conn_A`.
4.  `bot-manager` publishes `{"action": "reconfigure", "language": "ru", ...}` to Redis Pub/Sub channel `bot_commands:conn_A`.
5.  `vexa-bot` (listening on `bot_commands:conn_A`) receives the command via its Redis client.
6.  `vexa-bot` handler updates its internal language state, calls browser function `triggerWebSocketReconfigure`.
7.  Browser-side code closes the current WebSocket connection (`uid_1`).
8.  `onclose` handler triggers reconnection (`setupWebSocket`).
9.  `setupWebSocket` generates a *new UUID* (e.g., `uid_2`), sends initial WebSocket config message with `uid: uid_2` and `language: "ru"`.
10. `whisperlive` receives new connection, publishes `session_start` event with `uid: uid_2`.
11. `transcription-collector` consumes event, finds no session with `uid: uid_2`, creates new `MeetingSession` record (Session C) with `uid: uid_2`.
12. Subsequent audio is processed by `whisperlive` using `language: "ru"`, and transcriptions are published with `uid: uid_2`.

**Graceful Stop:**

1.  User sends `DELETE /bots/{platform}/{native_meeting_id}`.
2.  `api-gateway` forwards to `bot-manager`.
3.  `bot-manager` finds the active `Meeting` record and its *earliest* `MeetingSession` (`session_uid = conn_A`).
4.  `bot-manager` publishes `{"action": "leave"}` to Redis Pub/Sub channel `bot_commands:conn_A`.
5.  `bot-manager` schedules a delayed background task to stop the container (`container_id` from `Meeting` record) after 30 seconds.
6.  `bot-manager` updates `Meeting` status to `stopping` and returns `202 Accepted`.
7.  `vexa-bot` (listening on `bot_commands:conn_A`) receives the command.
8.  `vexa-bot` handler calls `performGracefulLeave()`:
    *   Attempts to click leave button(s) via Playwright (`leaveGoogleMeet`).
    *   Closes Redis connection.
    *   Closes the browser instance.
    *   If leave attempt was successful, exits the Node.js process (`process.exit(0)`).
    *   If leave attempt failed, exits with error code (`process.exit(1)`).
9.  **(If Bot Exits):** Container stops because the main process exited.
10. **(If Bot Doesn't Exit):** After 30 seconds, `bot-manager`'s delayed task executes, forcefully stopping the container via Docker API.

**Conclusion:**

The data pipeline involves `vexa-bot` capturing audio and sending it via WebSocket (each connection identified by a unique UUID) to `whisperlive`. `whisperlive` transcribes and publishes results (`session_start`, `transcription`) tagged with the connection's UUID to a Redis Stream. `transcription-collector` consumes this stream, creating distinct session records and storing transcriptions. Separately, `bot-manager` uses Redis Pub/Sub (targeting the bot's original `connectionId`) to send control commands (`reconfigure`, `leave`). The `leave` command triggers a graceful shutdown attempt in the bot, with a delayed container stop in `bot-manager` acting as a fallback.

**Key Issues Identified:**

*   **`bot-manager` Redis Client (Partial)**: The `aioredis` client for Pub/Sub is initialized and working. However, the initialization for the `redis_utils.py` client (locking/mapping) appears to still be commented out, making that specific functionality inactive.
*   **Unused Config**: `admin-api` Redis config and `transcription-collector` `REDIS_CLEANUP_THRESHOLD` are unused.
*   **Redundant Networking**: `whispernet` is likely unnecessary.
*   **Outdated Bot Code**: `transcript-adapter.js` in `vexa-bot` seems unused.
*   **WhisperLive Replicas**: Using >1 replica with a single specified GPU will likely lead to resource contention or errors. (Currently set to 1).

## Future Findings

*(This section can be updated as more details about the system are discovered)* 





## 
docker build -t vexa-bot:latest -f services/vexa-bot/core/Dockerfile services/vexa-bot/core