# Compilation & Various Artists Search Implementation

## Overview

This implementation enables finding songs on Various Artists compilations when the specific artist doesn't have the album in the library.

## Problem Statement

When a user requests a song like:
- **"'Abele Dance (85 Remix)' by Manu Dibango"**

The library may not have any albums by Manu Dibango, but the song might exist on a Various Artists compilation like:
- **"Celluloid Records- change the beat 1979-87"** (cataloged as "Various Artists - Rock - C")

Without this feature, the search would fail because:
1. We search for "Manu Dibango" + album title
2. Library only has "Various Artists - Rock - C" as the artist
3. Search returns no results

## Solution

The implementation uses Discogs as a track-level database to find ALL releases containing a requested song, then checks if any of those releases exist in our library.

### Search Flow

```
1. Parse request: song="Abele Dance (85 Remix)", artist="Manu Dibango"

2. First attempt: Search library for "Manu Dibango" + album
   ❌ No results

3. Fallback: Search library for "Manu Dibango" only
   ❌ No results

4. NEW: Search Discogs for all releases containing "Abele Dance (85 Remix)" by "Manu Dibango"
   ✅ Returns:
      - Manu Dibango - Electric Africa
      - Various - Celluloid Records- change the beat 1979-87
      - Various Artists - Best of Afro Funk

5. For each Discogs release, search library by album title only:
   ✅ Found "Celluloid Records- change the beat 1979-87" in library!

6. Return compilation to user with context message:
   "Found 'Abele Dance (85 Remix)' by Manu Dibango on:"
```

## Implementation Details

### 1. New Discogs Provider Method

**File:** `artwork/providers/discogs.py`

```python
async def search_releases_by_track(
    self, track: str, artist: Optional[str] = None, limit: int = 20
) -> list[tuple[str, str]]:
    """
    Search Discogs for ALL releases containing a track.

    Returns:
        List of (artist, album) tuples for releases containing the track.
    """
```

This queries the Discogs API with:
- `type=release`
- `track={track_name}`
- `artist={artist_name}` (optional)

Returns up to 20 releases that contain the requested track.

### 2. Artwork Router Helper

**File:** `artwork/router.py`

```python
async def lookup_releases_by_track(
    track: str, artist: Optional[str] = None, limit: int = 20
) -> list[tuple[str, str]]:
    """
    Look up all releases containing a track using Discogs.

    Returns:
        List of (artist, album) tuples for releases containing the track.
        Useful for finding compilations and alternate releases.
    """
```

Wrapper function that uses the DiscogsProvider to search for releases.

### 3. Request Handler Logic

**File:** `routers/request.py`

The main request handler now includes a third fallback step:

```python
# Step 3b: If still no results and we have a song + artist,
# search Discogs for ALL releases with that track and check our library
if not library_results and parsed.song and parsed.artist:
    logger.info(f"Searching for '{parsed.song}' on other releases (compilations, etc.)")

    releases = await lookup_releases_by_track(parsed.song, parsed.artist)

    # Check each release against our library
    for release_artist, release_album in releases:
        # Search by album title only (to catch V/A compilations)
        results = await db.search(query=release_album, limit=1)
        if results:
            library_results.extend(results)
            found_on_compilation = True
            # Limit to 5 total results
            if len(library_results) >= 5:
                library_results = library_results[:5]
                break
```

**Key Points:**
- Only triggers if song + artist provided but no library results found
- Searches library by album title ONLY (ignores artist name)
- This allows matching Various Artists compilations
- Limits results to 5 total albums
- Sets `found_on_compilation` flag for user messaging

### 4. User-Facing Messaging

When a song is found on a compilation, the Slack message includes helpful context:

```python
if found_on_compilation:
    context = f"Found \"{parsed.song}\" by {parsed.artist} on:"
```

This informs the DJ that the song was found on a different release (likely a compilation).

## Example Scenarios

### Scenario 1: Song on Various Artists Compilation

**Request:** "Play 'Abele Dance (85 Remix)' by Manu Dibango"

**Result:**
```
Found "Abele Dance (85 Remix)" by Manu Dibango on:
- Celluloid Records- change the beat 1979-87
  Artist: Various Artists - Rock - C
  Call Number: Rock CD C 1/1
```

### Scenario 2: Song on Multiple Releases

**Request:** "Play 'Dancing in the Street' by Martha and the Vandellas"

**Result (if found on multiple albums):**
```
Found "Dancing in the Street" by Martha and the Vandellas on:
- Motown Greatest Hits
  Artist: Various Artists - Soul - M
- Summer of '64
  Artist: Various Artists - Soul - S
```

### Scenario 3: Song Only on Original Album

**Request:** "Play 'Love Will Tear Us Apart' by Joy Division"

**Result:**
```
(No special message - works like normal search)
- Closer
  Artist: Joy Division
```

## Testing

New test class `TestTrackSearch` in `tests/test_artwork.py` includes:

1. **test_search_releases_by_track**: Verifies multiple releases are returned
2. **test_search_releases_by_track_no_artist**: Searches without artist filter
3. **test_search_releases_by_track_no_results**: Handles no results gracefully
4. **test_search_releases_by_track_rate_limit**: Handles API rate limiting

Run tests:
```bash
pytest tests/test_artwork.py::TestTrackSearch -v
```

## Performance Considerations

1. **Additional API calls**: This adds a Discogs API call when the first two searches fail
2. **Multiple library searches**: May search the library multiple times (once per Discogs result)
3. **Rate limiting**: Discogs has rate limits; implementation gracefully handles 429 responses

## Future Improvements

1. **Caching**: Cache Discogs track search results to avoid repeated API calls
2. **Track table**: Add a dedicated `tracks` table to the library database for faster lookups
3. **Fuzzy matching**: Use fuzzy string matching when comparing album titles
4. **Sorting**: Prioritize original artist albums over compilations in results
