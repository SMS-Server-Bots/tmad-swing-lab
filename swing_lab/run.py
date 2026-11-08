r"""CLI del laboratorio de swings: lista EOD, seguimiento de posiciones y marcador de promoción.

Uso (desde la raíz del repo):
  python -m swing_lab.run fetch   --archives data/                      # descarga ZIP de Releases (gh)
  python -m swing_lab.run list    --archives data/ [--live-db X] [--date YYYY-MM-DD]
  python -m swing_lab.run track   --archives data/ [--live-db X]
  python -m swing_lab.run backfill --archives data/ --start 2026-08-20 --end 2026-10-30
  En el VPS:  python -m swing_lab.run list --data-root C:\...\03_DATOS   (lee ZIP + base viva en solo lectura)

``list`` añade al registro ``ledger/picks.csv`` las selecciones de cada política (modo PROSPECTIVE si se
genera antes de la apertura de la sesión de entrada; HISTORICAL si es un backfill).
``track`` recalcula todas las posiciones con los precios disponibles y escribe el marcador.
Solo las selecciones PROSPECTIVE cuentan para la promoción.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os

import numpy as np
import pandas as pd

from . import data as D
from . import policy as P
from .features import add_features, universe

LEDGER = "ledger"
OUT = "out"
PROMOTION_MIN_SESSIONS = 40
PROMOTION_MIN_T = 2.0


def _load(args):
    if args.data_root:   # estructura del VPS: 03_DATOS/data_bot/db/{archive,live}
        base = os.path.join(args.data_root, "data_bot", "db")
        weekly, intraday = os.path.join(base, "archive"), os.path.join(base, "archive", "intraday")
        live = args.live_db or os.path.join(base, "live", "tmad_scanner_live.db")
    else:
        weekly, intraday, live = os.path.join(args.archives, "weekly"), os.path.join(args.archives, "intraday"), args.live_db
    eod = D.load_eod(weekly, live)
    intra = D.load_intraday(intraday, live)
    px = D.daily_prices(eod, intra)
    feats = add_features(eod, px[["ticker", "date", "i_hi", "i_lo"]])
    return feats, px


def make_picks(feats: pd.DataFrame, date: str, mode: str) -> pd.DataFrame:
    day = feats[(feats.trade_date == date) & universe(feats)].copy()
    if len(day) < 100:
        raise SystemExit(f"Universo insuficiente para {date}: {len(day)} tickers")
    rows = []
    for pol in P.POLICIES:
        sc = P.score(day, pol)
        order = day.assign(sc=sc).sort_values(["sc", "ticker"])
        for side, sel in (("LONG", order.tail(P.TOP_N)[::-1]), ("SHORT", order.head(P.TOP_N))):
            for rank, (_, r) in enumerate(sel.iterrows(), 1):
                v = float(np.clip((r.vol10 if pd.notna(r.vol10) else 4.0) / 100, 0.01, 0.15))
                rows.append(dict(signal_date=date, policy=pol, side=side, rank=rank, ticker=r.ticker,
                                 signal_close=r.current_price, score=round(float(r.sc), 4), vol10=round(v, 4),
                                 change_pct=r.change_percent, put_wall_pct=r.put_wall_pct,
                                 mode=mode, policy_fp=P.policy_fingerprint(),
                                 generated_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    return pd.DataFrame(rows)


def append_ledger(p: pd.DataFrame) -> int:
    os.makedirs(LEDGER, exist_ok=True)
    fn = os.path.join(LEDGER, "picks.csv")
    old = pd.read_csv(fn) if os.path.exists(fn) else pd.DataFrame(columns=p.columns)
    key = ["signal_date", "policy", "side", "rank"]
    new = p.merge(old[key], on=key, how="left", indicator=True)
    new = new[new._merge == "left_only"].drop(columns="_merge")
    pd.concat([old, new], ignore_index=True).to_csv(fn, index=False)
    return len(new)


def simulate(pk: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """Gestión de cada posición: entrada 09:35 ET del día siguiente, stop protector, salida por tiempo."""
    cal = D.sessions(pk.signal_date.min(), max(px.date.max(), pk.signal_date.max()))
    si = {d: i for i, d in enumerate(cal)}
    pxi = px.set_index(["ticker", "date"])
    out = []
    for _, r in pk.iterrows():
        sg = 1 if r.side == "LONG" else -1
        i0 = si.get(r.signal_date)
        rec = dict(r); rec.update(status="PENDING_ENTRY", entry=np.nan, exit=np.nan, exit_reason="", days_held=0,
                                 ret_net=np.nan, last_px=np.nan, stop=np.nan)
        if i0 is None or i0 + 1 >= len(cal):
            out.append(rec); continue
        d1 = cal[i0 + 1]
        if (r.ticker, d1) not in pxi.index or pd.isna(pxi.loc[(r.ticker, d1), "open_px"]):
            rec["status"] = "PENDING_ENTRY" if d1 > px.date.max() else "NO_ENTRY_DATA"
            out.append(rec); continue
        e = float(pxi.loc[(r.ticker, d1), "open_px"]); v = float(r.vol10)
        stop = e * (1 - sg * P.STOP_VOL_MULT * v)
        rec.update(entry=e, stop=round(stop, 4), status="HOLD")
        for j in range(1, P.MAX_HOLD + 1):
            if i0 + j >= len(cal):
                break
            d = cal[i0 + j]
            if (r.ticker, d) not in pxi.index or pd.isna(pxi.loc[(r.ticker, d), "close"]):
                if d <= px.date.max():
                    rec.update(status="DATA_GAP")
                break
            row = pxi.loc[(r.ticker, d)]
            adv = row.lo if sg == 1 else row.hi
            rec.update(days_held=j, last_px=float(row.close))
            if (sg == 1 and adv <= stop) or (sg == -1 and adv >= stop):
                fill = adv if abs(adv / stop - 1) > 0.5 * v else stop      # hueco a través del stop
                rec.update(status="CLOSED", exit=fill, exit_reason="STOP", ret_net=sg * (fill / e - 1) - P.COST_ROUNDTRIP)
                break
            if j >= P.PRIMARY_HOLD:
                rec.update(status="CLOSED", exit=float(row.close), exit_reason=f"TIME_D{j}",
                           ret_net=sg * (row.close / e - 1) - P.COST_ROUNDTRIP)
                break
            if sg * (row.close / e - 1) <= -P.WATCH_VOL_MULT * v:
                rec["status"] = "WATCH"
        if rec["status"] in ("HOLD", "WATCH") and pd.notna(rec["last_px"]):
            rec["ret_open"] = sg * (rec["last_px"] / e - 1)
        out.append(rec)
    return pd.DataFrame(out)


def universe_returns(feats: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """Retorno medio del universo operable para la misma ventana (base para el alfa)."""
    s = D.attach_paths(feats, px, P.PRIMARY_HOLD)
    u = s[universe(s)]
    return u.groupby("trade_date")[f"r{P.PRIMARY_HOLD}"].mean().rename("univ_ret").reset_index().rename(
        columns={"trade_date": "signal_date"})


def scoreboard(pos: pd.DataFrame, uni: pd.DataFrame) -> str:
    c = pos[pos.status == "CLOSED"].merge(uni, on="signal_date", how="left")
    c["alpha"] = np.where(c.side == "LONG", c.ret_net + P.COST_ROUNDTRIP - c.univ_ret,
                          c.ret_net + P.COST_ROUNDTRIP + c.univ_ret)
    lines = [f"# Marcador TMAD Swing Lab — políticas `{P.policy_fingerprint()}`", "",
             f"Promoción: ≥{PROMOTION_MIN_SESSIONS} sesiones PROSPECTIVE cerradas, alfa vs universo con t ≥ "
             f"{PROMOTION_MIN_T} y mejor que C5050 y RANDOM en el mismo lado.", ""]
    for mode in ("PROSPECTIVE", "HISTORICAL"):
        m = c[c["mode"] == mode]
        lines += [f"## {mode}", "", "| Política | Lado | Sesiones | Ret. neto medio | Alfa medio | t alfa | % ganadoras | Stops |",
                  "|---|---|---:|---:|---:|---:|---:|---:|"]
        if m.empty:
            lines += ["| — | — | 0 | — | — | — | — | — |", ""]; continue
        for (pol, side), g in m.groupby(["policy", "side"]):
            d = g.groupby("signal_date").agg(ret=("ret_net", "mean"), alpha=("alpha", "mean"))
            n = len(d); t = d.alpha.mean() / d.alpha.std() * np.sqrt(n) / np.sqrt(P.PRIMARY_HOLD) if n > 2 else np.nan
            lines.append(f"| {pol} | {side} | {n} | {d.ret.mean()*100:+.2f} % | {d.alpha.mean()*100:+.2f} % | {t:+.2f} | "
                         f"{(g.ret_net > 0).mean()*100:.0f} % | {(g.exit_reason == 'STOP').mean()*100:.0f} % |")
        lines.append("")
    return "\n".join(lines)


def list_markdown(p: pd.DataFrame, date: str) -> str:
    v1 = p[p.policy == "PAPER_V1_REV"]
    lines = [f"# Lista EOD de swings — señal {date} (modo PAPEL)", "",
             f"Entrada: apertura de la siguiente sesión (primer precio ≤ 10:45 ET). Salida por tiempo: cierre del día "
             f"{P.PRIMARY_HOLD}. Stop protector: {P.STOP_VOL_MULT}× volatilidad diaria. Política `PAPER_V1_REV` "
             f"(`{P.policy_fingerprint()}`), **sin ventaja demostrada**: no operar con dinero real.", ""]
    for side in ("LONG", "SHORT"):
        lines += [f"## {side}", "", "| # | Ticker | Cierre | Var. día | Vol. diaria | Stop orientativo |", "|---:|---|---:|---:|---:|---:|"]
        sg = 1 if side == "LONG" else -1
        for _, r in v1[v1.side == side].iterrows():
            lines.append(f"| {r['rank']} | {r.ticker} | {r.signal_close:.2f} | {r.change_pct:+.2f} % | {r.vol10*100:.1f} % | "
                         f"{r.signal_close*(1-sg*P.STOP_VOL_MULT*r.vol10):.2f} |")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "list", "track", "backfill"])
    ap.add_argument("--archives", default="data")
    ap.add_argument("--live-db", default=None)
    ap.add_argument("--data-root", default=None, help="Ruta a 03_DATOS en el VPS (lectura)")
    ap.add_argument("--date"); ap.add_argument("--start"); ap.add_argument("--end")
    a = ap.parse_args()
    if a.cmd == "fetch":
        D.fetch_archives(a.archives); return
    feats, px = _load(a)
    os.makedirs(OUT, exist_ok=True)
    if a.cmd in ("list", "backfill"):
        dates = sorted(feats.trade_date.unique())
        todo = [a.date or dates[-1]] if a.cmd == "list" else [d for d in dates if a.start <= d <= a.end]
        for d in todo:
            mode = "PROSPECTIVE" if a.cmd == "list" and d == dates[-1] and px.date.max() <= d else "HISTORICAL"
            p = make_picks(feats, d, mode)
            n = append_ledger(p)
            if a.cmd == "list":
                open(os.path.join(OUT, f"eod_list_{d}.md"), "w").write(list_markdown(p, d))
                print(f"{d}: {n} selecciones nuevas ({mode}) -> {OUT}/eod_list_{d}.md")
        if a.cmd == "backfill":
            print(f"backfill {len(todo)} sesiones")
    if a.cmd in ("track", "backfill"):
        pk = pd.read_csv(os.path.join(LEDGER, "picks.csv"))
        pos = simulate(pk, px)
        pos.to_csv(os.path.join(LEDGER, "positions.csv"), index=False)
        sb = scoreboard(pos, universe_returns(feats, px))
        open(os.path.join(OUT, "scoreboard.md"), "w").write(sb)
        print(sb)


if __name__ == "__main__":
    main()
