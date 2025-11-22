import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from groq import Groq
from pydantic import BaseModel

from parser import ParsedRequest, parse_request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Request Parser",
    description="Parse unstructured song requests into structured metadata",
    version="1.0.0",
)

client: Groq | None = None


@app.on_event("startup")
async def startup():
    global client
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY environment variable not set")
        raise RuntimeError("GROQ_API_KEY environment variable not set")
    client = Groq(api_key=api_key)
    logger.info("Groq client initialized")


class ParseRequest(BaseModel):
    message: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/parse", response_model=ParsedRequest)
async def parse(request: ParseRequest):
    if client is None:
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
