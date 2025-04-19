Okay, assuming you have successfully stashed the recent changes and reverted the `bot-manager` and `vexa-bot` codebases to a stable version where bots start reliably (even if they don't leave gracefully or support runtime config), here is the revised, chunked implementation plan using Redis Pub/Sub.

**Overall Objectives:**

1.  **Initial Configuration:** Set transcription `language` and `task` on bot creation.
2.  **Runtime Reconfiguration:** Change `language`/`task` for active bots.
3.  **Graceful Leave:** Have bots attempt to leave the meeting cleanly before container termination.

**Chosen Architecture:** Redis Pub/Sub for runtime commands (`reconfigure`, `leave`).

**Prerequisites & Notes:**
*   **`whisperlive` Confirmation:** Research confirms `whisperlive` accepts `language`/`task`/`uid`/etc. via an initial JSON message sent over the WebSocket upon connection. No changes are needed in `whisperlive` for Phase 1.
*   **`bot-manager` Redis Client:** The `aioredis` client needed for publishing commands in Phase 3/4 must be initialized (e.g., during startup) in `services/bot-manager/main.py`, as the existing general Redis init is commented out. **[DONE in Phase 3]**
*   **Unique ID:** The `connectionId` generated in `docker_utils.py` and passed via `BOT_CONFIG` to `vexa-bot` will be used for the Pub/Sub channel (`bot_commands:{connectionId}`).
*   **`vexa-bot` Network:** The current `DOCKER_NETWORK=vexa_vexa_default` setting is confirmed to be working. No change is needed.

---

**Phase 1: Initial Configuration (`language`/`task` on Start) - ✅ COMPLETE & VERIFIED**

*   **Goal:** Allow passing optional `language` and `task` during bot creation (`POST /bots`) and have `vexa-bot` use these when establishing the initial WebSocket connection to `whisperlive`.
*   **Steps:**
    1.  **(Schema):** Modify `libs/shared-models/shared_models/schemas.py`: Add `language: Optional[str] = None`, `task: Optional[str] = None` to the `MeetingCreate` Pydantic model.
    2.  **(Bot Manager API):** Modify `services/bot-manager/main.py` -> `request_bot` function: Ensure it correctly passes the received `req.language` and `req.task` values down to the `start_bot_container` function call.
    3.  **(Bot Manager Docker):** Modify `services/bot-manager/docker_utils.py` -> `start_bot_container` function:
        *   Accept optional `language` and `task` parameters.
        *   Add `language`, `task` (if provided, else `None`), and the `redis_url` (e.g., `os.getenv("REDIS_URL", "redis://redis:6379/0")`) to the `bot_config_data` dictionary. Ensure `None` values are handled correctly during JSON serialization (they should ideally be included as `null` or omitted if `whisperlive` / `vexa-bot` prefers). Clean the dict of `None` values before `json.dumps`.
    4.  **(Vexa Bot Config Schema):** Modify `services/vexa-bot/core/src/docker.ts` (or wherever `BotConfigSchema` is defined): Add `language: z.string().nullish()`, `task: z.string().nullish()`, and `redisUrl: z.string()` to the Zod schema.
    5.  **(Vexa Bot Type):** Modify `services/vexa-bot/core/src/types.ts` -> `BotConfig` type: Add `language?: string | null`, `task?: string | null`, and `redisUrl: string`.
    6.  **(Vexa Bot Config Usage):** Modify `services/vexa-bot/core/src/index.ts` (or the main entry point):
        *   Parse `language`, `task`, `redisUrl`, and `connectionId` from the validated `botConfig`.
        *   Store `language`, `task`, and `connectionId` in module-level variables so the WebSocket logic and Redis listener can access the *current* desired values.
    7.  **(Vexa Bot WebSocket):** Modify the WebSocket connection logic (likely in `services/vexa-bot/core/src/platforms/google.ts` -> `setupWebSocket` or the `evaluate` block within `startRecording`):
        *   When sending the *initial* config message (the first message after WS connection), retrieve the stored `language`, `task`, `connectionId`, `platform`, `meetingUrl`, `token`, `nativeMeetingId` etc. from the module-level variables derived from `BOT_CONFIG`.
        *   Construct the JSON payload including these fields (`uid` should be `connectionId`). Send `null` for optional fields if they are `null`/`undefined`.
*   **Testing (After Rebuilding `bot-manager` & `vexa-bot`):**
    *   `Test 1.1:` Call `POST /bots` *without* language/task. Check `vexa-bot` logs: verify the initial WS config message sends `language: null` (or similar default) and `task: null` (or default like 'transcribe'), along with `uid` (matching `connectionId`), `platform`, `meetingUrl`, `token`, `meeting_id` (matching nativeMeetingId). Note the `connectionId`.
    *   `Test 1.2:` Stop the previous bot (`DELETE /bots/...`).
    *   `Test 1.3:` Call `POST /bots` *with* `language: "es"` and `task: "translate"`. Check `vexa-bot` logs: verify the initial WS config message sends `language: "es"` and `task: "translate"`. Note the `connectionId`.
    *   `Test 1.4:` Stop the second bot (`DELETE /bots/...`).

**Phase 2: `vexa-bot` Redis Client & Command Listener Setup - ✅ COMPLETE**

*   **Goal:** Enable `vexa-bot` to connect to Redis and listen for commands on its unique channel.
*   **Status:** This was implemented as part of Phase 3.
*   **Steps:**
    1.  **(Vexa Bot Dependency):** Added `redis` to `package.json`. **[DONE]**
    2.  **(Vexa Bot Redis Logic):** Modified `services/vexa-bot/core/src/index.ts`: **[DONE]**
        *   Imported `redis` library.
        *   Created Redis client instance (`subscriber`).
        *   Defined subscription channel `bot_commands:{currentConnectionId}`.
        *   Defined `handleRedisMessage` function (initially just logging, now expanded in Phase 3).
        *   Subscribed to channel using `subscriber.subscribe`.
*   **Testing:** Initial connection/subscription verified. Full command handling tested in Phase 3.

**Phase 3: Implement Runtime Reconfiguration (Language/Task via Redis) - ✅ COMPLETE & VERIFIED**

*   **Goal:** Change `language`/`task` via `PUT /bots/.../config`, triggering a WebSocket reconnect in the bot using the new parameters.
*   **Session Handling Clarification:** To send the `reconfigure` command to the correct *running* bot instance, `bot-manager` **must** identify the *earliest* `session_uid` (the original `connectionId`) associated with the active meeting (from the `MeetingSession` table). It uses this `session_uid` to publish the command to the correct Redis channel (`bot_commands:{original_session_uid}`). The running `vexa-bot` instance receives this command, signals its browser context, closes the existing WebSocket connection, and then initiates a **new WebSocket connection** (by re-running its `setupWebSocket` logic which executes `new WebSocket(...)`) to WhisperLive using the updated configuration parameters (`language`, `task`). Crucially, for this new connection, `vexa-bot` **generates a new, unique session identifier (`uid`)** to distinguish this reconfigured session. This new `uid` is sent in the initial WebSocket message. Downstream processing (e.g., by `transcription-collector` processing the `session_start` event from `whisperlive`) **results in a new `MeetingSession` record** being created in the database for this new `uid`.
*   **Steps:**
    1.  **(Bot Manager: Redis Client Init):** Modified `services/bot-manager/main.py`: Initialized `aioredis` client in `startup_event`, added global variable, added closing logic in `shutdown_event`. **[DONE]**
    2.  **(Bot Manager: API Endpoint):** Modified `services/bot-manager/main.py`: Added `MeetingConfigUpdate` Pydantic model, added `PUT /bots/{platform}/{native_meeting_id}/config` endpoint, implemented logic to find the *most recent* active meeting and its *earliest* session UID, construct payload, and publish via Redis client. **[DONE]**
    3.  **(API Gateway: Routing):** Modified `services/api-gateway/main.py`: Added route definition for `PUT /bots/{platform}/{native_meeting_id}/config` to forward requests to `bot-manager`. **[DONE]**
    4.  **(Vexa Bot: Browser Reconfig):** Modified `services/vexa-bot/core/src/platforms/google.ts` (inside `page.evaluate`): Added browser-scope state variables (`currentWsLanguage`, `currentWsTask`), added UUID generation, modified WS `onopen` payload to use the new UUID and current language/task, exposed `window.triggerWebSocketReconfigure` function to update browser state and close socket. **[DONE]**
    5.  **(Vexa Bot: Node Handler):** Modified `services/vexa-bot/core/src/index.ts`: Made `handleRedisMessage` async, added `page` parameter, implemented `reconfigure` action handling to update Node state and call `page.evaluate` to trigger the browser function. Updated `redisSubscriber.subscribe` call to pass `page` and added debug logging. **[DONE]**
    6.  **(Vexa Bot: Build Fix):** Resolved `docker build` context issue for `entrypoint.sh`. **[DONE]**
*   **Testing:**
    *   Verified `bot-manager` publishes `reconfigure` command to the correct Redis channel (`bot_commands:{original_connectionId}`).
    *   Verified `vexa-bot` receives command via Redis and logs receipt.
    *   Verified `vexa-bot` calls browser function, closes WebSocket, and reconnects using new parameters (language/task).
    *   Verified each WebSocket reconnection generates and uses a **new unique session ID (UUID)** in the initial config message.
    *   Verified `transcription-collector` processes the `session_start` event for each new connection UID.
    *   Verified a **new `MeetingSession` row** is created in the database for each unique session UID associated with the meeting.
    *   Verified multiple sequential reconfigurations work correctly.

**Phase 4: Implement Graceful Leave via Redis & Delayed Stop - ⏳ TO DO**

*   **Goal:** Trigger graceful leave in `vexa-bot` via Redis before `bot-manager` forcefully stops the container.
*   **Steps:**
    1.  **(Vexa Bot Leave Function):** Implement/Refine `performGracefulLeave()` async function in `services/vexa-bot/core/src/index.ts`:
        *   Contain Playwright logic (find/click "Leave call", wait, find/click "Just leave call", wait). Use `isConnected`/`isClosed` checks.
        *   Attempt `await browserInstance?.close();` (use optional chaining).
        *   Call `process.exit(0)`.
    2.  **(Vexa Bot Leave Handler):** Modify `services/vexa-bot/core/src/index.ts` -> Redis `messageHandler`:
        *   If `message.action === "leave"`:
            *   Set `isShuttingDown = true`.
            *   Log the leave command.
            *   Call `await performGracefulLeave()`. *Handle potential errors here*.
    3.  **(Bot Manager Delayed Stop Task):** Modify `services/bot-manager/main.py`:
        *   Create `_delayed_container_stop(container_id: str, delay_seconds: int = 30)` async background task.
        *   Inside: `await asyncio.sleep(delay_seconds)`, then log attempt, then call *synchronous* `stop_bot_container(container_id)`. Log result.
    4.  **(Bot Manager Delete API):** Modify `services/bot-manager/main.py` -> `stop_bot` (`DELETE /bots/...`) endpoint:
        *   Find active `Meeting` and latest `session_uid` (from `MeetingSession` table). Handle not found.
        *   Construct leave payload: `json.dumps({"action": "leave", "uid": session_uid})`.
        *   Get the initialized `aioredis` client.
        *   Publish payload to Redis channel `bot_commands:{session_uid}`.
        *   Schedule the *delayed* stop task: `background_tasks.add_task(_delayed_container_stop, container_id, 30)` (get `container_id` from the `Meeting` record).
        *   Return `202 Accepted`.
*   **Testing (After Rebuilding `bot-manager` & `vexa-bot`):**
    *   `Test 4.1:` Start a bot.
    *   `Test 4.2:` Call `DELETE /bots/...`. Verify `202 Accepted`.
    *   `Test 4.3:` Check `vexa-bot` logs: Verify "leave" command received, button clicks attempted/logged, browser closed, process exited. Verify container disappears soon after (much less than 30s).
    *   `Test 4.4:` (Fallback Test) Temporarily modify `performGracefulLeave` to *not* call `process.exit(0)`. Rebuild/restart bot. Start bot. Call `DELETE /bots/...`. Verify bot logs show leave attempt but process doesn't exit. Verify container is forcefully stopped by `bot-manager`'s delayed task after ~30 seconds (check `bot-manager` logs for the delayed stop attempt). Stop the bot properly after test.

**Phase 5: (Optional but Recommended) Add Signal Handling Back - ⏳ TO DO**

*   **Goal:** Make `SIGTERM`/`SIGINT` also trigger the graceful leave as a fallback.
*   **Steps:**
    1.  **(Vexa Bot Signal Handler):** Modify `services/vexa-bot/core/src/index.ts`:
        *   Define `gracefulShutdown(signal: string)` function.
        *   Inside, check `isShuttingDown` flag. If already set, return.
        *   Set `isShuttingDown = true`.
        *   Log signal received.
        *   Call `await performGracefulLeave()`.
    2.  **(Vexa Bot Register Handlers):** Add `process.on('SIGTERM', ...)` and `process.on('SIGINT', ...)` to call `gracefulShutdown`.
    3.  **(Vexa Bot Entrypoint):** Modify `services/vexa-bot/core/entrypoint.sh` to use `exec node dist/index.js` (or `exec node dist/docker.js` if that's still the entry point after compilation) so Node receives the signal.
*   **Testing (After Rebuilding `vexa-bot`):**
    *   `Test 5.1:` Start a bot. Get its container ID (`docker ps`).
    *   `Test 5.2:` Run `docker stop {container_id}`.
    *   `Test 5.3:` Check `vexa-bot` logs: Verify `[SIGTERM]` received, leave attempted, process exited cleanly.

This chunked plan allows for incremental development and testing of each functional piece. Remember to rebuild the necessary images (`api-gateway`, `bot-manager`, `vexa-bot`) after applying the code changes for each phase.
