import json,sys
import measure
E=json.load(open('events.json'))
out=[]; btc_cache={}
targets=[e for e in E if e.get('time_precision')=='second']
print("events with minute-precision T:",len(targets),flush=True)
for e in targets:
    for a in e['assets']:
        if not a.get('ref'):
            print("skip (no ref):",e['id'],a['ticker'],flush=True); continue
        sym=a['ref_symbol']
        if a['ref']=='hyperliquid': sym=a.get('ref_symbol')
        try:
            r,_=measure.measure(e,{'ticker':a['ticker'],'ref':a['ref'],'ref_symbol':sym,
                                   'first_in_jp':a.get('first_in_jp')})
        except Exception as ex:
            print("ERR",e['id'],a['ticker'],ex,flush=True); continue
        r['kind']=e['kind']; r['listing_type']=e.get('listing_type')
        out.append(r)
        pm=r['returns'].get('T+15m->T+24h')
        print(f"{e['id']:34s} {a['ticker']:6s} cov={r['coverage_T-1h_T+4h']:.3f} "
              f"main_excess_net={(pm or {}).get('excess_net')} flags={r['flags']}",flush=True)
json.dump(out,open('measurements.json','w'),ensure_ascii=False,indent=1)
print("rows",len(out),flush=True)
