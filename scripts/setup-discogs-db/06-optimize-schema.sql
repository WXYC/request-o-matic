-- Optimize Discogs cache schema: drop unused tables/columns, add FK constraints
-- Run once against the existing database to migrate in-place.
--
-- Usage:
--   psql -U postgres -d discogs -f 06-optimize-schema.sql
--
-- After running, execute VACUUM FULL separately (locks tables):
--   VACUUM FULL release;
--   VACUUM FULL release_artist;
--   VACUUM FULL release_track;
--   VACUUM FULL release_track_artist;

BEGIN;

-- ============================================
-- 1. Drop unused tables
-- ============================================

DROP TABLE IF EXISTS release_label CASCADE;
DROP TABLE IF EXISTS release_genre CASCADE;
DROP TABLE IF EXISTS release_style CASCADE;
DROP TABLE IF EXISTS artist CASCADE;

-- ============================================
-- 2. Add artwork_url column to release
-- ============================================

ALTER TABLE release ADD COLUMN IF NOT EXISTS artwork_url text;

-- Populate from release_image if that table exists.
-- Only keep primary images (one per release).
-- Uncomment and run manually if release_image table is available:
--
-- UPDATE release r SET artwork_url = ri.uri
-- FROM (SELECT DISTINCT ON (release_id) release_id, uri
--        FROM release_image WHERE type = 'primary'
--        ORDER BY release_id, uri) ri
-- WHERE r.id = ri.release_id;
--
-- DROP TABLE IF EXISTS release_image CASCADE;

-- ============================================
-- 3. Add release_year column (extract from released text)
-- ============================================

ALTER TABLE release ADD COLUMN IF NOT EXISTS release_year smallint;

UPDATE release SET release_year = CAST(LEFT(released, 4) AS smallint)
WHERE released IS NOT NULL
  AND released ~ '^\d{4}';

-- ============================================
-- 4. Drop unused columns from release
-- ============================================

ALTER TABLE release DROP COLUMN IF EXISTS notes;
ALTER TABLE release DROP COLUMN IF EXISTS data_quality;
ALTER TABLE release DROP COLUMN IF EXISTS status;
ALTER TABLE release DROP COLUMN IF EXISTS country;
ALTER TABLE release DROP COLUMN IF EXISTS released;

-- ============================================
-- 5. Drop unused columns from release_artist
-- ============================================

ALTER TABLE release_artist DROP COLUMN IF EXISTS artist_id;
ALTER TABLE release_artist DROP COLUMN IF EXISTS anv;
ALTER TABLE release_artist DROP COLUMN IF EXISTS position;
ALTER TABLE release_artist DROP COLUMN IF EXISTS join_string;
ALTER TABLE release_artist DROP COLUMN IF EXISTS role;
ALTER TABLE release_artist DROP COLUMN IF EXISTS tracks;

-- ============================================
-- 6. Drop unused columns from release_track
-- ============================================

ALTER TABLE release_track DROP COLUMN IF EXISTS parent;
ALTER TABLE release_track DROP COLUMN IF EXISTS track_id;

-- ============================================
-- 7. Drop unused columns from release_track_artist
-- ============================================

ALTER TABLE release_track_artist DROP COLUMN IF EXISTS artist_id;
ALTER TABLE release_track_artist DROP COLUMN IF EXISTS extra;
ALTER TABLE release_track_artist DROP COLUMN IF EXISTS anv;
ALTER TABLE release_track_artist DROP COLUMN IF EXISTS position;
ALTER TABLE release_track_artist DROP COLUMN IF EXISTS join_string;
ALTER TABLE release_track_artist DROP COLUMN IF EXISTS role;

-- ============================================
-- 8. Add FK constraints with CASCADE
-- ============================================

-- Drop existing constraints if they exist (safe to re-run)
ALTER TABLE release_artist DROP CONSTRAINT IF EXISTS fk_release_artist_release;
ALTER TABLE release_track DROP CONSTRAINT IF EXISTS fk_release_track_release;
ALTER TABLE release_track_artist DROP CONSTRAINT IF EXISTS fk_release_track_artist_release;
ALTER TABLE cache_metadata DROP CONSTRAINT IF EXISTS fk_cache_metadata_release;

ALTER TABLE release_artist
    ADD CONSTRAINT fk_release_artist_release
    FOREIGN KEY (release_id) REFERENCES release(id) ON DELETE CASCADE;

ALTER TABLE release_track
    ADD CONSTRAINT fk_release_track_release
    FOREIGN KEY (release_id) REFERENCES release(id) ON DELETE CASCADE;

ALTER TABLE release_track_artist
    ADD CONSTRAINT fk_release_track_artist_release
    FOREIGN KEY (release_id) REFERENCES release(id) ON DELETE CASCADE;

ALTER TABLE cache_metadata
    ADD CONSTRAINT fk_cache_metadata_release
    FOREIGN KEY (release_id) REFERENCES release(id) ON DELETE CASCADE;

-- ============================================
-- 9. Drop serial PK columns from child tables
-- ============================================
-- These tables don't need surrogate keys; they're looked up by release_id.

ALTER TABLE release_artist DROP COLUMN IF EXISTS id;
ALTER TABLE release_track DROP COLUMN IF EXISTS id;
ALTER TABLE release_track_artist DROP COLUMN IF EXISTS id;

-- ============================================
-- 10. Drop indexes for removed tables
-- ============================================

DROP INDEX IF EXISTS idx_release_label_release_id;
DROP INDEX IF EXISTS idx_release_genre_release_id;
DROP INDEX IF EXISTS idx_release_style_release_id;
DROP INDEX IF EXISTS idx_artist_name_trgm;

COMMIT;

-- ============================================
-- 11. Master release deduplication
-- ============================================
-- For releases sharing a master_id, keep the one with the most tracks.
-- FK CASCADE handles child row cleanup automatically.
-- Run this AFTER the main migration, in a separate transaction.

BEGIN;

DELETE FROM release
WHERE id IN (
    SELECT id FROM (
        SELECT r.id, r.master_id,
               ROW_NUMBER() OVER (
                   PARTITION BY r.master_id
                   ORDER BY tc.track_count DESC, r.id ASC
               ) as rn
        FROM release r
        JOIN (
            SELECT release_id, COUNT(*) as track_count
            FROM release_track
            GROUP BY release_id
        ) tc ON tc.release_id = r.id
        WHERE r.master_id IS NOT NULL
    ) ranked
    WHERE rn > 1
);

-- Now drop master_id (no longer needed)
ALTER TABLE release DROP COLUMN IF EXISTS master_id;

COMMIT;

-- ============================================
-- Post-migration: reclaim disk space
-- ============================================
-- Run these separately as they lock tables:
--
-- VACUUM FULL release;
-- VACUUM FULL release_artist;
-- VACUUM FULL release_track;
-- VACUUM FULL release_track_artist;
-- VACUUM FULL cache_metadata;
