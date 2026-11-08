from __future__ import annotations
import json, os, re, shutil, sqlite3, time, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION='6.2.67'
ROOT=Path(os.getenv('TMAD_DATA_DIR', Path(__file__).resolve().parents[2]/'03_DATOS')).resolve()
DATA=ROOT/'data_bot'; LIVE=DATA/'db'/'live'; ARCHIVE=DATA/'db'/'archive'; OUTPUT=DATA/'output'
CACHE=OUTPUT/'gex_negative_cache.json'


def _madrid_now() -> datetime:
    return datetime.now(ZoneInfo('Europe/Madrid'))


def _off_hours() -> bool:
    now=_madrid_now()
    return now.weekday() >= 5 or not (12 <= now.hour < 24)


def _env_bool(name: str, default: bool) -> bool:
    raw=os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {'1','true','yes','on','y'}


def _storage_metrics(con: sqlite3.Connection, db: Path) -> dict:
    def p(name: str, default: int=0) -> int:
        try:
            row=con.execute(f'PRAGMA {name}').fetchone()
            return int(row[0] or 0) if row else default
        except Exception:
            return default
    page_size=p('page_size',4096); page_count=p('page_count'); freelist=p('freelist_count')
    allocated=max(0,page_size*page_count); reclaimable=max(0,page_size*freelist)
    return {
        'file_mb': round((db.stat().st_size if db.exists() else 0)/(1024*1024),1),
        'allocated_mb': round(allocated/(1024*1024),1),
        'reclaimable_mb': round(reclaimable/(1024*1024),1),
        'reclaimable_pct': round(reclaimable/max(1,allocated)*100,2),
        'auto_vacuum': p('auto_vacuum'),
        'freelist_pages': freelist,
    }


def build_negative_cache() -> dict:
    db=LIVE/'tmad_raw_live.db'; OUTPUT.mkdir(parents=True,exist_ok=True)
    result={'version':VERSION,'generated_at':_madrid_now().isoformat(),'tickers':{}}
    if not db.exists():
        CACHE.write_text(json.dumps(result,indent=2),encoding='utf-8'); return result
    con=sqlite3.connect(db)
    rows=con.execute("""SELECT UPPER(ticker), status_code, COUNT(*), MAX(snapshot_date)
      FROM raw_api_payloads WHERE endpoint_type LIKE 'gex%' AND ticker IS NOT NULL
      AND status_code IN (400,404) GROUP BY UPPER(ticker),status_code""").fetchall()
    for ticker,code,count,last_date in rows:
        days=5 if int(code)==404 else 2
        try: retry=(datetime.fromisoformat(last_date)+timedelta(days=days)).date().isoformat()
        except Exception: retry=last_date
        item=result['tickers'].setdefault(ticker,{'status_code':int(code),'failures':0,'last_failure_date':last_date,'retry_after_date':retry})
        item['failures'] += int(count)
        if str(last_date)>str(item['last_failure_date']): item.update(status_code=int(code),last_failure_date=last_date,retry_after_date=retry)
    con.close(); CACHE.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def cleanup_finalized_weeks() -> list[str]:
    removed=[]
    if not ARCHIVE.exists(): return removed
    for folder in ARCHIVE.iterdir():
        if not folder.is_dir() or not re.fullmatch(r'\d{4}-W\d{2}',folder.name): continue
        marker=folder/'FINALIZED.ok'; zpath=ARCHIVE/(folder.name+'.zip')
        if not marker.exists() or not zpath.exists(): continue
        try:
            with zipfile.ZipFile(zpath) as z:
                bad=z.testzip(); names=set(z.namelist())
                if bad or not any(n.endswith('/FINALIZED.ok') for n in names) or not any(n.endswith('/archive_manifest.json') for n in names): continue
            shutil.rmtree(folder); removed.append(folder.name)
        except Exception: continue
    return removed


def compact_extra_fields(cutoff_date: str | None=None) -> dict:
    """Roll old per-payload extra_fields into a compact daily schema-evidence table.

    Current market-day rows stay detailed for debugging. Historical raw payloads and
    schema_registry/schema_change_events are untouched, so this only removes a
    redundant denormalized copy after its daily evidence has been summarized.
    """
    db=LIVE/'tmad_meta_live.db'
    result={'rows_compacted':0,'daily_groups':0,'summary_purged':0,'cutoff_date':cutoff_date}
    if not db.exists(): return result
    cutoff_date=cutoff_date or _madrid_now().date().isoformat()
    retention_days=max(30,int(os.getenv('EXTRA_FIELD_SUMMARY_RETENTION_DAYS','180') or 180))
    summary_cutoff=(_madrid_now().date()-timedelta(days=retention_days)).isoformat()
    con=sqlite3.connect(db,timeout=30)
    con.execute('PRAGMA busy_timeout=30000')
    try:
        tables={str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'extra_fields' not in tables: return result
        con.executescript("""
        CREATE TABLE IF NOT EXISTS extra_field_daily_summary (
            snapshot_date TEXT NOT NULL,
            endpoint_type TEXT NOT NULL,
            field_path TEXT NOT NULL,
            value_type TEXT NOT NULL DEFAULT '',
            observations INTEGER NOT NULL DEFAULT 0,
            distinct_tickers INTEGER NOT NULL DEFAULT 0,
            first_seen_madrid TEXT,
            last_seen_madrid TEXT,
            sample_ticker TEXT,
            sample_value TEXT,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, endpoint_type, field_path, value_type)
        );
        CREATE INDEX IF NOT EXISTS idx_extra_field_daily_summary_date
          ON extra_field_daily_summary(snapshot_date, endpoint_type);
        """)
        rows=int(con.execute('SELECT COUNT(*) FROM extra_fields WHERE snapshot_date < ?', (cutoff_date,)).fetchone()[0] or 0)
        groups=int(con.execute("""SELECT COUNT(*) FROM (
            SELECT 1 FROM extra_fields WHERE snapshot_date < ?
            GROUP BY snapshot_date,endpoint_type,field_path,COALESCE(value_type,'')
        )""",(cutoff_date,)).fetchone()[0] or 0)
        now_utc=datetime.now(timezone.utc).isoformat(timespec='seconds')
        if rows:
            con.execute("""
            INSERT INTO extra_field_daily_summary(
              snapshot_date,endpoint_type,field_path,value_type,observations,distinct_tickers,
              first_seen_madrid,last_seen_madrid,sample_ticker,sample_value,updated_at_utc
            )
            SELECT snapshot_date,endpoint_type,field_path,COALESCE(value_type,''),COUNT(*),
              COUNT(DISTINCT COALESCE(ticker,'')),MIN(captured_at_madrid),MAX(captured_at_madrid),
              MIN(ticker),MIN(SUBSTR(COALESCE(field_value,''),1,1000)),?
            FROM extra_fields WHERE snapshot_date < ?
            GROUP BY snapshot_date,endpoint_type,field_path,COALESCE(value_type,'')
            ON CONFLICT(snapshot_date,endpoint_type,field_path,value_type) DO UPDATE SET
              observations=excluded.observations,
              distinct_tickers=excluded.distinct_tickers,
              first_seen_madrid=excluded.first_seen_madrid,
              last_seen_madrid=excluded.last_seen_madrid,
              sample_ticker=excluded.sample_ticker,
              sample_value=excluded.sample_value,
              updated_at_utc=excluded.updated_at_utc
            """,(now_utc,cutoff_date))
            cur=con.execute('DELETE FROM extra_fields WHERE snapshot_date < ?', (cutoff_date,))
            result['rows_compacted']=max(0,int(cur.rowcount or rows))
            result['daily_groups']=groups
        cur=con.execute('DELETE FROM extra_field_daily_summary WHERE snapshot_date < ?', (summary_cutoff,))
        result['summary_purged']=max(0,int(cur.rowcount or 0))
        result['cutoff_date']=cutoff_date; result['summary_retention_days']=retention_days
        con.commit()
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return result
    except Exception:
        try: con.rollback()
        except Exception: pass
        raise
    finally:
        con.close()


def optimize_sqlite() -> tuple[list[str], list[str], list[dict]]:
    if not _off_hours():
        return [], [], []
    done: list[str] = []
    failed: list[str] = []
    details: list[dict] = []
    pages=max(0,int(os.getenv('SQLITE_INCREMENTAL_VACUUM_PAGES','0') or 0))
    weekend=_madrid_now().weekday() >= 5
    full_vacuum=weekend and _env_bool('SQLITE_WEEKEND_VACUUM_ENABLED',True)
    min_mb=max(32.0,float(os.getenv('SQLITE_WEEKEND_VACUUM_MIN_FREE_MB','128') or 128))
    min_pct=max(5.0,float(os.getenv('SQLITE_WEEKEND_VACUUM_MIN_FREE_PCT','20') or 20))
    for db in LIVE.glob('*.db'):
        con = None
        try:
            con=sqlite3.connect(db,timeout=15,isolation_level=None)
            con.execute('PRAGMA busy_timeout=15000')
            before=_storage_metrics(con,db)
            con.execute('PRAGMA wal_checkpoint(PASSIVE)')
            con.execute('PRAGMA optimize')
            incremental=False; vacuumed=False
            if pages and int(before.get('auto_vacuum') or 0)==2:
                con.execute(f'PRAGMA incremental_vacuum({pages})'); incremental=True
            if full_vacuum and float(before.get('reclaimable_mb') or 0)>=min_mb and float(before.get('reclaimable_pct') or 0)>=min_pct:
                con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                con.execute('VACUUM')
                con.execute('PRAGMA optimize')
                vacuumed=True
            after=_storage_metrics(con,db)
            done.append(db.name)
            details.append({'db':db.name,'before':before,'after':after,'incremental_vacuum':incremental,'full_vacuum':vacuumed})
        except Exception as exc:
            failed.append(f"{db.name}:{type(exc).__name__}")
        finally:
            if con is not None:
                try: con.close()
                except Exception: pass
    return done, failed, details


def main():
    started=time.perf_counter()
    off_hours=_off_hours()
    cache=build_negative_cache()
    removed=cleanup_finalized_weeks()
    compact={'rows_compacted':0,'daily_groups':0,'summary_purged':0}
    if off_hours:
        try: compact=compact_extra_fields()
        except Exception as exc: compact={'rows_compacted':0,'daily_groups':0,'summary_purged':0,'error':f'{type(exc).__name__}:{exc}'}
    optimized, failed, storage=optimize_sqlite()
    status='OK' if not failed and not compact.get('error') else 'PARTIAL'
    if not off_hours: status='CACHE_ONLY_MARKET_HOURS'
    live_db_mb=sum(p.stat().st_size for p in LIVE.glob('*.db') if p.is_file())/(1024*1024) if LIVE.exists() else 0.0
    vacuums=sum(1 for item in storage if item.get('full_vacuum'))
    reclaimable=sum(float((item.get('after') or {}).get('reclaimable_mb') or 0) for item in storage)
    print(
        f"[MAINT DATA v{VERSION}] status={status} elapsed={time.perf_counter()-started:.2f}s "
        f"window={'OFF_HOURS' if off_hours else 'MARKET_HOURS'} live_db={live_db_mb:.1f}MB "
        f"gex_negative_cache={len(cache['tickers'])} weekly_removed={len(removed)} "
        f"extra_compacted={int(compact.get('rows_compacted') or 0)} extra_groups={int(compact.get('daily_groups') or 0)} "
        f"sqlite_ok={len(optimized)} sqlite_failed={len(failed)} vacuumed={vacuums} reclaimable_after={reclaimable:.1f}MB "
        f"optimized={','.join(optimized) or 'none'} failed={','.join(failed) or 'none'}",
        flush=True,
    )
    return {
        'cache_tickers': len(cache['tickers']),
        'weekly_removed': removed,
        'extra_fields': compact,
        'sqlite_optimized': optimized,
        'sqlite_failed': failed,
        'storage': storage,
        'off_hours': off_hours,
    }

if __name__=='__main__': main()
