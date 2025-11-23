import logging

from dotenv import load_dotenv
from fastapi import FastAPI

from artwork.router import (
    router as artwork_router,
    init_artwork_service,
    shutdown_artwork_service,
)
from library.router import (
    router as library_router,
    init_library_service,
    shutdown_library_service,
)
from routers.health import router as health_router
from routers.parse import router as parse_router
from routers.request import router as request_router
from services.groq import init_groq_client
from services.slack import init_slack_service, shutdown_slack_service

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Request-O-Matic",
    description="Supplement song requests with structured metadata, album artwork, and library catalog info",
    version="1.0.0",
)

# Include routers
app.include_router(health_router)
app.include_router(parse_router)
app.include_router(request_router)
app.include_router(artwork_router)
app.include_router(library_router)


@app.on_event("startup")
async def startup():
    init_groq_client()
    init_artwork_service()
    await init_slack_service()
    await init_library_service()
    logger.info("All services initialized")


@app.on_event("shutdown")
async def shutdown():
    await shutdown_artwork_service()
    await shutdown_library_service()
    await shutdown_slack_service()
    logger.info("All services shut down")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
