import pandas as pd,numpy as np,itertools
COST=0.002
o=pd.read_pickle('panel18.pkl')
U=o[(o.category=='Stocks')&(o.close>=5)&(o.adv20>=5e6)&(o.jump60==0)&o.consistent&o.path_ok&o.atr14.notna()].copy()
U=U.sample(frac=0.5,random_state=1)   # speed; still ~110k entries
mid=sorted(U.date.unique())[len(U.date.unique())//2]
def sim(df,side,k,m,H):
    e=df.entry.values; a=df.atr14.values.clip(0.005,0.15); sg=1 if side=='L' else -1
    stop=e*(1-sg*k*a) if k else None; tgt=e*(1+sg*m*a) if m else None
    out=np.full(len(df),np.nan); done=np.zeros(len(df),bool)
    for j in range(1,H+1):
        op=df[f'open_{j}'].values; hi=df[f'high_{j}'].values; lo=df[f'low_{j}'].values; cl=df[f'close_{j}'].values
        v=~done
        if stop is not None:
            if j>1:  # gap through stop at the open
                g=v&((op<=stop) if sg==1 else (op>=stop)); out[g]=sg*(op[g]/e[g]-1); done|=g; v&=~g
            h=v&((lo<=stop) if sg==1 else (hi>=stop)); out[h]=sg*(stop[h]/e[h]-1); done|=h; v&=~h
        if tgt is not None:
            if j>1:
                g=v&((op>=tgt) if sg==1 else (op<=tgt)); out[g]=sg*(op[g]/e[g]-1); done|=g; v&=~g
            h=v&((hi>=tgt) if sg==1 else (lo<=tgt)); out[h]=sg*(tgt[h]/e[h]-1); done|=h; v&=~h
        if j==H: out[v]=sg*(cl[v]/e[v]-1); done|=v
    return out-COST
rows=[]
for side in ['L','S']:
    for H in [2,3,5]:
        for k,m in itertools.product([None,1,1.5,2,3],[None,1,2,3]):
            r=pd.Series(sim(U,side,k,m,H),index=U.index); d=r.groupby(U.date).mean()
            rows.append((side,H,k,m,d[d.index<mid].mean()*100,d[d.index>=mid].mean()*100,d.mean()*100,np.nanpercentile(r,5)*100))
G=pd.DataFrame(rows,columns=['side','H','stop_atr','tgt_atr','h1_%','h2_%','all_%','p5_%'])
G.to_csv('exits18_results.csv',index=False)
for side in ['L','S']:
    g=G[G.side==side].copy(); base=g[g.stop_atr.isna()&g.tgt_atr.isna()].set_index('H')['all_%']
    g['vs_hold']=g['all_%']-g.H.map(base); g['min_half']=g[['h1_%','h2_%']].min(axis=1)
    print('==',side,'hold:',base.round(3).to_dict()); print(g.sort_values('min_half',ascending=False).head(6).round(3).to_string(index=False))
