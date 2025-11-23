import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.groq import get_groq_client
from services.parser import ParsedRequest, parse_request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["parse"])


class ParseRequestBody(BaseModel):
    message: str


@router.post("/parse", response_model=ParsedRequest)
async def parse(request: ParseRequestBody):
    """Parse a listener message and extract song request metadata."""
    try:
        client = get_groq_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Groq client not initialized")

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    try:
        result = parse_request(request.message, client)
        logger.info(f"Parsed request: is_request={result.is_request}, type={result.message_type}")
        return result
    except ValueError as e:
        logger.error(f"Parsing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
