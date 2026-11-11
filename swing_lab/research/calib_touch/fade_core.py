"""Contraria: cuando CORE(+GEX) pasa a favorable al alza -> corto; a la baja -> largo. Horizonte fijo, entrada
al cierre de la sesión siguiente, exceso frente al universo equiponderado en las mismas fechas. Coste 0,2 %."""
import numpy as np, pandas as pd, sys
sys.argv=['x']; exec(open('calib/hold_signal.py').read().split("def run(")[0])
def fade(d,label,variant):
    for c in ['mom','gex','close']: d[c]=pd.to_numeric(d[c],errors='coerce')
    d['date']=d.date.astype(str)
    C=d.pivot(index='date',columns='ticker',values='close').sort_index()
    T=d.pivot(index='date',columns='ticker',values='trend').reindex(C.index)
    Mo=d.pivot(index='date',columns='ticker',values='mom').reindex(C.index); G=d.pivot(index='date',columns='ticker',values='gex').reindex(C.index)
    upT=T.apply(lambda s:s.astype(str).str.contains('↑')); dnT=T.apply(lambda s:s.astype(str).str.contains('↓'))
    if variant=='core+gex': favL=upT&(Mo>0)&(G>0); favS=dnT&(Mo<0)&(G<0)
    else: favL=upT&(Mo>0); favS=dnT&(Mo<0)
    onL=favL&~favL.shift(1,fill_value=False); onS=favS&~favS.shift(1,fill_value=False)
    ret=C.pct_change(fill_method=None).where(lambda x:x.abs()<0.5)
    out=[]
    for H in (3,5,10):
        fwd=C.shift(-1-H)/C.shift(-1)-1               # cierre t+1 -> cierre t+1+H
        uf=fwd.where(fwd.abs()<1.5).mean(axis=1)
        ex=fwd.sub(uf,axis=0)
        a=(-ex[onL]).stack(); b=(ex[onS]).stack()       # contraria: corto tras giro alcista, largo tras giro bajista
        for lab,s in (('corto tras giro ↑',a),('largo tras giro ↓',b)):
            s=s.dropna()-0.002; by=s.groupby(level=0).mean(); t=by.mean()/by.std()*np.sqrt(len(by))/np.sqrt(H)
            out.append((label,variant,H,lab,len(s),round(s.mean()*100,2),round(np.mean(s>0)*100),round(t,2)))
    return out
rows=[]
for v in ['core+gex','core']:
    rows+=fade(load_a(),"abr–jun",v); rows+=fade(load_b(),"jul–oct",v)
print(pd.DataFrame(rows,columns=['periodo','señal','H','operación','n','exceso_%','gana_%','t_adj']).to_string(index=False))
