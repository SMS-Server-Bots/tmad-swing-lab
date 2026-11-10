import pandas as pd,numpy as np
from features import load
COST=0.002; MAXH=15
S,U=load(5.0)
P=S[(S.category=='Stocks')&(S.current_price>=5)].copy()
for c in ['call_wall','put_wall','max_pos_strike']: P[c]=pd.to_numeric(P[c],errors='coerce')
P=P.sort_values(['ticker','si']); g=P.groupby('ticker')
P['core']=P.trend.map({'↑':1,'↓':-1,'→':0})
P['core_prev']=g.core.shift(); P['si_prev']=g.si.shift()
consec=(P.si-P.si_prev)<=2
sh=lambda c: np.sign(P[c]-g[c].shift())
P['gex_score']=(sh('call_wall').fillna(0)+sh('put_wall').fillna(0)+sh('max_pos_strike').fillna(0)).where(consec)
P['gex_dir']=np.where(P.gex_score>=2,1,np.where(P.gex_score<=-2,-1,0))
P['core_start']=np.where(consec&(P.core!=0)&(P.core!=P.core_prev),P.core,0)
spy=S[S.ticker=='SPY'].set_index('trade_date')
P['mkt']=np.sign(P.trade_date.map(spy.past5))
px=pd.read_pickle('daily_px.pkl'); from data_sessions import si_map
px['si']=px.date.map(si_map); px=px[px.si.notna()]; px['si']=px.si.astype(int)
O=px.pivot_table(index='ticker',columns='si',values='open_px',aggfunc='first')   # 09:35 price per session
univ=set(P.ticker)
def bench(e_si,x_si):
    a=O.get(e_si); b=O.get(x_si)
    if a is None or b is None: return np.nan
    r=(b/a-1).loc[list(univ & set(O.index))].dropna()
    r=r[(r>-0.5)&(r<1)]
    return r.mean()
BCACHE={}
def B(e,x):
    k=(e,x)
    if k not in BCACHE: BCACHE[k]=bench(e,x)
    return BCACHE[k]
state={t:d.set_index('si') for t,d in P.groupby('ticker')}
def run(entry_mask, exit_mode, fixed=None, seed=None):
    trades=[]
    E=P[entry_mask]
    if seed is not None:  # random entries: same count per day, random tickers & sides
        rng=np.random.default_rng(seed); rows=[]
        for d,gr in E.groupby('si'):
            pool=P[(P.si==d)]
            pick=pool.sample(len(gr),random_state=int(rng.integers(1e9)))
            pick=pick.assign(dir_=rng.choice([-1,1],len(pick))); rows.append(pick)
        E=pd.concat(rows) if rows else E
    for _,r in E.iterrows():
        dr=r['dir_'] if 'dir_' in E.columns and not pd.isna(r.get('dir_',np.nan)) else r.core_start
        t=r.ticker; st=state[t]; e_si=r.si+1
        if t not in O.index or e_si not in O.columns or pd.isna(O.at[t,e_si]): continue
        ep=O.at[t,e_si]; x_si=None
        for k in range(1,MAXH+1):
            d=r.si+k
            if fixed is not None:
                if k==fixed: x_si=d+1; break
                continue
            if d not in st.index: continue          # no EOD info that day: hold
            row=st.loc[d]
            gex_flip=row.gex_dir==-dr
            core_flip=row.core==-dr
            if (exit_mode=='gex' and gex_flip) or (exit_mode=='gex_or_core' and (gex_flip or core_flip)) or (exit_mode=='core' and core_flip):
                x_si=d+1; break
        if x_si is None: x_si=r.si+MAXH+1
        # find first available exit price at or after x_si
        xp=None
        for s in range(x_si,x_si+4):
            if s in O.columns and not pd.isna(O.at[t,s]): xp=O.at[t,s]; x_si=s; break
        if xp is None or xp/ep>3 or xp/ep<0.33: continue
        ret=dr*(xp/ep-1)-COST; b=B(e_si,x_si); alpha=dr*((xp/ep-1)-b) if not pd.isna(b) else np.nan
        trades.append((r.trade_date,t,dr,x_si-e_si,ret,alpha))
    T=pd.DataFrame(trades,columns=['date','ticker','dir','days','ret','alpha'])
    return T
def summ(name,T):
    if T.empty: return (name,0,np.nan,np.nan,np.nan,np.nan,np.nan,np.nan)
    d=T.groupby('date').alpha.mean().dropna()
    return (name,len(T),T.days.mean(),T.ret.mean()*100,(T.ret>0).mean()*100,T.alpha.mean()*100,d.mean()/d.std()*np.sqrt(len(d)) if len(d)>2 else np.nan,len(d))
base=P.core_start!=0
gex_ok=P.gex_dir==P.core_start
fus_ok=P.mkt==P.core_start
res=[]
res.append(summ('A CORE+GEX+FUSION | salida GEX o CORE',run(base&gex_ok&fus_ok,'gex_or_core')))
res.append(summ('B CORE+GEX+FUSION | salida GEX',run(base&gex_ok&fus_ok,'gex')))
res.append(summ('C CORE+GEX        | salida GEX o CORE',run(base&gex_ok,'gex_or_core')))
res.append(summ('D CORE solo       | salida GEX o CORE',run(base,'gex_or_core')))
res.append(summ('E CORE+GEX+FUSION | salida fija 3d',run(base&gex_ok&fus_ok,None,fixed=3)))
res.append(summ('F CORE+GEX+FUSION | salida fija 5d',run(base&gex_ok&fus_ok,None,fixed=5)))
for s in range(3): res.append(summ(f'R{s} AZAR mismo nº | salida GEX o CORE',run(base&gex_ok&fus_ok,'gex_or_core',seed=s)))
R=pd.DataFrame(res,columns=['sistema','ops','dias_medios','ret_neto_%','%ganadoras','alfa_%','t_alfa','dias_señal'])
pd.set_option('display.width',220); print(R.round(2).to_string(index=False))
R.to_csv('core_gex_fusion_results.csv',index=False)
for lab,(m,ex) in {'A':(base&gex_ok&fus_ok,'gex_or_core')}.items():
    T=run(m,ex); print('\nA por lado:'); print(T.groupby('dir').agg(ops=('ret','size'),ret=('ret','mean'),alfa=('alpha','mean'),dias=('days','mean')).assign(ret=lambda x:x.ret*100,alfa=lambda x:x.alfa*100).round(2))
    print('A por mes:'); print(T.groupby(T.date.str[:7]).agg(ops=('ret','size'),alfa=('alpha','mean')).assign(alfa=lambda x:x.alfa*100).round(2).T)
