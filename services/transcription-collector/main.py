from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Depends, Header, Security, status
import json
import logging
import uuid
import os
import asyncio
from datetime import datetime, timedelta
import redis.asyncio as redis
from sqlalchemy import select, and_, func, distinct, text
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from typing import Optional, List, Dict, Any, Set
from pydantic import ValidationError

from shared_models.database import get_db, init_db
from shared_models.models import APIToken, User, Meeting, Transcription
from shared_models.schemas import (
    TranscriptionSegment, 
    HealthResponse, 
    ErrorResponse,
    MeetingResponse,
    MeetingListResponse,
    TranscriptionResponse,
    Platform,
    WhisperLiveData
)
from filters import TranscriptionFilter

# New configuration for batch processing
BACKGROUND_TASK_INTERVAL = int(os.environ.get("BACKGROUND_TASK_INTERVAL", "10"))  # seconds
IMMUTABILITY_THRESHOLD = int(os.environ.get("IMMUTABILITY_THRESHOLD", "30"))  # seconds
REDIS_SEGMENT_TTL = int(os.environ.get("REDIS_SEGMENT_TTL", "3600"))  # 1 hour default TTL for Redis segments

app = FastAPI(
    title="Transcription Collector",
    description="Collects and stores transcriptions from WhisperLive instances."
)

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("transcription_collector")

# Security - API Key auth
API_KEY_NAME = "X-API-Key"  # Standardize header name
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_current_user(api_key: str = Security(api_key_header),
                           db: AsyncSession = Depends(get_db)) -> User:
    """Dependency to verify X-API-Key and return the associated User."""
    if not api_key:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing API token")
    
    # Find the token in the database
    result = await db.execute(
        select(APIToken, User)
        .join(User, APIToken.user_id == User.id)
        .where(APIToken.token == api_key)
    )
    token_user = result.first()
    
    if not token_user:
        logger.warning(f"Invalid API token provided: {api_key[:10]}...")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API token"
        )
    
    _token_obj, user_obj = token_user
    return user_obj

# Redis connection
redis_client = None

# Initialize transcription filter
transcription_filter = TranscriptionFilter()

# Background task reference
background_task = None

@app.on_event("startup")
async def startup():
    global redis_client, background_task
    
    # Initialize Redis connection
    redis_host = os.environ.get("REDIS_HOST", "redis")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    logger.info(f"Connecting to Redis at {redis_host}:{redis_port}")
    
    redis_client = redis.Redis(
        host=redis_host,
        port=redis_port,
        db=0,
        decode_responses=True
    )
    
    # Initialize database connection
    await init_db()
    logger.info("Database initialized.")
    
    # Start background processing task
    background_task = asyncio.create_task(process_redis_to_postgres())
    logger.info(f"Background task started with interval {BACKGROUND_TASK_INTERVAL}s and immutability threshold {IMMUTABILITY_THRESHOLD}s")

@app.on_event("shutdown")
async def shutdown():
    # Cancel background task
    if background_task:
        background_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            logger.info("Background task cancelled")
    
    # Close Redis connection
    if redis_client:
        await redis_client.close()
    
    logger.info("Application shutting down, connections closed")

@app.websocket("/collector")
async def websocket_endpoint(websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    await websocket.accept()
    connection_id = str(uuid.uuid4()) # Unique ID for this connection instance
    logger.info(f"WebSocket connection {connection_id} accepted.")

    try:
        while True:
            data = await websocket.receive_text()
            logger.debug(f"[{connection_id}] RAW Data Received: {data[:500]}...")  # Log first 500 chars of raw data

            try:
                # Attempt to parse the message using the combined WhisperLiveData schema
                whisper_data = WhisperLiveData.parse_raw(data)
                logger.info(f"[{connection_id}] Parsed WhisperLiveData: platform={whisper_data.platform.value}, native_id={whisper_data.meeting_id}, token={whisper_data.token[:5]}..., segments={len(whisper_data.segments)}")

                # 1. Validate Token and Get User
                try:
                    user = await get_user_by_token(whisper_data.token, db)
                    if not user: raise ValueError("User not found for token") # Should be handled by HTTPException in helper
                    logger.info(f"[{connection_id}] Token validated for user {user.id}")
                except HTTPException as auth_exc:
                    logger.warning(f"[{connection_id}] Auth failed for incoming data: {auth_exc.detail}")
                    # Closing might be safer if auth fails.
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=f"Authentication Failed: {auth_exc.detail}")
                    return # Exit loop and close

                # 2. Find Internal Meeting ID
                stmt_meeting = select(Meeting).where(
                    Meeting.user_id == user.id,
                    Meeting.platform == whisper_data.platform.value,
                    Meeting.platform_specific_id == whisper_data.meeting_id # Match native ID from message
                ).order_by(Meeting.created_at.desc())

                result_meeting = await db.execute(stmt_meeting)
                meeting = result_meeting.scalars().first()

                if not meeting:
                    logger.warning(f"[{connection_id}] Meeting lookup failed: No meeting found for user {user.id}, platform '{whisper_data.platform.value}', native ID '{whisper_data.meeting_id}'")
                    continue # Skip processing this message if meeting not found

                internal_meeting_id = meeting.id
                logger.info(f"[{connection_id}] Associated internal meeting ID: {internal_meeting_id}")

                # 3. Store segments in Redis (new approach)
                if whisper_data.segments:  # Check if there are segments in this message
                    segment_count = 0
                    hash_key = f"meeting:{internal_meeting_id}:segments"
                    
                    # Add meeting_id to active meetings set for background processing
                    await redis_client.sadd("active_meetings", str(internal_meeting_id))
                    
                    # Set TTL on the hash (refreshed with each new message)
                    await redis_client.expire(hash_key, REDIS_SEGMENT_TTL)
                    
                    for segment in whisper_data.segments:
                        if not segment.text or segment.start_time is None or segment.end_time is None:
                            logger.debug(f"[{connection_id}] Skipping segment with missing data for meeting {internal_meeting_id}")
                            continue
                        
                        # Format start_time as a string key
                        start_time_key = f"{segment.start_time:.3f}"
                        
                        # Create segment data dictionary (JSON-serializable)
                        segment_data = {
                            "text": segment.text,
                            "end_time": segment.end_time,
                            "language": segment.language or "en",
                            "updated_at": datetime.utcnow().isoformat()
                        }
                        
                        # Store in Redis Hash
                        await redis_client.hset(
                            hash_key, 
                            start_time_key,
                            json.dumps(segment_data)
                        )
                        segment_count += 1
                    
                    logger.info(f"[{connection_id}] Stored {segment_count} segments in Redis for meeting {internal_meeting_id}")
                else:
                     logger.info(f"[{connection_id}] Received WhisperLiveData message for meeting {internal_meeting_id} with no segments.")

            except (json.JSONDecodeError, ValidationError) as parse_error:
                logger.warning(f"[{connection_id}] Failed to parse WhisperLiveData: {parse_error}. Data: {data[:500]}...") # Log more data on error
                # Don't close connection for parse errors, just log and wait for next message
            except Exception as process_err:
                # Catch errors during token validation or DB lookup *after* parsing
                logger.error(f"[{connection_id}] Error processing WhisperLiveData for native_id {whisper_data.meeting_id if 'whisper_data' in locals() else 'unknown'}: {process_err}", exc_info=True)

    except WebSocketDisconnect:
        logger.info(f"WebSocket connection {connection_id} disconnected.")
    except Exception as e:
        logger.error(f"Unhandled error in websocket connection {connection_id}: {e}", exc_info=True)
        # Attempt to close gracefully if possible
        try:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except Exception:
            pass # Ignore errors during close after another error
    finally:
        # No connection-specific context to clean up anymore
        logger.info(f"WebSocket connection {connection_id} handler finished.")

# Background task for processing Redis segments into PostgreSQL
async def process_redis_to_postgres():
    """
    Background task that runs periodically to:
    1. Check for segments in Redis that are older than IMMUTABILITY_THRESHOLD
    2. Filter these segments
    3. Store passing segments in PostgreSQL 
    4. Remove processed segments from Redis
    """
    logger.info("Background Redis to PostgreSQL processor started")
    
    while True:
        try:
            # Sleep at the beginning to allow the system to fully initialize
            await asyncio.sleep(BACKGROUND_TASK_INTERVAL)
            
            logger.debug("Background processor checking for immutable segments...")
            
            # Get list of active meetings
            meeting_ids_raw = await redis_client.smembers("active_meetings")
            if not meeting_ids_raw:
                logger.debug("No active meetings found")
                continue
                
            meeting_ids = [mid for mid in meeting_ids_raw]
            logger.debug(f"Found {len(meeting_ids)} active meetings")
            
            # Prepare batch storage
            batch_to_store = []
            segments_to_delete_from_redis = {}  # Dict of meeting_id -> set of start_times
            
            # Get database session for this batch
            async with get_db() as db:
                # Process each meeting
                for meeting_id_str in meeting_ids:
                    try:
                        meeting_id = int(meeting_id_str)
                        hash_key = f"meeting:{meeting_id}:segments"
                        
                        # Get all segments for this meeting
                        redis_segments = await redis_client.hgetall(hash_key)
                        
                        if not redis_segments:
                            # If no segments, remove from active meetings
                            await redis_client.srem("active_meetings", meeting_id_str)
                            logger.debug(f"Removed empty meeting {meeting_id} from active meetings")
                            continue
                            
                        logger.debug(f"Processing {len(redis_segments)} segments for meeting {meeting_id}")
                        
                        # Calculate the immutability threshold time
                        immutability_time = datetime.utcnow() - timedelta(seconds=IMMUTABILITY_THRESHOLD)
                        
                        # Process each segment
                        for start_time_str, segment_json in redis_segments.items():
                            try:
                                segment_data = json.loads(segment_json)
                                segment_updated_at = datetime.fromisoformat(segment_data['updated_at'])
                                
                                # Check if segment is old enough to be considered immutable
                                if segment_updated_at < immutability_time:
                                    # Apply filtering
                                    if transcription_filter.filter_segment(segment_data['text'], language=segment_data.get('language', 'en')):
                                        # Create Transcription object
                                        new_transcription = create_transcription_object(
                                            meeting_id=meeting_id,
                                            start=float(start_time_str),
                                            end=segment_data['end_time'],
                                            text=segment_data['text'],
                                            language=segment_data.get('language')
                                        )
                                        batch_to_store.append(new_transcription)
                                    
                                    # Mark for deletion from Redis
                                    segments_to_delete_from_redis.setdefault(meeting_id, set()).add(start_time_str)
                            except (json.JSONDecodeError, KeyError, ValueError) as e:
                                logger.error(f"Error processing segment {start_time_str} for meeting {meeting_id}: {e}")
                                # Still mark for deletion to avoid processing errors repeatedly
                                segments_to_delete_from_redis.setdefault(meeting_id, set()).add(start_time_str)
                    
                    except Exception as e:
                        logger.error(f"Error processing meeting {meeting_id_str}: {e}", exc_info=True)
                
                # Write batch to PostgreSQL if we have segments
                if batch_to_store:
                    try:
                        db.add_all(batch_to_store)
                        await db.commit()
                        logger.info(f"Stored {len(batch_to_store)} segments to PostgreSQL from {len(segments_to_delete_from_redis)} meetings")
                        
                        # Delete processed segments from Redis
                        for meeting_id, start_times in segments_to_delete_from_redis.items():
                            if start_times:
                                hash_key = f"meeting:{meeting_id}:segments"
                                await redis_client.hdel(hash_key, *start_times)
                                logger.debug(f"Deleted {len(start_times)} processed segments for meeting {meeting_id} from Redis")
                    except Exception as e:
                        logger.error(f"Error committing batch to PostgreSQL: {e}", exc_info=True)
                        await db.rollback()
                else:
                    logger.debug("No segments ready for PostgreSQL storage")
        
        except asyncio.CancelledError:
            logger.info("Background processor task cancelled")
            break
    except Exception as e:
            logger.error(f"Unhandled error in background processor: {e}", exc_info=True)
            # Don't break the loop - keep trying after sleep

# Simplified function - assumes meeting_id is valid
def create_transcription_object(meeting_id: int, start: float, end: float, text: str, language: Optional[str]) -> Transcription:
    """Creates a Transcription ORM object without adding/committing."""
    return Transcription(
        meeting_id=meeting_id,
        start_time=start,
        end_time=end,
        text=text,
        language=language,
        created_at=datetime.utcnow()
    )

@app.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_db)):
    """Health check endpoint"""
    redis_status = "healthy"
    db_status = "healthy"
    
    try:
        await redis_client.ping()
    except Exception as e:
        redis_status = f"unhealthy: {str(e)}"
    
    try:
        # Use the injected session 'db'
        await db.execute(text("SELECT 1")) 
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"
    
    return HealthResponse(
        status="healthy" if redis_status == "healthy" and db_status == "healthy" else "unhealthy",
        redis=redis_status,
        database=db_status,
        timestamp=datetime.now().isoformat()
    )

@app.get("/meetings", 
         response_model=MeetingListResponse,
         summary="Get list of all meetings for the current user",
         dependencies=[Depends(get_current_user)])
async def get_meetings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns a list of all meetings initiated by the authenticated user."""
    stmt = select(Meeting).where(Meeting.user_id == current_user.id).order_by(Meeting.created_at.desc())
    result = await db.execute(stmt)
    meetings = result.scalars().all()
    return MeetingListResponse(meetings=[MeetingResponse.from_orm(m) for m in meetings])
    
@app.get("/transcripts/{platform}/{native_meeting_id}",
         response_model=TranscriptionResponse,
         summary="Get transcript for a specific meeting by platform and native ID",
         dependencies=[Depends(get_current_user)])
async def get_transcript_by_native_id(
    platform: Platform,
    native_meeting_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Retrieves the meeting details and transcript segments for a meeting specified by its platform and native ID.
    Finds the *latest* matching meeting record for the user.
    
    Now combines data from both PostgreSQL (immutable segments) and Redis (mutable segments).
    """
    logger.info(f"User {current_user.id} requested transcript for {platform.value} / {native_meeting_id}")

    # 1. Find the latest meeting matching platform and native ID for the user
    stmt_meeting = select(Meeting).where(
                Meeting.user_id == current_user.id,
        Meeting.platform == platform.value,
        Meeting.platform_specific_id == native_meeting_id
    ).order_by(Meeting.created_at.desc())

    result_meeting = await db.execute(stmt_meeting)
    meeting = result_meeting.scalars().first()
    
    if not meeting:
        logger.warning(f"No meeting found for user {current_user.id}, platform '{platform.value}', native ID '{native_meeting_id}'")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting not found for platform {platform.value} and ID {native_meeting_id}"
        )

    logger.info(f"Found meeting record ID {meeting.id} for transcript request.")
    internal_meeting_id = meeting.id

    # 2. Fetch transcript segments from PostgreSQL (immutable segments)
    stmt_transcripts = select(Transcription).where(
        Transcription.meeting_id == internal_meeting_id
    ).order_by(Transcription.start_time)

    result_transcripts = await db.execute(stmt_transcripts)
    db_segments = result_transcripts.scalars().all()
    logger.info(f"Retrieved {len(db_segments)} segments from PostgreSQL for meeting {internal_meeting_id}")
    
    # 3. Fetch segments from Redis (mutable segments)
    hash_key = f"meeting:{internal_meeting_id}:segments"
    redis_segments_raw = await redis_client.hgetall(hash_key)
    logger.info(f"Retrieved {len(redis_segments_raw)} segments from Redis for meeting {internal_meeting_id}")
    
    # 4. Merge segments, with Redis taking precedence for the same start_time
    merged_segments = {}
    
    # Add PostgreSQL segments first
    for segment in db_segments:
        key = f"{segment.start_time:.3f}"
        merged_segments[key] = TranscriptionSegment(
            start_time=segment.start_time,
            end_time=segment.end_time,
            text=segment.text,
            language=segment.language
        )
    
    # Add Redis segments (overwriting PostgreSQL ones with same start_time)
    for start_time_str, segment_json in redis_segments_raw.items():
        try:
            segment_data = json.loads(segment_json)
            merged_segments[start_time_str] = TranscriptionSegment(
                start_time=float(start_time_str),
                end_time=segment_data['end_time'],
                text=segment_data['text'],
                language=segment_data.get('language', 'en')
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Error parsing Redis segment {start_time_str}: {e}")
            # Skip this segment
    
    # Convert to sorted list
    sorted_segments = sorted(merged_segments.values(), key=lambda s: s.start_time)
    logger.info(f"Merged into {len(sorted_segments)} total segments for meeting {internal_meeting_id}")
    
    # 5. Construct the response using the found meeting and segments
    meeting_details = MeetingResponse.from_orm(meeting)

    # Combine into the final response model
    response_data = meeting_details.dict() # Get meeting data as dict
    response_data["segments"] = sorted_segments # Add merged segments list

    return TranscriptionResponse(**response_data)

# ADD Helper for token validation (or ensure it exists in an auth.py)
async def get_user_by_token(token: str, db: AsyncSession) -> Optional[User]:
    """Validates an API token and returns the associated User or raises HTTPException."""
    if not token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing API token")
    
    result = await db.execute(
        select(User).join(APIToken).where(APIToken.token == token)
    )
    user = result.scalars().first()
    
    if not user:
        logger.warning(f"Invalid API token provided in WebSocket handshake: {token[:5]}...")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API token"
        )
    return user

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=8000, 
        reload=False,
        log_level="info"
    ) 