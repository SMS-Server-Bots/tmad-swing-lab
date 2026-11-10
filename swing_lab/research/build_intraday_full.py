import zipfile,sqlite3,glob,os,tempfile,pandas as pd,numpy as np
COLS="captured_at_utc,ticker,category,side,current_price,change_percent,option_signal,total_gex,gex_change,gamma_flip,max_pos_strike,max_neg_strike,rvol,trend,momentum_score,rsi14,iv_z_spread,otm_call_volume_z,otm_put_volume_z,pc_ratio,put_wall,call_wall,put_wall_pct,call_wall_pct,wall_range_pct,vwap"
out=[]
for z in sorted(glob.glob('/home/claude/data/intraday/intraday_*.zip')):
    zf=zipfile.ZipFile(z); n=[x for x in zf.namelist() if x.startswith('scanner_intraday')][0]
    with tempfile.TemporaryDirectory() as td:
        zf.extract(n,td); c=sqlite3.connect(os.path.join(td,n))
        out.append(pd.read_sql(f"select {COLS} from scanner_snapshots",c)); c.close()
I=pd.concat(out,ignore_index=True)
num=[c for c in COLS.split(',') if c not in ('captured_at_utc','ticker','category','side','option_signal','trend')]
for c in num: I[c]=pd.to_numeric(I[c],errors='coerce')
I=I[I.current_price>0]
et=pd.to_datetime(I.captured_at_utc,utc=True,format='ISO8601').dt.tz_convert('America/New_York')
I['date']=et.dt.strftime('%Y-%m-%d'); I['hm']=et.dt.hour*60+et.dt.minute
I=I[(I.hm>=570)&(I.hm<=960)]
I['slot']=((I.hm-575)//60).clip(0,6)   # 0=09:35,1=10:35,...,6=15:35
I=I.sort_values('hm').drop_duplicates(['ticker','date','slot'],keep='first')
I.to_pickle('intraday_full.pkl')
print(I.shape, I.date.nunique(), I.groupby('slot').size().to_dict())
print(I.groupby('slot')[['call_wall','max_pos_strike','trend']].apply(lambda d:d.notna().mean()).round(2))
