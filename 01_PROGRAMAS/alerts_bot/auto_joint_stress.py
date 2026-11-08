from __future__ import annotations

"""Detached one-shot historical stress: frozen Rolling/Fixed, no FULL or LIVE."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib import request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = Path(__file__).resolve().parent
RUNTIME = ROOT / "04_RUNTIME"
SOURCE = ROOT / "03_DATOS" / "alerts_bot" / "output" / "historical_validation"
STATUS = RUNTIME / "autonomous_research" / "joint_entry_exit_stress"
LANES = ("ROLLING_LATEST", "FIXED_BENCHMARK")
FILES = ("rolling_latest.v1.json.gz", "fixed_benchmark.v1.json.gz")
COST_BPS = 20.0


def _read(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _save(state: str, **detail: object) -> None:
    STATUS.mkdir(parents=True, exist_ok=True)
    target = STATUS / "latest_status.json"
    tmp = STATUS / ("latest_status.json.tmp." + str(os.getpid()))
    payload = {
        "status": state, "worker_pid": os.getpid(),
        "updated_at_madrid": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
        "final_test_locked": True, "live_mutation": False, **detail,
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    print("[TMAD_STRESS] " + state + " " + str(detail)[:300], flush=True)


def _lock():
    STATUS.mkdir(parents=True, exist_ok=True)
    path = STATUS / "single_instance.lock"
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def _cache_pair() -> tuple[dict[str, str], str | None]:
    status = _read(SOURCE / "latest_fast_research_cache_status.json")
    lanes = status.get("lanes") or {}
    fingerprint = {}
    contracts = set()
    for lane, filename in zip(LANES, FILES):
        meta = lanes.get(lane) or {}
        path = RUNTIME / "autonomous_research" / "cache" / filename
        if (meta.get("status") != "OK" or not meta.get("dataset_fingerprint")
                or not meta.get("contract_hash") or not path.is_file()
                or path.stat().st_size < 100):
            return {}, lane + "_CACHE_UNAVAILABLE"
        fingerprint[lane] = str(meta["dataset_fingerprint"])
        contracts.add(str(meta["contract_hash"]))
    if len(contracts) != 1:
        return {}, "CACHE_CONTRACT_CONFLICT"
    perf = _read(SOURCE / "latest_historical_replay_performance.json")
    if perf.get("status") != "ok":
        return {}, "LAST_HISTORICAL_NOT_VALID"
    if str(status.get("producer_version")) != str(perf.get("producer_version")):
        return {}, "HISTORICAL_PRODUCER_MISMATCH"
    return fingerprint, None


def _price_revision() -> str:
    report = _read(SOURCE / "latest_research_ohlc_backfill.json")
    return str((report.get("outcomes") or {}).get("revision") or "NONE")


def _backfill_saturated(pair: dict[str, str]) -> bool:
    """Don't requery proven provider failures when inputs have not changed."""
    last = _read(SOURCE / "latest_research_ohlc_backfill.json")
    gaps = last.get("gaps") or {}
    outcome = last.get("outcomes") or {}
    recorded_pair = {
        lane: str((((last.get("required") or {}).get("cache") or {}).get(lane) or {}).get("fingerprint") or "")
        for lane in LANES
    }
    if recorded_pair != pair or last.get("rate_limited"):
        return False
    if not outcome.get("revision") or gaps.get("keys") is None:
        return False
    before, after = gaps.get("keys"), outcome.get("gaps_after_keys")
    # For 6.2.64: second batch 88 -> 88, 37 failed tickers,
    # no untried symbols. Keeping hammering Yahoo cannot increase coverage.
    return (int(before) > 0 and int(after or -1) == int(before) and
            int(outcome.get("newly_unattempted_queue_remaining") or 0) == 0)


def _triage(python: Path, pair: dict[str, str]) -> bool:
    script = PROGRAM / "research_ohlc_gap_triage.py"
    if not script.is_file():
        _save("TRIAGE_RUNNER_MISSING")
        return False
    log = STATUS / ("gap_triage_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    _save("TRIAGE_RUNNING", fingerprints=pair, research_ohlc_revision=_price_revision(), log=str(log))
    env = os.environ.copy()
    env.update({
        "TMAD_INSTALL_ROOT": str(ROOT), "TMAD_CONFIG_DIR": str(ROOT / "02_CONFIG"),
        "TMAD_DATA_DIR": str(ROOT / "03_DATOS"),
        "TMAD_RUNTIME_DIR": str(RUNTIME),
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
    })
    try:
        with log.open("wb") as out:
            process = subprocess.run(
                [str(python), "-u", str(script), "--max-secondary-tickers", "40",
                 "--pause-seconds", "1.5"],
                cwd=str(PROGRAM), env=env, stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, timeout=40 * 60,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                                  getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)),
            )
        if process.returncode:
            _save("ERROR_GAP_TRIAGE", exit_code=process.returncode, log=str(log))
            return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        _save("ERROR_GAP_TRIAGE", error=type(exc).__name__, log=str(log))
        return False
    result = _read(SOURCE / "latest_research_ohlc_gap_triage.json")
    actual_pair = result.get("source_cache") or {}
    fingerprints = {
        lane: str((actual_pair.get(lane) or {}).get("fingerprint") or "")
        for lane in LANES
    }
    if fingerprints != pair:
        _save("ERROR_GAP_TRIAGE_CACHE_MISMATCH", expected=pair, actual=fingerprints)
        return False
    gaps = result.get("residual") or {}
    economic = result.get("economic_validation") or {}
    secondary = result.get("secondary_source") or {}
    _save("TRIAGE_COMPLETE",
          fingerprints=pair, research_ohlc_revision=_price_revision(),
          residual_signals=gaps.get("missing_10session_signal_keys"),
          residual_tickers=gaps.get("affected_tickers"),
          secondary=secondary.get("status"),
          train_frozen_candidates=economic.get("train_frozen_shortlist_size"),
          live_promotion_allowed=False, final_test="LOCKED",
          report=str(SOURCE / "latest_research_ohlc_gap_triage.json"), log=str(log))
    _notify("🔬 TMAD diagnóstico histórico: " +
            str(gaps.get("missing_10session_signal_keys")) + " señales / " +
            str(gaps.get("affected_tickers")) + " tickers con OHLC pendiente; " +
            "fuente secundaria " + str(secondary.get("status")) +
            ". Alternativas en auditoría solamente; LIVE intacto.")
    return True


def _already_done(pair: dict[str, str]) -> bool:
    report = _read(SOURCE / "latest_joint_entry_exit_stress.json")
    if report.get("mode") != "JOINT_ENTRY_DIRECTION_EXIT_STRESS_V1":
        return False
    if (report.get("decision") or {}).get("cost_bps") != COST_BPS:
        return False
    if str((report.get("methodology") or {}).get("research_ohlc_revision") or "NONE") != _price_revision():
        return False
    grid = report.get("train_search") or {}
    return all(
        str(((grid.get(label) or {}).get("cache") or {}).get("fingerprint") or "") == pair[lane]
        for label, lane in (("rolling", LANES[0]), ("fixed", LANES[1]))
    )


def _process_busy() -> tuple[bool | None, str]:
    if os.name != "nt":
        return False, ""
    cmd = (
        "$ErrorActionPreference='Stop'; "
        "$p=Get-CimInstance Win32_Process -Filter "
        "\"Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'powershell.exe'\"; "
        "$m=@($p | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match "
        "'historical_replay_once[.]py|adaptive_research_cycle[.]py|"
        "deferred_historical_replay[.]ps1|research_joint_entry_exit_stress[.]py|research_ohlc_backfill[.]py' }); "
        "Write-Output ($m.Count)"
    )
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
            text=True, capture_output=True, timeout=35,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        numbers = re.findall(r"^\s*(\d+)\s*$", p.stdout, re.MULTILINE)
        if p.returncode or not numbers:
            return None, "PROCESS_INSPECTION_FAILED"
        return int(numbers[-1]) > 0, "competing_count=" + numbers[-1]
    except (OSError, subprocess.TimeoutExpired):
        return None, "PROCESS_INSPECTION_EXCEPTION"


def _startup_ready() -> bool:
    status = _read(SOURCE.parent / "startup_calibration_status.json")
    return bool(status.get("finished_at_madrid")) and str(status.get("status") or "").lower() not in {
        "", "running", "starting", "error", "failed"
    }


def _notify(text: str) -> None:
    cfg = ROOT / "02_CONFIG"
    url = ""
    for name in ("configuracion_canales.env", "tmad_execution.secret.env"):
        path = cfg / name
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if line.strip().startswith("EXECUTION_WEBHOOK="):
                    url = line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    if url:
        try:
            req = request.Request(url,
                data=json.dumps({"content": text[:1800]}, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with request.urlopen(req, timeout=15) as response:
                response.read()
        except Exception:
            pass


def run() -> int:
    if os.environ.get("TMAD_JOINT_STRESS_AUTO_ENABLED", "1").lower() in ("0", "false", "no"):
        _save("DISABLED")
        return 0
    python = RUNTIME / "venvs" / "alerts_bot" / "Scripts" / "python.exe"
    runner = PROGRAM / "research_joint_entry_exit_stress.py"
    if not python.is_file() or not runner.is_file():
        _save("RUNNER_MISSING")
        return 1
    lock = _lock()
    if lock is None:
        print("[TMAD_STRESS] already running", flush=True)
        return 0

    try:
        deadline = time.monotonic() + 16 * 60 * 60
        _save("WAITING_FOR_STARTUP_AND_HISTORY")
        while time.monotonic() < deadline:
            busy, detail = _process_busy()
            if busy is None:
                _save("PROCESS_INSPECTION_ERROR", detail=detail)
                return 1
            if busy or not _startup_ready():
                time.sleep(45)
                continue
            pair, error = _cache_pair()
            if error:
                _save("WAITING_VALID_CACHE", reason=error)
                time.sleep(45)
                continue
            # Research-only, resumable batches; no repeated FULL. Previous
            # releases stopped at 400 symbols regardless of uncovered dates.
            # Audit coverage after each batch and stop on saturation/429.
            backfill_runner = PROGRAM / "research_ohlc_backfill.py"
            if backfill_runner.is_file() and not _backfill_saturated(pair):
                env_backfill = os.environ.copy()
                env_backfill.update({
                    "TMAD_INSTALL_ROOT": str(ROOT),
                    "TMAD_CONFIG_DIR": str(ROOT / "02_CONFIG"),
                    "TMAD_DATA_DIR": str(ROOT / "03_DATOS"),
                    "TMAD_RUNTIME_DIR": str(RUNTIME),
                    "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
                })
                last_report = {}
                for batch_no in range(1, 4):
                    # Recheck other background research before each iteration.
                    concurrent, reason = _process_busy()
                    if concurrent is None:
                        _save("PROCESS_INSPECTION_ERROR", detail=reason)
                        return 1
                    if concurrent:
                        _save("WAITING_HISTORY_BETWEEN_BATCHES", reason=reason)
                        time.sleep(45)
                        break
                    log_b = STATUS / ("backfill_" + datetime.now().strftime("%Y%m%d_%H%M%S") +
                                      "_batch" + str(batch_no) + ".log")
                    _save("BACKFILL_RUNNING", fingerprints=pair, batch=batch_no, max_batches=3, log=str(log_b))
                    try:
                        with log_b.open("wb") as out_b:
                            backfill = subprocess.run(
                                [str(python), "-u", str(backfill_runner), "--max-tickers", "300",
                                 "--pause-seconds", "1.25"],
                                cwd=str(PROGRAM), env=env_backfill, stdin=subprocess.DEVNULL,
                                stdout=out_b, stderr=subprocess.STDOUT, timeout=2 * 60 * 60,
                                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                                                  getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)),
                            )
                        if backfill.returncode:
                            _save("ERROR_BACKFILL", exit_code=backfill.returncode, batch=batch_no, log=str(log_b))
                            _notify("❌ TMAD: el backfill histórico falló. Revisa el log; LIVE sigue intacto.")
                            return 1
                    except subprocess.TimeoutExpired:
                        _save("ERROR_BACKFILL_TIMEOUT", batch=batch_no, log=str(log_b))
                        return 1
                    report_b = _read(SOURCE / "latest_research_ohlc_backfill.json")
                    if not report_b or not (report_b.get("outcomes") or {}).get("revision"):
                        _save("ERROR_BACKFILL_REPORT", batch=batch_no)
                        return 1
                    reported_pair = {
                        lane: str(((report_b.get("required") or {}).get("cache") or {}).get(lane, {}).get("fingerprint") or "")
                        for lane in LANES
                    }
                    if reported_pair != pair:
                        _save("ERROR_BACKFILL_CACHE_CHANGED", expected=pair, reported=reported_pair)
                        return 1
                    last_report = report_b
                    outcomes = report_b.get("outcomes") or {}
                    _save("BACKFILL_BATCH_COMPLETE", batch=batch_no,
                          gaps_before=(report_b.get("gaps") or {}).get("keys"),
                          gaps_after=outcomes.get("gaps_after_keys"),
                          improvement=outcomes.get("verified_gap_reduction"),
                          attempted=report_b.get("attempted"),
                          remaining=outcomes.get("gaps_after_tickers"),
                          rate_limited=bool(report_b.get("rate_limited")))
                    if (report_b.get("rate_limited") or
                            outcomes.get("coverage_status") == "COMPLETE" or
                            int(report_b.get("attempted") or 0) == 0):
                        break
                    if (int(outcomes.get("verified_gap_reduction") or 0) == 0 and
                            int(outcomes.get("newly_unattempted_queue_remaining") or 0) == 0):
                        # No new untried symbols and no verified data gain.
                        # Avoid hammering permanently delisted/rejected tickers.
                        break
                if last_report:
                    outcomes = last_report.get("outcomes") or {}
                    _notify(
                        "📈 TMAD — recuperación OHLC revisada: "
                        + str(outcomes.get("gaps_after_keys", "n/a"))
                        + " señales sin 10 sesiones verificadas; "
                        + str(outcomes.get("gaps_after_tickers", "n/a"))
                        + " tickers pendientes. "
                        + ("Límite proveedor; reintento en próxima release. "
                           if last_report.get("rate_limited") else "")
                        + "Preparando validación económica sin tocar LIVE."
                    )
            if _already_done(pair):
                _save("ALREADY_EVALUATED", fingerprints=pair, research_ohlc_revision=_price_revision())
                return 0 if _triage(python, pair) else 1
            time.sleep(3)
            again, detail = _process_busy()
            if again is None:
                _save("PROCESS_INSPECTION_ERROR", detail=detail)
                return 1
            if again:
                time.sleep(45)
                continue
            env = os.environ.copy()
            env.update({
                "TMAD_INSTALL_ROOT": str(ROOT), "TMAD_CONFIG_DIR": str(ROOT / "02_CONFIG"),
                "TMAD_DATA_DIR": str(ROOT / "03_DATOS"),
                "TMAD_RUNTIME_DIR": str(RUNTIME),
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            })
            log = STATUS / ("stress_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
            _save("RUNNING", fingerprints=pair, log=str(log))
            _notify("🧪 TMAD: stress test de entrada × dirección × salida iniciado con histórico congelado Rolling + Fixed. Sin FULL ni cambios LIVE.")
            try:
                with log.open("wb") as out:
                    result = subprocess.run(
                        [str(python), "-u", str(runner), "--cost-bps", str(COST_BPS)],
                        cwd=str(PROGRAM), env=env, stdin=subprocess.DEVNULL,
                        stdout=out, stderr=subprocess.STDOUT, timeout=5 * 60 * 60,
                        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                                          getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)),
                    )
            except subprocess.TimeoutExpired:
                _save("ERROR_TIMEOUT", log=str(log))
                _notify("❌ TMAD: stress histórico agotó el límite de ejecución; LIVE intacto.")
                return 1
            if result.returncode:
                _save("ERROR", exit_code=result.returncode, log=str(log))
                _notify("❌ TMAD: falló el stress histórico; revisa el log. LIVE intacto.")
                return result.returncode
            report = _read(SOURCE / "latest_joint_entry_exit_stress.json")
            if not _already_done(pair):
                _save("ERROR_REPORT_MISMATCH", log=str(log))
                return 1
            shortlist = len(report.get("train_frozen_shortlist") or [])
            _save("COMPLETE", fingerprints=pair, research_ohlc_revision=_price_revision(), shortlist=shortlist,
                  decision=(report.get("decision") or {}).get("status"),
                  result=str(SOURCE / "latest_joint_entry_exit_stress.json"), log=str(log))
            _notify("✅ TMAD: stress histórico completado. Variantes TRAIN: "
                    + str((report.get("train_search") or {}).get("candidate_variants"))
                    + " · shortlist Rolling+Fixed: " + str(shortlist)
                    + " · " + str((report.get("decision") or {}).get("status"))
                    + ". Informe: latest_joint_entry_exit_stress.json. Sin cambios LIVE.")
            return 0 if _triage(python, pair) else 1
        _save("WAIT_TIMEOUT", reason="Startup, historical replay or caches never became ready")
        _notify("⚠️ TMAD: stress histórico no iniciado porque los datos/procesos no quedaron listos.")
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(run())
