from __future__ import annotations

from typing import Iterable

from db_core import gex_conn


def _insert_rows(conn, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    if any(list(row.keys()) != columns for row in rows):
        raise ValueError(f"{table}: GEX batch rows must share ordered columns")
    sql = (
        f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) "
        f"VALUES ({','.join(['?'] * len(columns))})"
    )
    conn.executemany(sql, [[row[column] for column in columns] for row in rows])
    return len(rows)


def persist_gex_bundle(
    summary_row: dict,
    level_rows: Iterable[dict] = (),
    expiration_rows: Iterable[dict] = (),
    bucket_rows: Iterable[dict] = (),
) -> dict[str, int]:
    """Persist one ticker's complete GEX state atomically in one WAL transaction.

    Network fetch and ticker ordering remain outside this helper. A failed child
    insert rolls back the complete ticker bundle, so Alerts can never observe a
    summary whose strikes/expiries belong to a partially committed state.
    """
    levels = list(level_rows)
    expirations = list(expiration_rows)
    buckets = list(bucket_rows)
    conn = gex_conn()
    try:
        conn.execute("BEGIN")
        summary_count = _insert_rows(conn, "gex_summary", [summary_row])
        level_count = _insert_rows(conn, "gex_levels", levels)
        expiration_count = _insert_rows(conn, "gex_expirations", expirations)
        bucket_count = _insert_rows(conn, "gex_expiry_buckets", buckets)
        conn.commit()
        return {
            "summary": summary_count,
            "levels": level_count,
            "expirations": expiration_count,
            "buckets": bucket_count,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
