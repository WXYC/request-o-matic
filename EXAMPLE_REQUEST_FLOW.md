# Example Request Flow: Finding Songs on Compilations

## Example Request

**User Input:** "Play 'Abele Dance (85 Remix)' by Manu Dibango"

## Step-by-Step Flow

### Step 1: Parse Request
```
POST /request
Body: { "message": "Play 'Abele Dance (85 Remix)' by Manu Dibango" }

Parsed Result:
- song: "Abele Dance (85 Remix)"
- artist: "Manu Dibango"
- album: null
- is_request: true
```

### Step 2: Look Up Album (Existing Logic)
```
Discogs search_track API:
GET /database/search?type=release&track=Abele Dance (85 Remix)&artist=Manu Dibango

Returns: "Electric Africa"
album_for_search = "Electric Africa"
```

### Step 3: Search Library - Attempt 1
```
Library FTS search:
query = "Manu Dibango Electric Africa"

Result: No matches ❌
(Library doesn't have "Electric Africa" by Manu Dibango)
```

### Step 4: Search Library - Attempt 2 (Existing Fallback)
```
Library FTS search (artist only):
query = "Manu Dibango"

Result: No matches ❌
(Library doesn't have any albums by Manu Dibango)
```

### Step 5: 🆕 Search Discogs for ALL Releases with Track
```
Discogs search_releases_by_track API:
GET /database/search?type=release&track=Abele Dance (85 Remix)&artist=Manu Dibango&per_page=20

Returns:
[
  ("Manu Dibango", "Electric Africa"),
  ("Various", "Celluloid Records- change the beat 1979-87"),
  ("Various Artists", "Best of Afro Funk"),
  ("Manu Dibango", "Afro Soul Machine")
]
```

### Step 6: 🆕 Check Each Release in Library
```
For each release, search library by album title ONLY:

Release 1: "Electric Africa"
  Library search: query = "Electric Africa"
  Result: No match ❌

Release 2: "Celluloid Records- change the beat 1979-87"
  Library search: query = "Celluloid Records- change the beat 1979-87"
  Result: MATCH! ✅

  Found:
  {
    "id": 12345,
    "title": "Celluloid Records- change the beat 1979-87",
    "artist": "Various Artists - Rock - C",
    "call_letters": "C",
    "artist_call_number": 1,
    "release_call_number": 1,
    "genre": "Rock",
    "format": "CD"
  }

  Stop searching (found result)
```

### Step 7: Fetch Artwork
```
For the found library item:
- Discogs search for artwork of "Celluloid Records- change the beat 1979-87"
- Returns cover image URL
```

### Step 8: Build Slack Message
```
Context: "Found 'Abele Dance (85 Remix)' by Manu Dibango on:"

Slack blocks:
┌────────────────────────────────────────────────────┐
│ 📻 Song Request                                     │
│ "Play 'Abele Dance (85 Remix)' by Manu Dibango"   │
├────────────────────────────────────────────────────┤
│ Found "Abele Dance (85 Remix)" by Manu Dibango on:│
│                                                     │
│ [Album Cover]  Celluloid Records- change the beat  │
│                1979-87                              │
│                Various Artists - Rock - C           │
│                Rock CD C 1/1                        │
│                View in Library →                    │
└────────────────────────────────────────────────────┘
```

### Step 9: Return Response
```json
{
  "parsed": {
    "song": "Abele Dance (85 Remix)",
    "artist": "Manu Dibango",
    "album": null,
    "is_request": true,
    "message_type": "request",
    "raw_message": "Play 'Abele Dance (85 Remix)' by Manu Dibango"
  },
  "artwork": {
    "artwork_url": "https://i.discogs.com/...",
    "release_url": "https://www.discogs.com/release/...",
    "album": "Celluloid Records- change the beat 1979-87",
    "artist": "Various",
    "source": "discogs",
    "confidence": 0.85
  },
  "library_results": [
    {
      "id": 12345,
      "title": "Celluloid Records- change the beat 1979-87",
      "artist": "Various Artists - Rock - C",
      "call_letters": "C",
      "artist_call_number": 1,
      "release_call_number": 1,
      "genre": "Rock",
      "format": "CD",
      "call_number": "Rock CD C 1/1",
      "library_url": "http://www.wxyc.info/wxycdb/libraryRelease?id=12345"
    }
  ]
}
```

## Why This Works

**Key Insight:** We search the library **by album title only**, not by artist name.

This allows us to match:
- Library entry: artist = "Various Artists - Rock - C"
- Discogs entry: artist = "Various"

Even though the artist names don't match, the album titles do, so we find the compilation!

## Edge Cases Handled

### Case 1: Song on Multiple Compilations
If the song appears on multiple compilations in the library, we return up to 5 results.

### Case 2: No Results Anywhere
If Discogs finds no releases with the track, we fall back to showing "No results found" with the parsed metadata.

### Case 3: Discogs Rate Limit
If we hit Discogs rate limits (429 response), we handle it gracefully and return empty results rather than crashing.

### Case 4: Original Album Also in Library
If both the original artist album AND a compilation are in the library, the original album will be found first (Step 3/4) so we never reach the compilation search. This is desirable behavior - we prefer the original artist release.

## API Calls Summary

For a successful compilation match:

1. **Groq API**: 1 call (parse request)
2. **Discogs API**: 3 calls
   - First track lookup (existing logic)
   - Search for all releases with track (new)
   - Artwork search for found album (existing)
3. **Library DB**: 2-5 queries
   - Initial search with artist+album
   - Fallback search with artist only
   - 1-3 searches for compilation releases

Total: ~4-8 total API/DB calls per request (when compilation search is needed)
