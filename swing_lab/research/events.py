import pandas as pd,numpy as np
from features import load
COST=0.002
S,U=load(5.0); U=U[U.category=='Stocks'].copy()
for h in [1,3,5]: U[f'a{h}']=U[f'r{h}']-U.groupby('trade_date')[f'r{h}'].transform('mean')
os_=U.option_signal.fillna(''); uf=U.unusual_flow_signal.fillna('')
EV={  # (mask, side) fixed ex-ante thresholds
 'CALL_SURGE_z>=3 & CallBuying':((U.otm_call_volume_z>=3)&(os_=='Call Buying'),1),
 'PUT_SURGE_z>=3 & PutBuying':((U.otm_put_volume_z>=3)&(os_=='Put Buying'),-1),
 'CALL_z>=5':(U.otm_call_volume_z>=5,1),
 'PUT_z>=5':(U.otm_put_volume_z>=5,-1),
 'IV_SPREAD>=2':(U.iv_z_spread>=2,1),
 'IV_SPREAD<=-2':(U.iv_z_spread<=-2,1),
 'IV_CHANGE>=3':(U.iv_change>=3,1),
 'IV_CHANGE<=-3':(U.iv_change<=-3,1),
 'PC_RATIO>=3':(U.pc_ratio>=3,-1),
 'PC_RATIO<=0.2':(U.pc_ratio<=0.2,1),
}
for k in ['Call Buying','Call Selling','Put Buying','Put Selling']: EV['opt:'+k]=(os_==k,1 if k in('Call Buying','Put Selling') else -1)
for k in ['Very High Call GEX','Very High Put GEX','High Call GEX','High Put GEX','Divergent Flow','High Both Sides','Very Low Call GEX','Very Low Put GEX']: EV['flow:'+k]=(uf==k,1)
rows=[]
for name,(m,side) in EV.items():
    e=U[m]
    for h in [1,3,5]:
        x=e[e[f'a{h}'].notna()]
        if len(x)<30: continue
        d=x.groupby('trade_date')[f'a{h}'].mean()*side
        t=d.mean()/d.std()*np.sqrt(len(d))/np.sqrt(h) if len(d)>3 else np.nan
        net=(x[f'r{h}']*side).mean()-COST
        rows.append((name,side,h,len(x),len(d),d.mean()*100,t,net*100))
R=pd.DataFrame(rows,columns=['event','side','h','n','days','alpha_%','t_adj','net_ret_%'])
R.to_csv('events_results.csv',index=False)
pd.set_option('display.width',200)
print(R.round(2).to_string(index=False))
print('\n|t|>=3:'); print(R[R.t_adj.abs()>=3].round(2).to_string(index=False))
