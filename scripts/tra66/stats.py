import json,statistics as st
M=json.load(open('measurements.json'))
def med(xs): 
    xs=[x for x in xs if x is not None]
    return (round(st.median(xs),6) if xs else None, len(xs))
def winrate(xs):
    xs=[x for x in xs if x is not None]
    return (round(sum(1 for x in xs if x>0)/len(xs),4) if xs else None, len(xs))
PRIM=[r for r in M if r['kind']=='new_listing' and r['coverage_T-1h_T+4h']>=0.9]
print("=== 主標本（新規取扱・分単位T・海外参照あり・カバレッジ>=90%）:", len(PRIM))
for r in PRIM: print("   ",r['venue'],r['ticker'],r['announce_utc'][:16],"first_in_jp=",r['first_in_jp'])
keys=[f"{e}->{x}" for e in ['T+5m','T+15m','T+1h'] for x in ['T+4h','T+24h','T+72h','start']]
print("\n=== 全12(+start)通り: 中央値 / 勝率（コスト控除後・BTC超過） ===")
print(f"{'組合せ':18s} {'n':>3s} {'med_excess_net':>15s} {'win':>7s} {'med_net':>9s} {'med_gross':>10s}")
for k in keys:
    ex=[(r['returns'].get(k) or {}).get('excess_net') for r in PRIM]
    nt=[(r['returns'].get(k) or {}).get('net') for r in PRIM]
    gr=[(r['returns'].get(k) or {}).get('gross') for r in PRIM]
    m,n=med(ex); w,_=winrate(ex); mn,_=med(nt); mg,_=med(gr)
    print(f"{k:18s} {n:3d} {('%.4f'%m if m is not None else '  n/a'):>15s} {('%.3f'%w if w is not None else 'n/a'):>7s} {('%.4f'%mn if mn is not None else 'n/a'):>9s} {('%.4f'%mg if mg is not None else 'n/a'):>10s}")
print("\n=== 主指標（T+15分 → T+24h） ===")
k='T+15m->T+24h'
ex=[(r['returns'].get(k) or {}).get('excess_net') for r in PRIM]
m,n=med(ex); w,_=winrate(ex)
print(f"  n={n}  median_excess_net={m}  winrate={w}")
print(f"  合格線: 中央値>=+0.02 かつ 勝率>=0.60 かつ n>=20")
print(f"  判定: ", "判定不能（標本不足）" if n<20 else ("合格" if (m>=0.02 and w>=0.60) else ("不合格" if m<0.005 else "合格基準未達")))
print("\n=== 国内初上場で分けた主指標 ===")
for lab,sel in [('国内初上場=True',[r for r in PRIM if r['first_in_jp']]),('国内初上場=False',[r for r in PRIM if not r['first_in_jp']])]:
    ex=[(r['returns'].get(k) or {}).get('excess_net') for r in sel]
    m,n=med(ex); w,_=winrate(ex)
    print(f"  {lab}: n={n} median={m} winrate={w}")
print("\n=== 発表前の漏れ（T-24h→T-5分） ===")
pre=[r['pre_T-24h_to_T-5m']['excess'] for r in PRIM]
m,n=med(pre); w,_=winrate(pre)
print(f"  n={n} median_excess={m} winrate(>0)={w}")
print("  各件:", [(r['ticker'],r['pre_T-24h_to_T-5m']['excess']) for r in PRIM])
print("\n=== 参考: 除外した行 ===")
for r in M:
    if r not in PRIM: print("   ",r['venue'],r['ticker'],r['kind'],'cov=',r['coverage_T-1h_T+4h'],r['flags'])
