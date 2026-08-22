"""Canonical sqlite-vec index schema and maintenance helpers."""

import re
import sqlite3


VECTOR_TABLE = "email_vectors_v2"
VECTOR_DIMENSIONS = 384
VECTOR_BYTES = VECTOR_DIMENSIONS * 4
SUPPORTED_VEC_VERSION = (0, 1, 9)

VECTOR_INDEX_DDL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {VECTOR_TABLE} USING vec0(
    email_rowid INTEGER PRIMARY KEY,
    embedding FLOAT[{VECTOR_DIMENSIONS}] distance_metric=cosine,
    searchable BOOLEAN,
    timestamp TEXT,
    from_address TEXT,
    is_outbound BOOLEAN,
    has_attachments BOOLEAN
)
"""


def load_sqlite_vec(conn: sqlite3.Connection) -> str:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    return conn.execute("SELECT vec_version()").fetchone()[0]


def parse_vec_version(version: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise RuntimeError(f"Unrecognized sqlite-vec version: {version}")
    return tuple(int(part) for part in match.groups())


def validate_vec_version(version: str) -> None:
    parsed = parse_vec_version(version)
    if parsed != SUPPORTED_VEC_VERSION:
        expected = ".".join(str(part) for part in SUPPORTED_VEC_VERSION)
        raise RuntimeError(
            f"Unsupported sqlite-vec version {version}; expected v{expected}"
        )


def create_vector_index(conn: sqlite3.Connection) -> None:
    conn.execute(VECTOR_INDEX_DDL)


def vector_index_schema(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (VECTOR_TABLE,),
    ).fetchone()
    return row[0] if row else None


def validate_vector_index(conn: sqlite3.Connection) -> None:
    schema = vector_index_schema(conn)
    normalized = " ".join((schema or "").lower().split())
    required = (
        f"using vec0",
        "email_rowid integer primary key",
        f"embedding float[{VECTOR_DIMENSIONS}] distance_metric=cosine",
        "searchable boolean",
        "timestamp text",
        "from_address text",
        "is_outbound boolean",
        "has_attachments boolean",
    )
    missing = [item for item in required if item not in normalized]
    if missing:
        raise RuntimeError(
            f"{VECTOR_TABLE} is missing the required cosine schema: {', '.join(missing)}"
        )

    conn.execute(
        f"SELECT email_rowid FROM {VECTOR_TABLE} "
        "WHERE embedding MATCH ? AND k = 0",
        (bytes(VECTOR_BYTES),),
    ).fetchall()


def searchable_value(source: str | None) -> int:
    return 0 if source == "quoted_reply" else 1


def replace_vector(
    conn: sqlite3.Connection,
    *,
    email_rowid: int,
    email_id: str,
    embedding: bytes,
    source: str | None,
    timestamp: str,
    from_address: str,
    is_outbound: int | bool | None,
    has_attachments: int | bool | None,
) -> None:
    if len(embedding) != VECTOR_BYTES:
        raise ValueError(
            f"Embedding for {email_id} is {len(embedding)} bytes; expected {VECTOR_BYTES}"
        )

    conn.execute(f"DELETE FROM {VECTOR_TABLE} WHERE email_rowid = ?", (email_rowid,))
    conn.execute(
        f"""
        INSERT INTO {VECTOR_TABLE} (
            email_rowid, embedding, searchable, timestamp, from_address,
            is_outbound, has_attachments
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email_rowid,
            embedding,
            searchable_value(source),
            timestamp,
            from_address,
            1 if is_outbound else 0,
            1 if has_attachments else 0,
        ),
    )
