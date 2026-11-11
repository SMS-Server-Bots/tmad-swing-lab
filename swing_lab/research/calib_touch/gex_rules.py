import pandas as pd,numpy as np,sys
sys.path.insert(0,'calib'); from outcome import first_touch
M=pd.read_pickle("calib/labeled2.pkl").reset_index(drop=True)
X=1.5*M.p_atr10.fillna(M.p_atr10.median()).clip(1,15).values
pl,wl,_=first_touch(M,X,X,'long'); M['pay']=pl  # long payoff (short = aprox -pay en simétrico)
M['exl']=M.pay-M.groupby('date').pay.transform('mean')
dates=sorted(M.date.unique()); half=dates[len(dates)//2]
M['tot_gex_sign']=np.sign(M.total_gex)
M['wall_pos']=M.put_wall_pct.abs()/(M.put_wall_pct.abs()+M.call_wall_pct.abs())  # 0 = pegado al put wall, 1 = pegado al call wall
M['near_wall_pos']=M.near_put_wall_strike_distance_pct.abs()/(M.near_put_wall_strike_distance_pct.abs()+M.near_call_wall_strike_distance_pct.abs())
feats=['wall_pos','near_wall_pos','gamma_flip_distance_pct','near_gamma_flip_distance_pct','swing_gamma_flip_distance_pct','total_gex_change_pct','near_gex_change_pct','swing_gex_change_pct','gex_term_near_minus_swing','wall_asymmetry','wall_range_pct','near_gross_share_pct','call_accumulation_score','put_accumulation_score','tot_gex_sign','delta_near_put_wall_strike_distance_pct','delta_near_call_wall_strike_distance_pct']
out=[]
for f in feats:
    if f not in M or M[f].notna().mean()<0.3: continue
    for part,sub in [('1ª mitad',M[M.date<half]),('2ª mitad',M[M.date>=half])]:
        x=sub[[f,'exl','date']].dropna()
        if x[f].nunique()<5: 
            q=x.groupby(f).exl.mean(); out.append((f,part,'sign',round(q.get(1.0,np.nan)-q.get(-1.0,np.nan),2),np.nan)); continue
        x['q']=x.groupby('date')[f].transform(lambda s:pd.qcut(s.rank(method='first'),5,labels=False))
        dd=x[x.q==4].groupby('date').exl.mean()-x[x.q==0].groupby('date').exl.mean()
        t=dd.mean()/dd.std()*np.sqrt(len(dd))/np.sqrt(5)
        out.append((f,part,'Q5-Q1',round(dd.mean(),2),round(t,2)))
T=pd.DataFrame(out,columns=['feature','mitad','medida','largo_Q5_menos_Q1_%','t_adj'])
W=T.pivot(index='feature',columns='mitad',values=['largo_Q5_menos_Q1_%','t_adj'])
print(W.round(2).to_string())
