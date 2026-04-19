"""Parse router with dependency injection."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from groq import AsyncGroq, RateLimitError
from pydantic import BaseModel

from core.dependencies import get_groq_client
from services.parser import ParsedRequest, parse_request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["parse"])


class ParseRequestBody(BaseModel):
    """Request body for parsing messages."""

    message: str


@router.post(
    "/parse",
    response_model=ParsedRequest,
    summary="Parse listener message",
    description="""
    Parse a natural language listener message and extract structured metadata.

    Returns information about:
    - Song, album, and artist
    - Whether it's a request or other message type
    - Message classification (request, dj_message, feedback, other)

    Example request:
    ```json
    {
        "message": "Play Bohemian Rhapsody by Queen"
    }
    ```
    """,
    responses={
        200: {"description": "Successfully parsed message"},
        400: {"description": "Invalid request (empty message)"},
        500: {"description": "Parsing failed"},
        503: {"description": "Groq service unavailable"},
    },
)
async def parse(
    request: ParseRequestBody,
    client: AsyncGroq = Depends(get_groq_client),
):
    """Parse a listener message and extract song request metadata."""
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    try:
        result = await parse_request(request.message, client)
        logger.info(f"Parsed request: is_request={result.is_request}, type={result.message_type}")
        return result
    except RateLimitError as e:
        logger.warning(f"Groq rate limit exceeded: {e}")
        raise HTTPException(status_code=429, detail="Rate limit exceeded, please retry") from e
    except ValueError as e:
        logger.error(f"Parsing error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
