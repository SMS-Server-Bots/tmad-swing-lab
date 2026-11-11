"""MOVE_TREND_V1 — «detectar el movimiento, entrar a favor del mercado y aguantarlo lo que dure» (PAPEL).

Regla congelada el 11/11/2026, antes de verla en vivo (ver REPORT_20261109.md, «Posicionarse en el movimiento»):

* Candidatos (cierre del día t): acciones del universo operable en el decil superior de ATR10 % (rango
  máximo-mínimo medio de 10 sesiones; equivale al modelo de movimiento, ρ 0,92). Se excluye ATR > 25 %.
* Lado: el del mercado. SPY por encima de su media de 20 sesiones → solo largos; por debajo → solo cortos.
* Orden: stop de entrada en el máximo de t (largos) o el mínimo de t (cortos), válida 2 sesiones.
  Se cancela si antes se rompe el extremo contrario (OCO).
* Stop inicial: el extremo contrario de t. Stop dinámico: 2 × ATR10 desde el mejor precio alcanzado.
* Salida: al tocar el stop, o al cierre de la 10.ª sesión. Coste 0,2 % ida y vuelta.
* Simulación con la secuencia real de snapshots intradía (resuelve el orden de los toques dentro del día).

El marcador mide TODO el decil (señal completa); la lista muestra las 10 de más GEX absoluto (más líquidas en
opciones) para poder seguirlas a mano.

Expectativa honesta al congelarla: en 18 meses con barras diarias el resultado va de 0 a +1,5 % por operación
según cómo se resuelva el orden de los toques dentro del día. Con la secuencia intradía real del laboratorio
(ago–oct 2026, 43 días de señal, 1.144 operaciones cerradas) da −1,24 % por operación (t −0,49), con un 27 % de
ganadoras y un 11 % por encima de +10 %. Se sigue en papel para medirla en vivo; no hay ventaja demostrada.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

POLICY = "MOVE_TREND_V1"
FREEZE_DATE = "2026-11-11"
ATR_N = 10
ATR_MAX = 25.0
TOP_DECILE = 0.90
SMA_N = 20
VALID = 2
K_TRAIL = 2.0
MAX_HOLD = 10
COST = 0.002
LIST_N = 10


def _wide(px: pd.DataFrame, col: str) -> pd.DataFrame:
    return px.pivot_table(index="date", columns="ticker", values=col, aggfunc="last").sort_index()


def signals(feats: pd.DataFrame, px: pd.DataFrame, date: str, universe_mask: pd.Series) -> tuple[pd.DataFrame, dict]:
    """Órdenes para la sesión siguiente a ``date``."""
    hi, lo, cl = _wide(px, "hi"), _wide(px, "lo"), _wide(px, "close")
    if date not in cl.index:
        return pd.DataFrame(), {"error": f"sin precios para {date}"}
    atr = ((hi - lo) / cl * 100).rolling(ATR_N, min_periods=max(5, ATR_N // 2)).mean()
    spy = cl.get("SPY")
    if spy is None:
        return pd.DataFrame(), {"error": "sin SPY"}
    sma = spy.rolling(SMA_N, min_periods=SMA_N).mean()
    if pd.isna(sma.get(date)):
        return pd.DataFrame(), {"error": "SPY sin 20 sesiones de historia"}
    up = bool(spy[date] > sma[date])
    day = feats[(feats.trade_date == date) & universe_mask].copy()
    day["atr"] = day.ticker.map(atr.loc[date])
    day["hi"] = day.ticker.map(hi.loc[date]); day["lo"] = day.ticker.map(lo.loc[date])
    day = day[day.atr.notna() & (day.atr <= ATR_MAX) & (day.hi > day.lo) & (day.lo > 0)]
    if len(day) < 100:
        return pd.DataFrame(), {"error": f"universo insuficiente ({len(day)})"}
    cut = day.atr.quantile(TOP_DECILE)
    sel = day[day.atr >= cut].copy()
    sel["side"] = "LONG" if up else "SHORT"
    sel["trigger"] = np.where(up, sel.hi, sel.lo)
    sel["stop0"] = np.where(up, sel.lo, sel.hi)
    sel["liq"] = pd.to_numeric(sel.get("total_gex"), errors="coerce").abs().fillna(0)
    sel = sel.sort_values(["liq", "ticker"], ascending=[False, True])
    sel["in_list"] = np.arange(len(sel)) < LIST_N
    out = sel[["ticker", "side", "trigger", "stop0", "atr", "current_price", "in_list"]].rename(columns={"current_price": "signal_close"})
    out.insert(0, "signal_date", date); out.insert(1, "policy", POLICY)
    ctx = {"spy": float(spy[date]), "sma20": float(sma[date]), "up": up, "cut_atr": float(cut), "n": int(len(sel))}
    return out.reset_index(drop=True), ctx


def _path(intra_by: dict, px_by: dict, ticker: str, dates: list[str]):
    """Secuencia de precios (fecha, precio) con snapshots intradía y el cierre de cada sesión al final."""
    seq = []
    for d in dates:
        snaps = intra_by.get((ticker, d))
        if snaps is not None:
            seq += [(d, float(p)) for p in snaps]
        c = px_by.get((ticker, d))
        if c is not None and np.isfinite(c):
            seq.append((d, float(c)))
    return seq


def simulate(sig: pd.DataFrame, px: pd.DataFrame, intra: pd.DataFrame, calendar: list[str]) -> pd.DataFrame:
    """Recalcula todas las señales con los precios disponibles. Estados: PENDING, CANCELLED, EXPIRED, OPEN, CLOSED."""
    if sig.empty:
        return sig
    ib = intra.sort_values("mins").groupby(["ticker", "date"]).current_price.apply(list).to_dict()
    pb = px.set_index(["ticker", "date"]).close.to_dict()
    have = set(px.date.dropna())
    pos = {d: i for i, d in enumerate(calendar)}
    rows = []
    for r in sig.itertuples(index=False):
        i = pos.get(r.signal_date)
        rec = r._asdict()
        if i is None:
            rec.update(status="DATA_GAP"); rows.append(rec); continue
        later = [d for d in calendar[i + 1:i + 1 + MAX_HOLD + VALID] if d in have]
        long = r.side == "LONG"
        entry = entry_date = None; stop = r.stop0; best = None; status = "PENDING"; exit_px = exit_date = None; days = 0
        for d in later:
            seq = _path(ib, pb, r.ticker, [d])
            if not seq:
                continue
            if entry is None:
                k = calendar.index(d) - i
                if k > VALID:
                    status = "EXPIRED"; break
                for j, (_, p) in enumerate(seq):
                    if (long and p <= r.stop0) or (not long and p >= r.stop0):
                        status = "CANCELLED"; break
                    if (long and p >= r.trigger) or (not long and p <= r.trigger):
                        # orden stop: se ejecuta en el disparo salvo si la sesión ya abre más allá (hueco)
                        entry = p if j == 0 else r.trigger
                        entry_date = d; best = p; status = "OPEN"; break
                if status == "CANCELLED":
                    break
                if entry is None:
                    continue
                # resto de la sesión de entrada tras la ejecución
                after = seq[[j for j, (_, p) in enumerate(seq) if ((long and p >= r.trigger) or (not long and p <= r.trigger))][0] + 1:]
            else:
                after = seq
            first_of_day = entry_date != d
            for j, (_, p) in enumerate(after):
                if (long and p <= stop) or (not long and p >= stop):
                    # stop: se ejecuta al nivel del stop salvo hueco de apertura (primer precio del día ya más allá)
                    exit_px = p if (first_of_day and j == 0) else stop
                    exit_date, status = d, "CLOSED"; break
                best = max(best, p) if long else min(best, p)
                stop = max(stop, best * (1 - K_TRAIL * r.atr / 100)) if long else min(stop, best * (1 + K_TRAIL * r.atr / 100))
            if status == "CLOSED":
                break
            days = calendar.index(d) - calendar.index(entry_date) + 1
            if days >= MAX_HOLD:
                exit_px, exit_date, status = seq[-1][1], d, "CLOSED"; break
        last_px = None
        if status == "OPEN":
            lp = [pb.get((r.ticker, d)) for d in later if pb.get((r.ticker, d)) is not None]
            last_px = lp[-1] if lp else None
        ref = exit_px if exit_px is not None else last_px
        ret = None
        if entry is not None and ref is not None:
            ret = (ref / entry - 1) if long else (1 - ref / entry)
            if status == "CLOSED":
                ret -= COST
        rec.update(status=status, entry=entry, entry_date=entry_date, stop_now=stop if entry is not None else r.stop0,
                   exit_px=exit_px, exit_date=exit_date, ret=ret)
        rows.append(rec)
    return pd.DataFrame(rows)


def scoreboard(t: pd.DataFrame) -> str:
    if t.empty:
        return f"## {POLICY}\n\nSin señales todavía."
    pro = t[t["mode"] == "PROSPECTIVE"] if "mode" in t.columns else t
    lines = [f"## {POLICY} — movimiento a favor del mercado (PAPEL, congelada {FREEZE_DATE})", ""]
    for lab, sub in (("Decil completo (prospectivo)", pro), ("Lista de 10 (prospectivo)", pro[pro.in_list]), ("Todo, incluido histórico", t)):
        c = sub[sub.status == "CLOSED"]
        n_sig = sub.signal_date.nunique()
        if len(c):
            by = c.groupby("signal_date").ret.mean()
            tt = by.mean() / by.std() * np.sqrt(len(by)) / np.sqrt(MAX_HOLD / 2) if len(by) > 2 and by.std() > 0 else float("nan")
            lines.append(f"- {lab}: {n_sig} días de señal, {len(c)} cerradas, abiertas {int((sub.status == 'OPEN').sum())}, "
                         f"canceladas {int((sub.status == 'CANCELLED').sum())}, sin ejecutar {int((sub.status == 'EXPIRED').sum())}. "
                         f"Media {c.ret.mean() * 100:+.2f} %, mediana {c.ret.median() * 100:+.2f} %, ganadoras {np.mean(c.ret > 0) * 100:.0f} %, "
                         f"> +10 % {np.mean(c.ret > 0.10) * 100:.0f} %, t ajustado {tt:+.2f}.")
        else:
            lines.append(f"- {lab}: {n_sig} días de señal, sin operaciones cerradas todavía.")
    lines += ["", "Criterio para pasar a Discord: ≥ 30 días de señal prospectivos, media > +0,5 % y t ≥ 2 en el decil completo."]
    return "\n".join(lines)


def markdown(orders: pd.DataFrame, ctx: dict, track: pd.DataFrame, date: str) -> str:
    lines = [f"## {POLICY} — órdenes para la sesión siguiente a {date} (PAPEL)", ""]
    if orders.empty:
        return "\n".join(lines + [f"Sin órdenes: {ctx.get('error', '')}"])
    side = "LARGOS" if ctx["up"] else "CORTOS"
    lines += [f"Mercado: SPY {ctx['spy']:.2f} {'>' if ctx['up'] else '<'} media 20 ({ctx['sma20']:.2f}) → solo **{side}**. "
              f"Decil de ATR ≥ {ctx['cut_atr']:.1f} %: {ctx['n']} candidatos (todos cuentan en el marcador).", "",
              "| Ticker | Lado | Entrar si rompe | Cancelar si toca | Stop inicial | Stop dinámico | ATR |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for r in orders[orders.in_list].itertuples():
        lines.append(f"| {r.ticker} | {r.side} | {r.trigger:.2f} | {r.stop0:.2f} | {r.stop0:.2f} | {K_TRAIL:.0f}×ATR = {K_TRAIL * r.atr:.1f} % | {r.atr:.1f} % |")
    lines += ["", "Orden válida 2 sesiones. Salida: stop dinámico o cierre de la 10.ª sesión."]
    if track is not None and len(track):
        op = track[(track.status == "OPEN") & track.in_list]
        if len(op):
            lines += ["", "### Posiciones abiertas de la lista (stop actualizado)", "",
                      "| Señal | Ticker | Lado | Entrada | Último | Resultado | Stop para mañana |", "|---|---|---|---:|---:|---:|---:|"]
            for r in op.sort_values("signal_date").itertuples():
                last = r.entry * (1 + r.ret) if r.side == "LONG" else r.entry * (1 - r.ret)
                lines.append(f"| {r.signal_date} | {r.ticker} | {r.side} | {r.entry:.2f} | {last:.2f} | {r.ret * 100:+.1f} % | {r.stop_now:.2f} |")
        cl = track[(track.status == "CLOSED") & track.in_list & (track.exit_date == track.exit_date.max())]
        if len(cl):
            lines += ["", "Cerradas en la última sesión: " + ", ".join(f"{r.ticker} {r.ret * 100:+.1f} %" for r in cl.itertuples())]
    return "\n".join(lines)


def append_signals(path: str, orders: pd.DataFrame, mode: str) -> int:
    if orders.empty:
        return 0
    o = orders.assign(mode=mode)
    if os.path.exists(path):
        old = pd.read_csv(path)
        o = o[~o.set_index(["signal_date", "ticker"]).index.isin(old.set_index(["signal_date", "ticker"]).index)]
        if len(o):
            pd.concat([old, o], ignore_index=True).to_csv(path, index=False)
    else:
        o.to_csv(path, index=False)
    return len(o)
