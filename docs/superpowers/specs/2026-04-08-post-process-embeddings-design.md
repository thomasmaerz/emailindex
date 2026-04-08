# Post-Process Embeddings Design Spec

**Date:** 2026-04-08
**Topic:** Adding post-process embedding capabilities to `emailindex`.
**Status:** Approved

## Problem Statement
The current system generates embeddings during initial ingestion. If ingestion is run with `--no-embeddings`, or if embeddings need to be refreshed, there is no built-in way to generate them without re-ingesting the files. Semantic search depends on these embeddings.

## Goals
- Add a way to backfill missing embeddings for already ingested emails.
- Add a way to completely replace (re-embed) all embeddings for ingested emails.
- Minimize database re-reads and file system access by using already stored subject and body content.

## Proposed Changes

### 1. `ingest.py` CLI Updates
- **`--backfill-embeddings`**: A new flag to trigger a backfill of missing embeddings (where `embedding IS NULL`).
- **`--re-embed`**: A new flag to clear all existing embeddings and re-generate them for all records.

### 2. Implementation Logic
A new function `manage_embeddings(mode)` will be added to `ingest.py`.

#### Modes:
- **`missing` (triggered by `--backfill-embeddings`)**:
    - Selects records from `emails` table where `embedding IS NULL`.
- **`all` (triggered by `--re-embed`)**:
    - Clears all `embedding` values in the `emails` table.
    - Deletes all entries in the `email_vectors` virtual table.
    - Selects all records from the `emails` table.

#### Processing:
- Uses the `Embedder` class for batch encoding.
- Generates embedding text using the existing `generate_embedding_text(record)` function.
- Updates both the `emails` table and the `email_vectors` table in batches.
- Commits transactions at regular intervals (e.g., every 100 records) to provide a form of progress checkpointing.

## Success Criteria
- Running `python3 ingest.py --backfill-embeddings` successfully generates vectors for records that lack them.
- Running `python3 ingest.py --re-embed` results in a fully refreshed `email_vectors` table and `embedding` column.
- Semantic search (e.g., via `query_email_database` tool) works as expected after these operations.
