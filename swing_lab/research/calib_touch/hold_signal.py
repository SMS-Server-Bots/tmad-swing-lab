"""Sistema de Raul: entrar cuando CORE+GEX son favorables y mantener SIN stop hasta que dejen de serlo.
Dos periodos independientes: abr–jun 2026 (historial de detalle) y jul–oct 2026 (Scanner EOD).
Entrada y salida al cierre de la sesión SIGUIENTE a la señal (no se puede operar al cierre ya visto).
Se compara con el universo equiponderado en exactamente las mismas fechas (exceso)."""
import sqlite3, numpy as np, pandas as pd
def load_a():
    c=sqlite3.connect("file:/home/claude/data/legacy/tmad_scanner_live.db?mode=ro&immutable=1",uri=True)
    d=pd.read_sql("select ticker,substr(history_date,1,10) date,trend,momentum_score mom,total_gex gex,close_price close,captured_at_utc from ticker_detail_history",c)
    d=d.sort_values('captured_at_utc').drop_duplicates(['ticker','date'],keep='last')
    return d
def load_b():
    p=pd.read_pickle("/home/claude/lab/panel_raw.pkl").sort_values('run_utc').drop_duplicates(['ticker','trade_date'],keep='last')
    p=p[p.category=='Stocks'] if 'category' in p else p
    return p.rename(columns={'trade_date':'date','momentum_score':'mom','total_gex':'gex','current_price':'close'})[['ticker','date','trend','mom','gex','close']]
def run(d,label,variant,MAXH=40):
    for c in ['mom','gex','close']: d[c]=pd.to_numeric(d[c],errors='coerce')
    d['date']=d.date.astype(str)
    C=d.pivot(index='date',columns='ticker',values='close').sort_index()
    T=d.pivot(index='date',columns='ticker',values='trend').reindex(C.index)
    Mo=d.pivot(index='date',columns='ticker',values='mom').reindex(C.index)
    G=d.pivot(index='date',columns='ticker',values='gex').reindex(C.index)
    up=T.astype(str).str.contains('↑') if hasattr(T,'str') else None
    upT=T.apply(lambda s:s.astype(str).str.contains('↑|Up|up|Bull',regex=True)); dnT=T.apply(lambda s:s.astype(str).str.contains('↓|Down|down|Bear',regex=True))
    if variant=='core+gex': favL=upT&(Mo>0)&(G>0); favS=dnT&(Mo<0)&(G<0)
    elif variant=='core': favL=upT&(Mo>0); favS=dnT&(Mo<0)
    elif variant=='gex_exit': favL=upT&(Mo>0)&(G>0); favS=dnT&(Mo<0)&(G<0)
    ret=C.pct_change(fill_method=None); ret=ret.where(ret.abs()<0.5)
    U=ret.mean(axis=1)  # universo equiponderado
    dates=list(C.index); n=len(dates); rows=[]
    for side,fav in (('L',favL),('S',favS)):
        F=fav.values; Cv=C.values
        for j in range(C.shape[1]):
            f=F[:,j]; i=1
            while i<n-2:
                if f[i] and not f[i-1]:           # empieza a ser favorable (principio del movimiento)
                    e=i+1                          # entra al cierre de la sesión siguiente
                    k=e
                    while k<n-1 and k-e<MAXH:
                        still = f[k] if variant!='gex_exit' else (G.values[k,j]>0 if side=='L' else G.values[k,j]<0)
                        if not still: break
                        k+=1
                    x=min(k+1,n-1)                 # sale al cierre de la sesión siguiente al cambio
                    if x>e and np.isfinite(Cv[e,j]) and np.isfinite(Cv[x,j]) and Cv[e,j]>0:
                        r=Cv[x,j]/Cv[e,j]-1; r=r if side=='L' else -r
                        u=np.prod(1+U.values[e+1:x+1])-1; u=u if side=='L' else -u
                        if abs(r)<1.5: rows.append((dates[i],C.columns[j],side,r-0.002,u,x-e,k>=n-1))
                    i=max(k,i+1)
                else: i+=1
    R=pd.DataFrame(rows,columns=['date','ticker','side','ret','univ','days','censored'])
    R=R[~R.censored]
    R['ex']=R.ret-R.univ
    print(f"{label} [{variant}]")
    for s,g in R.groupby('side'):
        by=g.groupby('date').ex.mean(); t=by.mean()/by.std()*np.sqrt(len(by))/np.sqrt(max(1,g.days.mean()))
        print(f"   {s}: n={len(g)} días medio {g.days.mean():.1f} | ret {g.ret.mean()*100:+.2f}% gana {np.mean(g.ret>0)*100:.0f}% | exceso vs universo {g.ex.mean()*100:+.2f}% (gana al universo {np.mean(g.ex>0)*100:.0f}%) t_adj {t:+.2f}")
    return R
A=load_a(); B=load_b()
print(A.trend.value_counts().head(5).to_dict(), B.trend.value_counts().head(5).to_dict())
for v in ['core+gex','core','gex_exit']:
    run(A.copy(),"abr–jun 2026",v); run(B.copy(),"jul–oct 2026",v)
