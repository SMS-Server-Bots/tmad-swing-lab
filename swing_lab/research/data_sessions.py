import pandas as pd
sess=[d.strftime('%Y-%m-%d') for d in pd.bdate_range('2026-07-20','2026-11-06') if d.strftime('%Y-%m-%d')!='2026-09-07']
si_map={d:i for i,d in enumerate(sess)}
