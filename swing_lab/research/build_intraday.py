import os
import zipfile,sqlite3,glob,os,tempfile,pandas as pd
out=[]; runs=[]
for z in sorted(glob.glob(''+os.environ.get('TMAD_INTRADAY','data/intraday')+'/intraday_*.zip')):
    zf=zipfile.ZipFile(z); n=[x for x in zf.namelist() if x.startswith('scanner_intraday')][0]
    with tempfile.TemporaryDirectory() as td:
        zf.extract(n,td); c=sqlite3.connect(os.path.join(td,n))
        r=pd.read_sql("select id as run_id,captured_at_utc,session_type,total_tickers_ok,status from scanner_runs",c)
        s=pd.read_sql("select scanner_run_id as run_id,captured_at_utc,snapshot_date,session_type,ticker,current_price,change_percent,rvol,total_gex,gex_change from scanner_snapshots",c)
        c.close()
    r['file']=os.path.basename(z); runs.append(r); out.append(s)
I=pd.concat(out,ignore_index=True); R=pd.concat(runs,ignore_index=True)
I['current_price']=pd.to_numeric(I.current_price,errors='coerce')
I['ts']=pd.to_datetime(I.captured_at_utc,utc=True,format='ISO8601')
I['et']=I.ts.dt.tz_convert('America/New_York'); I['et_date']=I.et.dt.strftime('%Y-%m-%d'); I['et_hm']=I.et.dt.strftime('%H:%M')
I.to_pickle('intraday.pkl'); R.to_pickle('intraday_runs.pkl')
print(I.shape, I.et_date.nunique()); print(I.groupby(I.et.dt.hour).size()); print(R.session_type.value_counts().head(20))
