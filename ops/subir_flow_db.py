"""Copia segura (API de backup de SQLite, sin parar el bot) de tmad_flow_live.db, resumen de contenido y gzip.

    python subir_flow_db.py --out <carpeta>
Imprime cuántas filas hay por tabla y fecha para comprobar que la captura de flujo funciona.
"""
import argparse
import gzip
import os
import shutil
import sqlite3
import time

DEFAULT_DB = r"C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick\03_DATOS\data_bot\db\live\tmad_flow_live.db"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if not os.path.exists(a.db):
        raise SystemExit(f"No existe {a.db}: la captura de flujo aún no ha creado la base.")
    os.makedirs(a.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    snap = os.path.join(a.out, f"tmad_flow_{stamp}.db")
    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(snap)
    src.backup(dst)
    src.close()
    for t, col in (("flow_runs", "captured_at_madrid"), ("option_heatmap_snapshots", "session_date"),
                   ("option_heatmap_intraday", "session_date"), ("intraday_bars_1m", "substr(last_ts,1,10)"),
                   ("score_regime_daily", "regime_date")):
        try:
            n = dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            extra = dst.execute(f"SELECT MIN({col}), MAX({col}) FROM {t}").fetchone()
            print(f"{t:28s} {n:>8,} filas  {extra[0]} .. {extra[1]}")
        except sqlite3.Error as e:
            print(f"{t:28s} sin tabla ({e})")
    for r in dst.execute("SELECT kind, status, COUNT(*), MAX(captured_at_madrid), SUM(rows_written) FROM flow_runs GROUP BY kind, status"):
        print("  ejecución:", r)
    dst.close()
    gz = snap + ".gz"
    with open(snap, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1024 * 1024)
    os.remove(snap)
    print(f"FLOW_DB_LISTA {gz} ({os.path.getsize(gz) / 1e6:,.0f} MB)")


if __name__ == "__main__":
    main()
