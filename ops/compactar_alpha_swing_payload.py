"""Compacta los payload_json gigantes de alpha_swing_positions / alpha_swing_events (bug de anidamiento).

Ejecutar SOLO después de desplegar el parche de alpha_swing_engine.py; si no, el bot vuelve a anidarlos.

    python compactar_alpha_swing_payload.py              # simulación: lista las filas afectadas
    python compactar_alpha_swing_payload.py --apply      # sustituye cada payload > umbral por un JSON pequeño

Solo toca la columna payload_json de filas con más de --min-mb MB (por defecto 1). El resto de columnas
(precio, estado, retornos, motivo de salida...) no cambia. El JSON nuevo indica el tamaño original y el motivo.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3

DEFAULT_DB = r"C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick\03_DATOS\alerts_bot\db\live\tmad_alerts_adaptive.db"
TABLES = {
    "alpha_swing_positions": "id, ticker, status",
    "alpha_swing_events": "id, ticker, event_type",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--min-mb", type=float, default=1.0)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    threshold = int(a.min_mb * 1024 * 1024)
    con = sqlite3.connect(a.db, timeout=60, isolation_level=None)
    con.execute("PRAGMA busy_timeout=60000")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    total = 0
    for table, cols in TABLES.items():
        rows = con.execute(
            f"SELECT {cols}, length(CAST(payload_json AS BLOB)) FROM {table} "
            f"WHERE length(CAST(payload_json AS BLOB)) > ? ORDER BY 4 DESC", (threshold,)
        ).fetchall()
        for r in rows:
            print(f"{table:24s} id={r[0]:<5} {r[1]:<6} {r[2]:<12} {r[3]/1e6:9.1f} MB")
            total += r[3]
        if a.apply and rows:
            con.execute("BEGIN IMMEDIATE")
            for r in rows:
                con.execute(
                    f"UPDATE {table} SET payload_json=json_object('compacted', 1, 'compacted_at_utc', ?, "
                    f"'original_bytes', ?, 'reason', 'nested payload_json bug in alpha_swing_engine (fixed)') WHERE id=?",
                    (now, r[3], r[0]),
                )
            con.execute("COMMIT")
            print(f"  -> {len(rows)} filas compactadas en {table}")
    print(f"TOTAL afectado: {total/1e9:.2f} GB" + ("" if a.apply else "  (simulación: usa --apply para compactar)"))
    if a.apply:
        print("checkpoint:", con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
    con.close()


if __name__ == "__main__":
    main()
