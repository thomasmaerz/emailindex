#!/usr/bin/env python3
"""Compare scalar cosine and vec0 MATCH on the M1 Max benchmark host."""

import argparse
import json
import sqlite3
import statistics
import time
from pathlib import Path

import sqlite_vec


def timed_runs(conn, sql: str, params: tuple, runs: int) -> tuple[list[float], list]:
    timings = []
    rows = []
    for _ in range(runs):
        started = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        timings.append(time.perf_counter() - started)
    return timings, rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--runs", type=int, default=7)
    args = parser.parse_args()

    conn = sqlite3.connect(args.database)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    query_embedding = conn.execute(
        "SELECT embedding FROM email_vectors LIMIT 1"
    ).fetchone()[0]

    queries = {
        "legacy_scalar": (
            """SELECT e.id, vec_distance_cosine(ev.embedding, ?) AS distance
               FROM emails e JOIN email_vectors ev ON e.id = ev.email_id
               ORDER BY distance ASC LIMIT 10""",
            (query_embedding,),
        ),
        "match_cosine": (
            """WITH knn AS (
                   SELECT email_rowid, distance FROM email_vectors_v2
                   WHERE embedding MATCH ? AND k = 10 AND searchable = 1
               )
               SELECT e.id, knn.distance
               FROM knn JOIN emails e ON e.rowid = knn.email_rowid
               ORDER BY knn.distance, e.id""",
            (query_embedding,),
        ),
        "legacy_scalar_date": (
            """SELECT e.id, vec_distance_cosine(ev.embedding, ?) AS distance
               FROM emails e JOIN email_vectors ev ON e.id = ev.email_id
               WHERE (e.source IS NULL OR e.source != 'quoted_reply')
                 AND e.timestamp >= ?
               ORDER BY distance ASC, e.id ASC LIMIT 10""",
            (query_embedding, "2020-01-01"),
        ),
        "match_cosine_date": (
            """WITH knn AS (
                   SELECT email_rowid, distance FROM email_vectors_v2
                   WHERE embedding MATCH ? AND k = 10 AND searchable = 1
                     AND timestamp >= ?
               )
               SELECT e.id, knn.distance
               FROM knn JOIN emails e ON e.rowid = knn.email_rowid
               ORDER BY knn.distance, e.id""",
            (query_embedding, "2020-01-01"),
        ),
    }

    output = {
        "environment": {
            "benchmark_host": "Apple M1 Max",
            "sqlite": conn.execute("SELECT sqlite_version()").fetchone()[0],
            "sqlite_vec": conn.execute("SELECT vec_version()").fetchone()[0],
            "vectors": conn.execute("SELECT COUNT(*) FROM email_vectors_v2").fetchone()[0],
        },
        "queries": {},
    }
    for name, (sql, params) in queries.items():
        timings, rows = timed_runs(conn, sql, params, args.runs)
        output["queries"][name] = {
            "first_seconds": timings[0],
            "warm_median_seconds": statistics.median(timings[1:]),
            "runs_seconds": timings,
            "ids": [row[0] for row in rows],
        }

    output["equivalence"] = {
        "unfiltered_same_order": (
            output["queries"]["legacy_scalar"]["ids"]
            == output["queries"]["match_cosine"]["ids"]
        ),
        "date_filtered_same_order": (
            output["queries"]["legacy_scalar_date"]["ids"]
            == output["queries"]["match_cosine_date"]["ids"]
        ),
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
