# Parallel Ingestion Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add parallel processing to the email ingestion pipeline in ingest.py using ThreadPoolExecutor with configurable concurrency, enabling multi-core utilization for parsing while batching embeddings and writes.

**Architecture:** Use ThreadPoolExecutor with a bounded work queue. Parse workers (configurable 2-4 default) each get their own DB connection for dedup checks, opened/closed per-file to avoid connection leaks. A dedicated embedding thread batches texts and encodes. A single DB writer commits in batches. Enable SQLite WAL mode once at init for concurrent reads.

**Tech Stack:** ThreadPoolExecutor, queue.Queue, sqlite3 with WAL mode, sentence-transformers

---

## File Structure

- Modify: `ingest.py` - Add CLI arg for --concurrent-limit, refactor ingest_emails() to use thread pool, add per-worker connection management
- Modify: `mcp_server/config.py` - Add DEFAULT_CONCURRENT_LIMIT constant
- Test: Existing validation suite + manual test with small maildir

---

### Task 1: Add CLI argument and config constant

**Files:**
- Modify: `mcp_server/config.py` - Add DEFAULT_CONCURRENT_LIMIT = 4
- Modify: `ingest.py:1104-1117` - Add --concurrent-limit argument to main()

- [ ] **Step 1: Add config constant**

In `mcp_server/config.py` after line 19 (ZSTD_COMPRESSION_LEVEL):

```python
DEFAULT_CONCURRENT_LIMIT = 4
```

- [ ] **Step 2: Add CLI argument**

In `ingest.py` before `args = parser.parse_args()` (around line 1109):

```python
parser.add_argument(
    "--concurrent-limit",
    type=int,
    default=4,
    help=f"Number of parallel workers (default: 4)"
)
```

- [ ] **Step 3: Pass to ingest_emails()**

Modify the call at line 1117:
```python
ingest_emails(args.maildir, resume=not args.no_resume, concurrent_limit=args.concurrent_limit)
```

And update function signature at line 980:
```python
def ingest_emails(maildir_path: Path, resume: bool = True, concurrent_limit: int = 4):
```

---

### Task 2: Enable SQLite WAL mode at init

**Files:**
- Modify: `ingest.py:565-663` - Add WAL PRAGMA in init_database()

- [ ] **Step 1: Add WAL mode to init_database**

After `conn = sqlite3.connect(db_path)` at line 568, add:

```python
cursor.execute("PRAGMA journal_mode=WAL")
```

Add a logger info line after:
```python
logger.info("SQLite WAL mode enabled")
```

---

### Task 3: Add per-worker connection helper

**Files:**
- Modify: `ingest.py:902-911` - Update get_db_connection() with context manager pattern

- [ ] **Step 1: Add context manager for worker connections**

Replace the `get_db_connection()` function with:

```python
from contextlib import contextmanager

@contextmanager
def get_db_connection(db_path: Path):
    """Get a DB connection. Usage: 'with get_db_connection(DB_PATH) as conn:'"""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception:
        pass
    try:
        yield conn
    finally:
        conn.close()
```

---

### Task 4: Refactor ingest_emails() with ThreadPoolExecutor

**Files:**
- Modify: `ingest.py:980-1094` - Replace sequential loop with parallel pipeline

- [ ] **Step 1: Add imports at top of file**

After line 17 (import logging):
```python
import threading
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor, as_completed
```

- [ ] **Step 2: Replace ingest_emails() function body**

Replace the entire `ingest_emails()` function (lines 980-1094) with:

```python
def ingest_emails(maildir_path: Path, resume: bool = True, concurrent_limit: int = 4):
    init_database(DB_PATH)
    
    checkpoint = load_checkpoint()
    
    email_files = collect_email_files(maildir_path)
    total_files = len(email_files)
    
    logger.info(f"Found {total_files} email files")
    
    start_count = checkpoint.get('processed_count', 0)
    if resume and start_count > 0:
        last_path = checkpoint.get('last_processed_path')
        if last_path:
            for i, (path, folder) in enumerate(email_files):
                if str(path) == last_path:
                    email_files = email_files[i + 1:]
                    logger.info(f"Resuming from file {i + 1}/{total_files}")
                    break
            else:
                logger.warning("Last processed file not found, starting fresh")
    else:
        logger.info("Starting fresh ingestion")
    
    if not email_files:
        logger.info("No emails to process")
        return
    
    embedder = Embedder()
    
    parse_queue = Queue(maxsize=concurrent_limit * 2)
    embed_queue = Queue(maxsize=concurrent_limit * 2)
    result_lock = threading.Lock()
    
    processed_count = checkpoint.get('processed_count', 0)
    last_processed_path = checkpoint.get('last_processed_path')
    
    def parse_worker(eml_path, folder):
        """Worker function: parse email with its own DB connection."""
        with get_db_connection(DB_PATH) as conn:
            records = parse_email_file(eml_path, folder, conn, source_path=str(eml_path))
            return (eml_path, records, conn)
    
    def process_batch():
        """Main processing loop with thread pool."""
        nonlocal processed_count, last_processed_path
        
        pending_records = []
        pending_texts = []
        
        with ThreadPoolExecutor(max_workers=concurrent_limit) as executor:
            future_to_path = {}
            
            for eml_path, folder in email_files:
                future = executor.submit(parse_worker, eml_path, folder)
                future_to_path[future] = eml_path
            
            for future in as_completed(future_to_path):
                eml_path = future_to_path[future]
                try:
                    _, records, conn = future.result()
                    
                    if not records:
                        checkpoint['errors'].append({
                            'file_path': str(eml_path),
                            'error_type': 'ParseError',
                            'error_message': 'Failed to parse email',
                            'timestamp': datetime.now(timezone.utc).isoformat()
                        })
                        continue
                    
                    original_record = records[0]
                    if is_duplicate_message_id(conn, original_record['message_id']):
                        logger.debug(f"Skipping duplicate: {original_record['message_id']}")
                        processed_count += 1
                        last_processed_path = str(eml_path)
                        continue
                    
                    for record in records:
                        text = generate_embedding_text(record)
                        pending_records.append(record)
                        pending_texts.append(text)
                    
                    if len(pending_records) >= EMBEDDING_BATCH_SIZE:
                        embeddings = embedder.encode_batch(pending_texts)
                        
                        for rec, emb in zip(pending_records, embeddings):
                            rec['embedding'] = emb
                            insert_email(conn, rec)
                        
                        conn.commit()
                        
                        for rec in pending_records:
                            processed_count += 1
                            last_processed_path = rec.get('source_path', str(eml_path))
                        
                        logger.info(f"Processed {processed_count}/{total_files} emails (including salvaged quotes)")
                        
                        if processed_count >= MAX_EMAILS:
                            logger.info(f"Reached limit of {MAX_EMAILS} emails, stopping")
                            break
                        
                        pending_records = []
                        pending_texts = []
                    
                    if processed_count > 0 and processed_count % CHECKPOINT_INTERVAL == 0:
                        checkpoint['processed_count'] = processed_count
                        checkpoint['last_processed_path'] = last_processed_path
                        save_checkpoint(checkpoint)
                
                except Exception as e:
                    logger.error(f"Error processing {eml_path}: {e}")
                    checkpoint['errors'].append({
                        'file_path': str(eml_path),
                        'error_type': type(e).__name__,
                        'error_message': str(e),
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    })
                    continue
        
        if pending_records:
            embeddings = embedder.encode_batch(pending_texts)
            
            with get_db_connection(DB_PATH) as conn:
                for rec, emb in zip(pending_records, embeddings):
                    rec['embedding'] = emb
                    insert_email(conn, rec)
                
                conn.commit()
                
                for rec in pending_records:
                    processed_count += 1
                    last_processed_path = rec.get('source_path', '')
        
        checkpoint['processed_count'] = processed_count
        checkpoint['last_processed_path'] = last_processed_path
        checkpoint['completed_at'] = datetime.now(timezone.utc).isoformat()
        save_checkpoint(checkpoint)
        
        logger.info(f"Ingestion complete: {processed_count} emails processed")
        
        errors = checkpoint.get('errors', [])
        if errors:
            error_types = {}
            for e in errors:
                error_types[e['error_type']] = error_types.get(e['error_type'], 0) + 1
            logger.info(f"Total errors: {len(errors)}")
            logger.info(f"Error types: {error_types}")
    
    process_batch()
```

**NOTE:** The above implementation simplifies by keeping embedding in the main thread. For true 3-stage parallelism (parse → embed → write), we'd need queues. The simpler approach achieves parse parallelism while keeping embedding batching straightforward.

---

### Task 5: Test the implementation

**Files:**
- Test: Run existing validation suite
- Test: Manual test with small maildir

- [ ] **Step 1: Run existing tests**

```bash
cd /Users/thomasmaerz/emailindex
python tests/run_all_validations.py
```

Expected: All existing tests pass (parallelism is internal, output should be identical)

- [ ] **Step 2: Run ingest with small maildir sample**

```bash
cd /Users/thomasmaerz/emailindex
python ingest.py /path/to/small/maildir --no-resume --concurrent-limit 2
```

Expected: Completes without errors, processes all emails

- [ ] **Step 3: Verify database integrity**

```bash
python tests/run_all_validations.py --verbose
```

Expected: All validations pass

---

### Task 6: Verify performance improvement (optional)

**Files:**
- Benchmark: Time ingestion with --concurrent-limit 1 vs 4

- [ ] **Step 1: Benchmark sequential vs parallel**

```bash
# Time sequential
time python ingest.py /path/to/maildir --no-resume --concurrent-limit 1

# Time parallel  
time python ingest.py /path/to/maildir --no-resume --concurrent-limit 4
```

Expected: Parallel shows measurable improvement (2-4x faster parse stage)

---

## Acceptance Criteria Validation

| Criterion | Status |
|-----------|--------|
| Configurable concurrency via CLI flag | ✅ Task 1 |
| Default limit (4) appropriate for modest hardware | ✅ Default in Task 1 |
| No regression in correctness (dedup, salvage, checkpoint) | ✅ Tests in Task 5 |
| Measurable throughput improvement | ✅ Task 6 (optional) |

---

## Plan Review Checklist

- [x] Spec coverage: All issue requirements mapped to tasks
- [x] Placeholder scan: No TBD/TODO in code steps
- [x] Type consistency: Function signatures match between definition and call sites

