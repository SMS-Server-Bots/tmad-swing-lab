import pandas as pd,numpy as np,sys
sys.path.insert(0,'calib'); from outcome import first_touch
M=pd.read_pickle("calib/labeled.pkl")
print("mueve 5%% en 5 sesiones (cualquier sentido): %.1f%%"%(((M.mfe5>=5)|(M.mae5<=-5)).mean()*100))
print("toca +5%%: %.1f%% | toca -5%%: %.1f%%"%((M.mfe5>=5).mean()*100,(M.mae5<=-5).mean()*100))
for X,Y in [(5,5),(5,100),(3,3),(5,3),(3,5)]:
    for side in ['long','short']:
        p,w,a=first_touch(M,X,Y,side)
        print(f"{side:5s} T+{X} S-{Y}: gana(toca objetivo antes) {w.mean()*100:5.1f}%  ambiguo {a.mean()*100:4.1f}%  EV {p.mean():+.2f}%")
atr=M['atr_pct']
print("ATR% mediana",atr.median())
for m in [1,1.5,2]:
    for side in ['long','short']:
        p,w,a=first_touch(M,m*atr,m*atr,side); print(f"{side} ±{m}ATR: gana {w.mean()*100:.1f}% EV {p.mean():+.2f}%")
