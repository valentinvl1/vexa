import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import logging
import os
import base64
from typing import Optional
import redis.asyncio as aioredis
import asyncio # <--- Import asyncio

# Local imports - Remove unused ones
# from app.database.models import init_db # Using local init_db now
# from app.database.service import TranscriptionService # Not used here
# from app.tasks.monitoring import celery_app # Not used here

from config import BOT_IMAGE_NAME, REDIS_URL
from docker_utils import get_socket_session, close_docker_client, start_bot_container, stop_bot_container, _record_session_start
from shared_models.database import init_db, get_db, async_session_local
from shared_models.models import User, Meeting # Import Meeting model
from shared_models.schemas import MeetingCreate, MeetingResponse, Platform # Import new schemas and Platform
from auth import get_user_and_token # Import the new dependency
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_
from datetime import datetime # For start_time

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bot_manager")

# Initialize the FastAPI app
app = FastAPI(title="Vexa Bot Manager")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models - Use schemas from shared_models
# class BotRequest(BaseModel): ... -> Replaced by MeetingCreate
# class BotResponse(BaseModel): ... -> Replaced by MeetingResponse

@app.on_event("startup")
async def startup_event():
    logger.info("Starting up Bot Manager...")
    # await init_db() # Removed - Admin API should handle this
    # await init_redis() # Removed redis init if not used elsewhere
    try:
        get_socket_session()
    except Exception as e:
        logger.error(f"Failed to initialize Docker client on startup: {e}", exc_info=True)
    logger.info("Database and Docker Client initialized (attempted).")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Bot Manager...")
    # await close_redis() # Removed redis close if not used
    close_docker_client()
    logger.info("Docker Client closed.")

@app.get("/", include_in_schema=False)
async def root():
    return {"message": "Vexa Bot Manager is running"}

@app.post("/bots",
          response_model=MeetingResponse,
          status_code=status.HTTP_201_CREATED,
          summary="Request a new bot instance to join a meeting",
          dependencies=[Depends(get_user_and_token)])
async def request_bot(
    req: MeetingCreate,
    auth_data: tuple[str, User] = Depends(get_user_and_token),
    db: AsyncSession = Depends(get_db)
):
    """Handles requests to launch a new bot container for a meeting.
    Requires a valid API token associated with a user.
    - Constructs the meeting URL from platform and native ID.
    - Creates a Meeting record in the database.
    - Starts a Docker container for the bot, passing user token, internal meeting ID, native meeting ID, and constructed URL.
    - Updates the Meeting record with container details and status.
    - Returns the created Meeting details.
    """
    # Unpack the token and user from the dependency result
    user_token, current_user = auth_data

    logger.info(f"Received bot request for platform '{req.platform.value}' with native ID '{req.native_meeting_id}' from user {current_user.id}")
    native_meeting_id = req.native_meeting_id # Store native_meeting_id for clarity

    # 1. Construct meeting URL
    constructed_url = Platform.construct_meeting_url(req.platform.value, native_meeting_id)
    if not constructed_url:
        # Handle cases where URL construction isn't possible (e.g., Teams, invalid ID format)
        # Depending on policy, either reject or proceed without a URL for the bot if it can handle it
        logger.warning(f"Could not construct meeting URL for platform {req.platform.value} and ID {native_meeting_id}. Proceeding without URL for bot.")
        # Or raise HTTPException: 
        # raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Could not construct URL for platform {req.platform.value} with ID {native_meeting_id}. Invalid ID or unsupported construction.")

    # 2. Check for existing active meeting for this user/platform/native_id
    existing_meeting_stmt = select(Meeting).where(
        Meeting.user_id == current_user.id,
        Meeting.platform == req.platform.value,
        Meeting.platform_specific_id == native_meeting_id,
        Meeting.status.in_(['requested', 'active'])
    )
    result = await db.execute(existing_meeting_stmt)
    existing_meeting = result.scalars().first()

    if existing_meeting:
        logger.warning(f"User {current_user.id} requested duplicate bot for active/requested meeting {existing_meeting.id} ({req.platform.value} / {native_meeting_id})")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active or requested meeting already exists for this platform and meeting ID. Meeting ID: {existing_meeting.id}"
        )

    # 3. Create Meeting record in DB
    new_meeting = Meeting(
        user_id=current_user.id,
        platform=req.platform.value,
        platform_specific_id=native_meeting_id, # Correct field name
        status='requested'
    )
    db.add(new_meeting)
    await db.commit()
    await db.refresh(new_meeting)
    meeting_id = new_meeting.id # Internal DB ID
    logger.info(f"Created meeting record with ID: {meeting_id}")

    # 4. Start the bot container
    container_id = None
    connection_id = None # Initialize connection_id
    try:
        logger.info(f"Attempting to start bot container for meeting {meeting_id} (native: {native_meeting_id})...")
        # MODIFY the call to start_bot_container:
        # Unpack both container_id and connection_id
        container_id, connection_id = start_bot_container(
            meeting_id=meeting_id,           # Internal DB ID
            meeting_url=constructed_url,     # Constructed URL (still pass it to bot if needed)
            platform=req.platform.value,     # Platform string
            bot_name=req.bot_name,
            user_token=user_token,           # *** ADDED: Pass the user's API token ***
            native_meeting_id=native_meeting_id, # *** ADDED: Pass the native meeting ID ***
            language=req.language,           # *** ADDED: Pass language ***
            task=req.task                    # *** ADDED: Pass task ***
        )
        logger.info(f"Call to start_bot_container completed. Container ID: {container_id}, Connection ID: {connection_id}") # Log both IDs

        if not container_id or not connection_id:
            # Log specific error based on which ID is missing
            error_msg = "Failed to start bot container."
            if not container_id:
                error_msg += " Container ID not returned."
            if not connection_id:
                error_msg += " Connection ID not generated/returned."
            logger.error(f"{error_msg} for meeting {meeting_id}")
            
            # Update status immediately if start failed
            new_meeting.status = 'error'
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"status": "error", "message": error_msg, "meeting_id": meeting_id}
            )

        # *** Schedule session start recording AFTER successful container start ***
        asyncio.create_task(_record_session_start(meeting_id, connection_id))
        logger.info(f"Scheduled background task to record session start for meeting {meeting_id}, session {connection_id}")

        # 5. Update Meeting record with container details and status
        logger.info(f"Attempting to update meeting {meeting_id} status to active with container ID {container_id}...") # Log before update
        new_meeting.bot_container_id = container_id
        new_meeting.status = 'active'
        new_meeting.start_time = datetime.utcnow()
        await db.commit()
        await db.refresh(new_meeting)
        logger.info(f"Successfully updated meeting {meeting_id} status.") # Log after update

        logger.info(f"Successfully started bot container {container_id} for meeting {meeting_id}")
        return MeetingResponse.from_orm(new_meeting)

    except Exception as e:
        # Enhanced logging in the exception handler
        logger.error(f"Exception occurred during bot startup process for meeting {meeting_id} (after DB creation): {e}", exc_info=True)
        # Attempt to update status to error even if container start failed or subsequent update failed
        try:
            # Fetch again in case session state is lost or object is detached
            meeting_to_update = await db.get(Meeting, meeting_id)
            if meeting_to_update and meeting_to_update.status != 'error': # Avoid redundant updates
                 logger.warning(f"Updating meeting {meeting_id} status to 'error' due to exception.")
                 meeting_to_update.status = 'error'
                 # Assign container ID even if update failed later, helps debugging
                 if container_id:
                     meeting_to_update.bot_container_id = container_id
                 await db.commit()
            elif not meeting_to_update:
                logger.error(f"Could not find meeting {meeting_id} to update status to error.")
        except Exception as db_err:
             logger.error(f"Failed to update meeting {meeting_id} status to error after bot startup exception: {db_err}")

        # Re-raise HTTPException to send appropriate response to client
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": f"An unexpected error occurred during bot startup: {str(e)}", "meeting_id": meeting_id}
        )

# --- Background Task for Stopping Container ---
async def _handle_container_stop(meeting_id: int, container_id: str):
    """
    Background task to update status to stopping, stop the Docker container,
    and update the final meeting status.
    """
    current_status = 'stopping' # Initial status for this task
    # Update DB to 'stopping' first using a new session
    async with async_session_local() as db:
        try:
            stmt = select(Meeting).where(Meeting.id == meeting_id)
            result = await db.execute(stmt)
            meeting = result.scalars().first()
            if meeting and meeting.status == 'active': # Only proceed if it was active
                meeting.status = current_status
                meeting.end_time = datetime.utcnow() # Mark when stop process started
                await db.commit()
                logger.info(f"[Background Task] Updated meeting {meeting_id} status to '{current_status}'. Proceeding to stop container.")
            elif not meeting:
                 logger.error(f"[Background Task] Could not find meeting {meeting_id} to initiate stop.")
                 return # Exit task if meeting not found
            else: # Meeting exists but wasn't active (e.g., requested, already stopping/stopped)
                 logger.warning(f"[Background Task] Meeting {meeting_id} status was '{meeting.status}'. Stop initiation skipped.")
                 return # Exit task if meeting wasn't in active state
        except Exception as e:
            logger.error(f"[Background Task] DB error setting status to '{current_status}' for meeting {meeting_id}: {e}", exc_info=True)
            return # Exit task if initial DB update fails
    
    # Now attempt to stop the container
    logger.info(f"[Background Task] Attempting to stop container {container_id} for meeting {meeting_id}")
    # Consider running synchronous blocking call in a thread pool executor
    # For now, call directly (might block event loop if long)
    stopped = stop_bot_container(container_id) 

    final_status = 'error' # Default to error
    if stopped:
        logger.info(f"[Background Task] Successfully stopped/confirmed stopped container {container_id} for meeting {meeting_id}")
        final_status = 'stopped'
    else:
        logger.error(f"[Background Task] Stop command failed or container {container_id} not found by Docker for meeting {meeting_id}. Marking as error.")

    # Update DB with final status using a new session
    async with async_session_local() as db:
        try:
            # Fetch the record again to perform the final update
            stmt = select(Meeting).where(Meeting.id == meeting_id)
            result = await db.execute(stmt)
            meeting = result.scalars().first()
            if meeting:
                # Only update if it's still in 'stopping' state (or maybe active if first update failed?)
                # Let's update regardless of previous state, as this is the final outcome
                meeting.status = final_status
                await db.commit()
                logger.info(f"[Background Task] Updated meeting {meeting_id} final status to '{final_status}'")
            else:
                logger.error(f"[Background Task] Could not find meeting {meeting_id} to update final status.")
        except Exception as e:
            logger.error(f"[Background Task] DB error updating final status for meeting {meeting_id}: {e}", exc_info=True)


@app.delete("/bots/{platform}/{native_meeting_id}",
             # Change status code to 202 Accepted if we want to strictly follow async pattern
             status_code=status.HTTP_202_ACCEPTED, 
             # status_code=status.HTTP_200_OK, # Keep 200 OK for now
             # response_model=MeetingResponse, # Return minimal response now
             summary="Request stop for a running bot",
             description="Requests the bot associated with the platform and native meeting ID to stop. Returns 202 Accepted immediately while stop happens in background.",
             dependencies=[Depends(get_user_and_token)])
async def stop_bot(
    platform: Platform,
    native_meeting_id: str,
    background_tasks: BackgroundTasks, # <--- Inject BackgroundTasks
    auth_data: tuple[str, User] = Depends(get_user_and_token),
    db: AsyncSession = Depends(get_db)
):
    """Requests the stop of the bot container associated with the platform and native meeting ID.
    Verifies ownership. Schedules background task for actual stop and DB updates.
    Returns 202 Accepted immediately.
    """
    # Unpack the token and user from the dependency result
    user_token, current_user = auth_data

    logger.info(f"User {current_user.id} requested to stop bot for platform '{platform.value}' with native ID: '{native_meeting_id}'")

    # 1. Find the *latest* meeting in a potentially stoppable state ('requested', 'active')
    # Let the background task handle idempotency for 'stopping' state
    stmt = select(Meeting).where(
        Meeting.user_id == current_user.id,
        Meeting.platform == platform.value,
        Meeting.platform_specific_id == native_meeting_id,
        Meeting.status.in_(['requested', 'active']) 
    ).order_by(Meeting.created_at.desc())

    result = await db.execute(stmt)
    meeting = result.scalars().first()

    # Prepare response content (will be returned if meeting found or not)
    response_content = {"message": "Stop request accepted and is being processed."} 

    if not meeting:
        # Check if a meeting exists but is already in a final state ('stopping', 'stopped', 'error')
        stmt_final = select(Meeting).where(
            Meeting.user_id == current_user.id,
            Meeting.platform == platform.value,
            Meeting.platform_specific_id == native_meeting_id,
            # Meeting.status.in_(['stopping', 'stopped', 'error']) # Check final/pending states
        ).order_by(Meeting.created_at.desc()).limit(1)
        result_final = await db.execute(stmt_final)
        final_meeting = result_final.scalars().first()
        
        if final_meeting:
            logger.warning(f"Attempt to stop meeting for {platform.value}/{native_meeting_id} which is already in status '{final_meeting.status}' (Meeting ID: {final_meeting.id}). Accepting request anyway for idempotency.")
            # Still return 202 Accepted, the state won't change
            return response_content
        else:
            logger.warning(f"No meeting found for user {current_user.id}, platform '{platform.value}', native ID '{native_meeting_id}' to stop.")
            # Return 404 if no meeting record exists at all
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No meeting found for platform {platform.value} and meeting ID {native_meeting_id}.")

    # Found a meeting in 'requested' or 'active' state - meeting.id is the internal ID
    internal_meeting_id = meeting.id
    logger.info(f"Found meeting {internal_meeting_id} (status: {meeting.status}) matching {platform.value}/{native_meeting_id} for user {current_user.id}")

    # 2. Handle based on current state ('requested' or 'active')
    container_id = meeting.bot_container_id
    
    if meeting.status == 'active':
        if container_id:
            logger.info(f"Scheduling background stop for container {container_id} for meeting {internal_meeting_id}")
            # Schedule the DB updates and actual stop
            background_tasks.add_task(_handle_container_stop, internal_meeting_id, container_id)
        else:
             # Should not happen for 'active' status, but handle defensively
             logger.error(f"Meeting {internal_meeting_id} is active but has no container ID. Cannot schedule stop.")
             # Fall through to return 202, but log error. Background task won't run.

    elif meeting.status == 'requested':
        # No container assigned yet. Update status directly to 'stopped' in background?
        # For now, let's just accept the request. If a container gets created later,
        # the stop logic might need refinement. Or maybe update to 'stopped' here.
        # Let's schedule the background task anyway, it will handle the 'requested' state check.
        logger.info(f"Meeting {internal_meeting_id} is in 'requested' state. Scheduling stop handler (will likely just update DB). Container ID: {container_id}")
        background_tasks.add_task(_handle_container_stop, internal_meeting_id, container_id if container_id else "N/A") # Pass placeholder if no ID
        
    # --- Return 202 Accepted immediately --- 
    # Avoid committing/refreshing in the main handler
    logger.info(f"Stop request for meeting {internal_meeting_id} accepted. Processing in background.")
    return response_content


# Remove old/debug endpoints if they exist

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080, # Default port for bot-manager
        reload=True # Enable reload for development if needed
    ) 