-- Create Discogs cache database schema
-- Run with: psql -U postgres -f 04-create-database.sql

-- Create database (run as superuser)
-- CREATE DATABASE discogs;

-- Connect to discogs database, then run the rest
-- \c discogs

-- Enable trigram extension for fuzzy text search
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================
-- Core tables (from discogs-xml2db structure)
-- ============================================

-- Releases
CREATE TABLE IF NOT EXISTS release (
    id              integer PRIMARY KEY,
    title           text NOT NULL,
    released        text,
    country         text,
    notes           text,
    data_quality    text,
    master_id       integer,
    status          text
);

-- Artists on releases
CREATE TABLE IF NOT EXISTS release_artist (
    id              serial PRIMARY KEY,
    release_id      integer NOT NULL,
    artist_id       integer,
    artist_name     text NOT NULL,
    extra           integer DEFAULT 0,  -- 0 = main artist, 1 = extra credit
    anv             text,               -- artist name variation
    position        integer,
    join_string     text,
    role            text,
    tracks          text
);

-- Tracks on releases
CREATE TABLE IF NOT EXISTS release_track (
    id              serial PRIMARY KEY,
    release_id      integer NOT NULL,
    sequence        integer NOT NULL,
    position        text,               -- "A1", "B2", etc.
    parent          text,
    title           text NOT NULL,
    duration        text,
    track_id        text
);

-- Artists on specific tracks (for compilations)
CREATE TABLE IF NOT EXISTS release_track_artist (
    id              serial PRIMARY KEY,
    release_id      integer NOT NULL,
    track_sequence  integer NOT NULL,
    artist_id       integer,
    artist_name     text NOT NULL,
    extra           integer DEFAULT 0,
    anv             text,
    position        integer,
    join_string     text,
    role            text
);

-- Labels on releases
CREATE TABLE IF NOT EXISTS release_label (
    id              serial PRIMARY KEY,
    release_id      integer NOT NULL,
    label_name      text,
    catno           text
);

-- Genres and styles
CREATE TABLE IF NOT EXISTS release_genre (
    id              serial PRIMARY KEY,
    release_id      integer NOT NULL,
    genre           text NOT NULL
);

CREATE TABLE IF NOT EXISTS release_style (
    id              serial PRIMARY KEY,
    release_id      integer NOT NULL,
    style           text NOT NULL
);

-- Artist lookup table
CREATE TABLE IF NOT EXISTS artist (
    id              integer PRIMARY KEY,
    name            text NOT NULL,
    realname        text,
    data_quality    text,
    profile         text
);

-- ============================================
-- Cache metadata (for tracking data freshness)
-- ============================================

CREATE TABLE IF NOT EXISTS cache_metadata (
    release_id      integer PRIMARY KEY,
    cached_at       timestamptz NOT NULL DEFAULT now(),
    source          text NOT NULL,  -- 'bulk_import' or 'api_fetch'
    last_validated  timestamptz
);

-- ============================================
-- Indexes
-- ============================================

-- Foreign key indexes
CREATE INDEX IF NOT EXISTS idx_release_artist_release_id ON release_artist(release_id);
CREATE INDEX IF NOT EXISTS idx_release_track_release_id ON release_track(release_id);
CREATE INDEX IF NOT EXISTS idx_release_track_artist_release_id ON release_track_artist(release_id);
CREATE INDEX IF NOT EXISTS idx_release_label_release_id ON release_label(release_id);
CREATE INDEX IF NOT EXISTS idx_release_genre_release_id ON release_genre(release_id);
CREATE INDEX IF NOT EXISTS idx_release_style_release_id ON release_style(release_id);

-- Cache metadata indexes
CREATE INDEX IF NOT EXISTS idx_cache_metadata_cached_at ON cache_metadata(cached_at);
CREATE INDEX IF NOT EXISTS idx_cache_metadata_source ON cache_metadata(source);
