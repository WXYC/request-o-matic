import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from library.models import LibrarySearchResponse
from library.db import LibraryDB

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/library", tags=["library"])

_db: LibraryDB | None = None


def get_db() -> LibraryDB:
    """Get the database client."""
    if _db is None:
        raise RuntimeError("Library database not initialized")
    return _db


async def init_library_service():
    """Initialize the library database connection."""
    global _db

    # Optional: allow overriding database path via env var
    db_path_str = os.getenv("LIBRARY_DB_PATH")
    db_path = Path(db_path_str) if db_path_str else None

    _db = LibraryDB(db_path=db_path)
    await _db.connect()
    logger.info("Library service initialized")


async def shutdown_library_service():
    """Close library database connection."""
    global _db
    if _db:
        await _db.close()
        _db = None
    logger.info("Library service shut down")


@router.get("/search", response_model=LibrarySearchResponse)
async def search_library(
    q: Optional[str] = Query(None, description="Full-text search query"),
    artist: Optional[str] = Query(None, description="Filter by artist name"),
    title: Optional[str] = Query(None, description="Filter by album title"),
    limit: int = Query(10, ge=1, le=100, description="Max results"),
):
    """
    Search the library catalog.

    Use `q` for full-text search across artist and title.
    Use `artist` and/or `title` for filtered searches.
    """
    if not q and not artist and not title:
        raise HTTPException(
            status_code=400,
            detail="At least one of q, artist, or title must be provided",
        )

    try:
        db = get_db()
        results = await db.search(query=q, artist=artist, title=title, limit=limit)

        return LibrarySearchResponse(
            results=results,
            total=len(results),
            query=q or f"artist={artist}, title={title}",
        )
    except RuntimeError as e:
        logger.error(f"Database not initialized: {e}")
        raise HTTPException(status_code=503, detail="Library service not available")
    except Exception as e:
        logger.error(f"Library search failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
