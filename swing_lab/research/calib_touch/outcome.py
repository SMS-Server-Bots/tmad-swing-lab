import numpy as np,pandas as pd
def first_touch(M, X, Y, side='long', N=5):
    """X,Y: arrays or scalars in %. Returns payoff % (target +X, stop -Y, else close at N), win flag, ambiguous flag."""
    X=np.broadcast_to(np.asarray(X,dtype=float),(len(M),)); Y=np.broadcast_to(np.asarray(Y,dtype=float),(len(M),))
    e=M.open1.values
    tk=np.full(len(M),99); sk=np.full(len(M),99)
    for k in range(N,0,-1):
        h=(M[f'h{k}'].values/e-1)*100; l=(M[f'l{k}'].values/e-1)*100
        if side=='long': hitT=h>=X; hitS=l<=-Y
        else: hitT=l<=-X; hitS=h>=Y
        tk=np.where(hitT,k,tk); sk=np.where(hitS,k,sk)
    c=(M[f'c{N}'].values/e-1)*100
    if side=='short': c=-c
    win=tk<sk; loss=sk<=tk  # same-day both -> loss (conservative)
    pay=np.where(tk<sk,X,np.where(sk<99,-Y,c))
    pay=np.where((tk==99)&(sk==99),c,pay)
    return pay, (tk<sk), (tk==sk)&(tk<99)
