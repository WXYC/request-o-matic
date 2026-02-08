-- Create trigram indexes for fuzzy text search
-- Run AFTER data import: psql -U postgres -d discogs -f 05-create-indexes.sql
--
-- These indexes enable fast fuzzy matching using pg_trgm extension.
-- Index creation on large tables can take 10-30 minutes.

-- Ensure extension is loaded
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================
-- Trigram indexes for fuzzy text search
-- ============================================

-- 1. Track title search: "Find releases containing track 'Blue Monday'"
--    Used by: search_releases_by_track()
--    Query pattern: WHERE lower(title) % $1 OR lower(title) ILIKE '%' || $1 || '%'
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_release_track_title_trgm
ON release_track USING GIN (lower(title) gin_trgm_ops);

-- 2. Artist name search on releases: "Find releases by 'New Order'"
--    Used by: search_releases_by_track() artist filter
--    Query pattern: WHERE lower(artist_name) % $1
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_release_artist_name_trgm
ON release_artist USING GIN (lower(artist_name) gin_trgm_ops);

-- 3. Track artist search: "Find compilation tracks by 'Joy Division'"
--    Used by: validate_track_on_release() for compilations
--    Query pattern: WHERE lower(artist_name) % $1
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_release_track_artist_name_trgm
ON release_track_artist USING GIN (lower(artist_name) gin_trgm_ops);

-- 4. Release title search: "Find releases named 'Power, Corruption & Lies'"
--    Used by: get_release searches
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_release_title_trgm
ON release USING GIN (lower(title) gin_trgm_ops);

-- ============================================
-- Verification queries
-- ============================================

-- Check index sizes
-- SELECT
--     indexrelname AS index_name,
--     pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
-- FROM pg_stat_user_indexes
-- WHERE schemaname = 'public'
-- ORDER BY pg_relation_size(indexrelid) DESC;

-- Test trigram search (should use index)
-- EXPLAIN ANALYZE
-- SELECT * FROM release_track
-- WHERE lower(title) % 'blue monday'
-- LIMIT 10;
