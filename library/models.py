from typing import Optional

from pydantic import BaseModel


class LibrarySearchRequest(BaseModel):
    """Request to search the library catalog."""

    query: Optional[str] = None
    artist: Optional[str] = None
    title: Optional[str] = None
    limit: int = 10


class LibraryItem(BaseModel):
    """A single item from the library catalog."""

    id: int
    title: Optional[str]
    artist: Optional[str]
    call_letters: Optional[str]
    call_numbers: Optional[int]
    genre: Optional[str]
    format: Optional[str]

    @property
    def call_number(self) -> str:
        """Combined call number for shelf lookup."""
        letters = self.call_letters or ""
        numbers = self.call_numbers or ""
        return f"{letters} {numbers}".strip()


class LibrarySearchResponse(BaseModel):
    """Response containing library search results."""

    results: list[LibraryItem]
    total: int
    query: Optional[str] = None
