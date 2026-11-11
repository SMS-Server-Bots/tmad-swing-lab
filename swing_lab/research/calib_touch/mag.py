import pandas as pd,numpy as np,sys,warnings; warnings.filterwarnings('ignore')
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
exec(open('calib/wf.py').read().split("MODE = sys.argv")[0])
M['mv']=((M.mfe5>=5)|(M.mae5<=-5)).astype(int)
dates=sorted(M.date.unique()); parts=[]
for s in range(25,len(dates),5):
    tr=M[M.date.isin(dates[:s-6])]; te=M[M.date.isin(dates[s:s+5])].copy()
    clf=HistGradientBoostingClassifier(max_depth=3,learning_rate=0.05,max_iter=200,min_samples_leaf=200,l2_regularization=1.0,random_state=0).fit(tr[NUM],tr.mv)
    te['pm']=clf.predict_proba(te[NUM])[:,1]; parts.append(te)
R=pd.concat(parts)
auc=np.mean([roc_auc_score(g.mv,g.pm) for d,g in R.groupby('date') if g.mv.nunique()==2])
R.to_pickle("calib/mag_pred.pkl")
R["dec"]=R.groupby('date').pm.transform(lambda s:pd.qcut(s.rank(method='first'),10,labels=False))
print("AUC movimiento ±5%% en 5 sesiones: %.3f"%auc)
print(R.groupby('dec').agg(mueve=('mv','mean'),toca_arriba=('mfe5',lambda x:(x>=5).mean()),toca_abajo=('mae5',lambda x:(x<=-5).mean())).mul(100).round(0).T.to_string())
