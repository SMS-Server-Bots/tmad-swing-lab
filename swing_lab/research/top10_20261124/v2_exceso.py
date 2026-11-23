import pandas as pd, numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from swing_lab import store, labels, top10
s=pd.read_pickle("/home/claude/data/store.pkl"); L=pd.read_pickle("/home/claude/data/labels.pkl")
print("store hasta", s.trade_date.max(), "| etiquetas con resultado:", L.long_res.notna().sum())
D=s[top10.universe(s)].merge(L,on=["ticker","trade_date"])
for side in ("long","short"):
    D[f"{side}_x"]=D[f"{side}_R"]-D.groupby("trade_date")[f"{side}_R"].transform("mean")
cols=[c for c in store.feature_columns(s) if s[c].notna().mean()>0.05]
sis=np.sort(D.si.unique()); first=sis[0]+25; preds=[]; mdl=None; last=None
P=dict(max_depth=3,learning_rate=0.05,max_iter=200,min_samples_leaf=400,l2_regularization=2.0,max_features=0.5,random_state=7)
for si in sis[sis>=first]:
    if mdl is None or si-last>=5:
        tr=D[(D.si+6<si)&D.long_x.notna()&D.short_x.notna()]; cc=[c for c in cols if tr[c].nunique()>1]
        mdl={sd:HistGradientBoostingRegressor(**P).fit(tr[cc],tr[f"{sd}_x"].clip(-3,3)) for sd in ("long","short")}; last=si
    d=D[D.si==si].copy()
    if d.long_x.isna().all(): continue
    d["pL"]=mdl["long"].predict(d[cc]); d["pS"]=mdl["short"].predict(d[cc]); preds.append(d)
Q=pd.concat(preds); Q["p"]=Q[["pL","pS"]].max(axis=1); side=np.where(Q.pL>=Q.pS,"L","S")
Q["x"]=np.where(side=="L",Q.long_x,Q.short_x); Q["R"]=np.where(side=="L",Q.long_R,Q.short_R)
rows=[]
for nm,sel in (("V2 top10 por exceso previsto",lambda d:d.nlargest(10,"p")),
               ("V2 solo largos top10",lambda d:d.nlargest(10,"pL").assign(x=lambda z:z.long_x,R=lambda z:z.long_R)),
               ("V2 solo cortos top10",lambda d:d.nlargest(10,"pS").assign(x=lambda z:z.short_x,R=lambda z:z.short_R)),
               ("V2 peores 10 (control)",lambda d:d.nsmallest(10,"p"))):
    g=pd.concat([sel(d) for _,d in Q.groupby("trade_date")]).dropna(subset=["x"])
    dd=g.groupby("trade_date").x.mean(); h=dd.index<"2026-10-09"
    rows.append(dict(estrategia=nm,dias=len(dd),exceso_R=dd.mean(),t=top10.nw_t(dd.values,5),primera_mitad=dd[h].mean(),segunda_mitad=dd[~h].mean(),R_bruto=g.groupby("trade_date").R.mean().mean()))
pd.set_option("display.width",200); print(pd.DataFrame(rows).round(3).to_string(index=False))
ic=pd.DataFrame([{"d":k,"L":d.pL.corr(d.long_x,method="spearman"),"S":d.pS.corr(d.short_x,method="spearman")} for k,d in Q.dropna(subset=["long_x"]).groupby("trade_date")])
print("IC medio (rango) largo %.3f t %.2f | corto %.3f t %.2f | días %d" % (ic.L.mean(), top10.nw_t(ic.L.values,5), ic.S.mean(), top10.nw_t(ic.S.values,5), len(ic)))
Q.to_pickle("/home/claude/data/v2_preds.pkl")
