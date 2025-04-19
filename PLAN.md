# Vexa Billing, Rate Limiting, and Analytics Implementation Plan

This document outlines a detailed plan for integrating billing, rate limiting, and analytics features into the Vexa system, building upon the existing architecture described in `system-design.md` and `docker-compose.yml`.

**Assumptions:**

*   Access to modify codebases: `api-gateway`, `admin-api`, `bot-manager`, `transcription-collector`, `vexa-bot`, `whisperlive`.
*   Existence of a `shared_models` package/directory for common PostgreSQL models/schemas.
*   Introduction of a new `billing-service`.
*   Primary billing metric: **Transcription minutes consumed, differentiated by model type**.
*   Rate limits apply to: **API request frequency** and **concurrent bot sessions**.

**Phase 1: Foundations - Database Models & Billing Service Shell**

1.  **Define Database Schemas (`shared_models/models.py` or similar):**
    *   **`Plan` Table:**
        *   `id` (PK, UUID/Serial)
        *   `name` (String, unique, e.g., "Free", "Pro", "Enterprise")
        *   `description` (Text, optional)
        *   `api_requests_per_minute` (Integer, nullable) - Rate limit for API gateway
        *   `max_concurrent_bots` (Integer, nullable) - Rate limit for bot manager
        *   `monthly_cost` (Numeric/Decimal, optional)
        *   `is_active` (Boolean, default: True)
        *   `created_at`, `updated_at` (Timestamps)
    *   **`PlanModelLimit` Table:** (Billing limits per model)
        *   `id` (PK)
        *   `plan_id` (FK -> `Plan.id`)
        *   `model_identifier` (String, e.g., "faster-whisper-medium", "whisper-large-v3") - Must match identifier used by `whisperlive`.
        *   `included_minutes` (Integer, nullable) - Billing quota
        *   `price_per_extra_minute` (Numeric/Decimal, nullable) - Overage cost
        *   `created_at`, `updated_at`
    *   **`UserPlan` Table:** (Assigns plans to users)
        *   `id` (PK)
        *   `user_id` (FK -> `User.id`) - Assumes `User` table in `admin-api`/`shared_models`.
        *   `plan_id` (FK -> `Plan.id`)
        *   `billing_cycle_anchor` (Timestamp) - Day/time billing resets each cycle.
        *   `start_date` (Timestamp)
        *   `end_date` (Timestamp, nullable)
        *   `status` (String, e.g., "active", "cancelled", "trial")
        *   `created_at`, `updated_at`
    *   **`UsageRecord` Table:** (Aggregated usage per billing cycle)
        *   `id` (PK)
        *   `user_plan_id` (FK -> `UserPlan.id`)
        *   `model_identifier` (String)
        *   `billing_period_start` (Timestamp)
        *   `billing_period_end` (Timestamp)
        *   `consumed_minutes` (Numeric/Decimal, default: 0) - Use decimal for precision.
        *   `last_updated` (Timestamp)

2.  **Update `shared_models/schemas.py`:**
    *   Create Pydantic schemas corresponding to the new DB tables for API validation and responses.

3.  **Create `billing-service`:**
    *   **Project Setup:** `services/billing-service` directory with standard FastAPI structure (`main.py`, `crud.py`, `database.py`, `models.py` [imports `shared_models`], `schemas.py` [imports/extends `shared_models`]).
    *   **Dockerfile:** Create `services/billing-service/Dockerfile`.
    *   **`docker-compose.yml`:**
        *   Add `billing-service` definition.
        *   Set build context/Dockerfile.
        *   Define dependencies: `postgres` (condition: service_healthy), `redis` (condition: service_started).
        *   Map environment variables (DB connection string, Redis URL).
        *   Assign to `vexa_default` network.
        *   Expose internal port (e.g., 8002).
        *   Implement a basic health check endpoint and configure it in `docker-compose`.

**Phase 2: Metric Collection - Tracking Model & Duration**

1.  **Allow Model Selection (`api-gateway`, `bot-manager`, `vexa-bot`):**
    *   **`api-gateway` (`services/api-gateway/app/main.py` - Forwarding Logic):**
        *   Modify `POST /bots` endpoint: Expect optional `model_type` (String) in the request body.
        *   Pass `model_type` along in the forwarded request payload to `bot-manager`.
    *   **`bot-manager` (`services/bot-manager/app/main.py` - `POST /bots` Handler):**
        *   Accept `model_type` from the request body.
        *   *Enhancement:* Validate `model_type` against `PlanModelLimit` entries for the user's plan (requires call to `billing-service` or DB).
        *   Add `model_type` to the `BOT_CONFIG` JSON string passed as an environment variable to the `vexa-bot` container.
    *   **`vexa-bot` (`services/vexa-bot/core/src/config.ts`, `main.ts`, `google.ts`):**
        *   Parse `BOT_CONFIG` JSON to extract `model_type`.
        *   Include the requested `model_type` in the initial JSON configuration message sent over the WebSocket to `whisperlive`.

2.  **Report Model Used & Emit Usage (`whisperlive`, `transcription-collector`):**
    *   **`whisperlive` (`services/WhisperLive/` - WebSocket Handler):**
        *   Receive `model_type` from the initial client configuration message.
        *   Determine the `actual_model_identifier` being used for transcription (e.g., could be a default if requested model is invalid/unavailable).
        *   **Crucial:** Include this `actual_model_identifier` in **both** the `session_start` event and **every** `transcription` event published to the `transcription_segments` Redis Stream. Add a new field like `"model_identifier": "..."`.
    *   **`transcription-collector` (`services/transcription-collector/app/` - Stream Consumer):**
        *   **Caching Model Info:** When processing `session_start` events, cache the `actual_model_identifier` associated with the `uid` (e.g., in a Redis Hash `session_model:{uid}` with a TTL).
        *   **Processing Transcriptions:** When processing `transcription` events:
            *   Retrieve `actual_model_identifier` for the event's `uid` from the cache.
            *   Calculate the duration of the *new* segment (`end_time - start_time`). Ensure idempotency if streams deliver duplicates.
            *   Look up `user_id` and `meeting_id` (likely using `uid`).
            *   Publish a new event type, `usage_update`, to a **new Redis Stream** (e.g., `name: usage_events`).
            *   `usage_update` event payload (JSON): `{ "user_id": ..., "meeting_id": ..., "model_identifier": ..., "duration_seconds_added": ..., "timestamp": ... }`.

**Phase 3: Enforcement - Limits & Quotas**

1.  **API Rate Limiting (`api-gateway`):**
    *   **Dependency:** Add `slowapi` to `services/api-gateway/requirements.txt`.
    *   **Implementation (`services/api-gateway/app/main.py`):**
        *   Initialize `slowapi.Limiter` with `RedisStorage` using `REDIS_URL`.
        *   Create a reusable dependency function (`get_current_user_and_plan_limits`) that:
            *   Extracts `X-API-Key` header.
            *   Authenticates key -> `user_id` (call `admin-api` or direct DB, use caching).
            *   Fetches user's active `UserPlan` -> `plan_id` (call `billing-service` or direct DB, use caching).
            *   Fetches `Plan` details for `plan_id` -> `api_requests_per_minute` limit (use caching).
            *   Returns `(user_id, api_limit_string)` (e.g., `(123, "100/minute")`).
        *   Apply the limiter using FastAPI middleware or route decorators (`@limiter.limit(get_current_user_and_plan_limits)`), dynamically setting the limit per user based on their plan. Key the limit by `user_id`.

2.  **Concurrent Bot Limit (`bot-manager`):**
    *   **Implementation (`services/bot-manager/app/main.py` - `POST /bots` Handler):**
        *   After authenticating `X-API-Key` -> `user_id`:
            *   Fetch the user's active plan and `max_concurrent_bots` limit (call `billing-service` or DB, use caching).
            *   Perform a DB query: `SELECT COUNT(*) FROM meetings WHERE user_id = :user_id AND status = 'active'`.
            *   If `count >= max_concurrent_bots`, return HTTP `429 Too Many Requests` or `403 Forbidden`.

3.  **Transcription Quota Check (`bot-manager`, `billing-service`):**
    *   **`billing-service` API Endpoint:**
        *   Create `GET /internal/usage/check_quota` (or similar internal path).
        *   Query Parameters: `user_id`, `model_identifier`.
        *   Logic:
            *   Find active `UserPlan` for `user_id`. If none, deny.
            *   Determine current billing cycle start/end based on `UserPlan.billing_cycle_anchor`.
            *   Fetch `PlanModelLimit.included_minutes` for the plan and `model_identifier`. If no limit defined, allow.
            *   Fetch `UsageRecord.consumed_minutes` for the user, model, and current cycle.
            *   Calculate `remaining_minutes = included_minutes - consumed_minutes`.
            *   Return JSON: `{"quota_available": (remaining_minutes > 0), "remaining_minutes": remaining_minutes}`.
    *   **`bot-manager` (`services/bot-manager/app/main.py` - `POST /bots` Handler):**
        *   **Before** starting the Docker container:
            *   Make an HTTP request to the `billing-service`'s `/internal/usage/check_quota` endpoint with `user_id` and requested `model_type`.
            *   If the response indicates `quota_available` is `False`, return HTTP `402 Payment Required` or `403 Forbidden`.

**Phase 4: Aggregation & Presentation**

1.  **Usage Aggregation (`billing-service`):**
    *   **Consumer:** Implement a background task (e.g., using `arq`, `Celery`, or simple `asyncio` loop) or a dedicated stream consumer process within `billing-service` listening to the `usage_events` Redis Stream.
    *   **Logic:** For each `usage_update` event:
        *   Find the user's active `UserPlan`.
        *   Determine the correct billing period based on the event's timestamp and `UserPlan.billing_cycle_anchor`.
        *   Find or create the `UsageRecord` for the `user_plan_id`, `model_identifier`, and `billing_period_start`/`end`.
        *   Atomically update `UsageRecord.consumed_minutes` by adding `duration_seconds_added / 60.0`. Use database atomic operations (e.g., `UPDATE usage_records SET consumed_minutes = consumed_minutes + :added WHERE id = :id`) to prevent race conditions. Update `last_updated`.

2.  **Plan/Subscription Management (`admin-api`):**
    *   **Endpoints (`services/admin-api/app/routers/admin_billing.py` or similar):**
        *   Add CRUD endpoints protected by `X-Admin-API-Key` authentication:
            *   `/admin/plans` (POST, GET)
            *   `/admin/plans/{plan_id}` (GET, PUT, DELETE) - Handle nested `PlanModelLimit` updates.
            *   `/admin/users/{user_id}/plan` (POST, GET, PUT, DELETE) - Manage `UserPlan` assignments.
    *   **CRUD Functions:** Implement corresponding database operations using SQLAlchemy/shared models.

3.  **Billing/Usage View (Optional - Future):**
    *   **`billing-service` API:** Add user-facing endpoints like `GET /billing/me/plan`, `GET /billing/me/usage`.
    *   **`api-gateway`:** Expose these endpoints, forwarding requests authenticated with `X-API-Key` to `billing-service`.

**Phase 5: Analytics**

1.  **Strategy Selection:** Choose between:
    *   **Log-based (Simpler Start):** Structured JSON logging from services -> Log Collector (Filebeat/Fluentd) -> Storage/Visualizer (Elasticsearch/Loki + Kibana/Grafana).
    *   **Event-based (More Robust):** Services publish detailed events to dedicated stream (Redis Streams/Kafka) -> Analytics Processor Service -> Analytics DB (TimescaleDB/ClickHouse) -> Visualizer (Grafana).
    *   *Decision:* Assume Log-based for initial implementation.

2.  **Structured Logging:**
    *   **Libraries:** Use `python-json-logger` or similar in all Python services (`api-gateway`, `bot-manager`, `transcription-collector`, `billing-service`).
    *   **Key Events to Log (JSON format):**
        *   `api-gateway`: API request start/end (user_id, endpoint, method, status_code, latency). Rate limit hit.
        *   `bot-manager`: Bot start request (user_id, model_type), bot start success/failure (reason), bot stop request, concurrent limit hit, quota limit hit.
        *   `transcription-collector`: Usage update published (user_id, model, duration). Errors processing stream.
        *   `billing-service`: Usage record updated, quota check performed, plan created/updated. Errors consuming usage stream.

3.  **Log Collection (`docker-compose.yml`):**
    *   Add `Filebeat` or `Fluentd` service.
    *   Configure volume mounts to access Docker container logs (e.g., `/var/lib/docker/containers`).
    *   Configure processors to parse JSON logs and add necessary metadata.
    *   Configure output to Elasticsearch or Loki.

4.  **Storage & Visualization (`docker-compose.yml`):**
    *   Add `Elasticsearch` & `Kibana` or `Loki` & `Grafana` services.
    *   Build initial dashboards in Kibana/Grafana for:
        *   API Usage (requests/min, errors, latency by user/endpoint).
        *   Transcription Volume (minutes per model per user/plan).
        *   Bot Activity (active bots, starts/stops).
        *   Billing System Health (usage processing rate, errors).

**Phase 6: Testing & Deployment**

1.  **Unit Tests:** Cover new logic: rate limiting, quota checks, usage calculation, stream processing, DB CRUD operations, API endpoint logic.
2.  **Integration Tests:** Test interactions between services:
    *   API Gateway <-> Admin API/Billing Service (Auth & Limits).
    *   Bot Manager <-> Billing Service (Concurrency & Quota).
    *   WhisperLive -> Collector -> Billing Service (Usage Pipeline).
3.  **End-to-End Tests:** Simulate full user workflows:
    *   Sign up, get assigned plan, hit rate limit, start bot, get transcription, exceed quota, view usage (if implemented).
4.  **Deployment:**
    *   Update CI/CD pipelines to build/test/deploy the new `billing-service`.
    *   Handle database migrations for new tables/schemas.
    *   Roll out configuration changes for existing services.
    *   Deploy analytics stack if chosen.

This plan provides a structured approach to implementing the required features across the Vexa microservices architecture. 