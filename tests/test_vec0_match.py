import sqlite3
import struct

import sqlite_vec

from mcp_server.vector_index import VECTOR_INDEX_DDL, VECTOR_TABLE


def _embedding(*values: float) -> bytes:
    padded = list(values) + [0.0] * (384 - len(values))
    return struct.pack("384f", *padded)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute(VECTOR_INDEX_DDL)
    return conn


def test_cosine_match_preserves_cosine_ranking_not_l2_ranking():
    conn = _connection()
    try:
        conn.execute(
            f"INSERT INTO {VECTOR_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, _embedding(10.0, 0.0), 1, "2024", "a", 0, 0),
        )
        conn.execute(
            f"INSERT INTO {VECTOR_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
            (2, _embedding(0.9, 0.1), 1, "2024", "b", 0, 0),
        )

        rows = conn.execute(
            f"SELECT email_rowid FROM {VECTOR_TABLE} WHERE embedding MATCH ? AND k = 2",
            (_embedding(1.0, 0.0),),
        ).fetchall()
        assert [row[0] for row in rows] == [1, 2]
    finally:
        conn.close()


def test_match_filters_before_selecting_top_k():
    conn = _connection()
    try:
        conn.execute(
            f"INSERT INTO {VECTOR_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, _embedding(1.0, 0.0), 0, "2024", "a", 0, 0),
        )
        conn.execute(
            f"INSERT INTO {VECTOR_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
            (2, _embedding(0.8, 0.2), 1, "2024", "b", 0, 0),
        )

        rows = conn.execute(
            f"""
            SELECT email_rowid FROM {VECTOR_TABLE}
            WHERE embedding MATCH ? AND k = 1 AND searchable = 1
            """,
            (_embedding(1.0, 0.0),),
        ).fetchall()
        assert rows == [(2,)]
    finally:
        conn.close()
