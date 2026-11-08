from __future__ import annotations

import config
from db import connect


def upsert_relational_contexts_bulk(rows: list[dict]) -> int:
    """Persist one Scanner run's relational contexts in one SQLite transaction."""
    if not rows:
        return 0
    columns = list(rows[0].keys())
    if any(list(row.keys()) != columns for row in rows):
        raise ValueError("Relational context batch rows must share ordered columns")
    sql = (
        f"INSERT INTO relational_context_snapshots ({','.join(columns)}) "
        f"VALUES ({','.join(['?'] * len(columns))}) "
        f"ON CONFLICT(unique_key) DO UPDATE SET "
        + ",".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"id", "unique_key"}
        )
    )
    conn = connect(config.ALERTS_DB)
    try:
        conn.executemany(sql, [[row[column] for column in columns] for row in rows])
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
