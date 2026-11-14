import json,datetime,statistics as st
import measure, klines
E=json.load(open('events.json'))
def ms(d,hh=0,mm=0):
    return int(datetime.datetime(int(d[:4]),int(d[5:7]),int(d[8:10]),hh,mm,tzinfo=datetime.timezone.utc).timestamp()*1000)
COST=0.0029
rows=[]
for e in E:
    if e.get('time_precision')=='second': continue
    d=e.get('announce_date_jst') or (e.get('announce_jst_hour') or '')[:10]
    if not d or len(d)<10: continue
    # JST day boundaries in UTC: 00:00 JST = prev day 15:00 UTC
    t_open = ms(d)-9*3600000           # 00:00 JST of announcement day
    t_close= t_open+86400000           # 24:00 JST of announcement day
    for a in e['assets']:
        if not a.get('ref'): continue
        sym=a['ref_symbol']
        if a['ref']=='hyperliquid':
            idx=measure.hl_series_1h(sym,t_open-86400000-600000,t_close+259200000+600000)
        else:
            idx=measure.series(a['ref'],sym,t_open-86400000-600000,t_close+259200000+600000)
        bidx=measure.series('binance','BTCUSDT',t_open-86400000-600000,t_close+259200000+600000)
        P=lambda t: measure.pat(idx,t)[0]; B=lambda t: measure.pat(bidx,t)[0]
        def ret(p0,p1):
            a0,a1,b0,b1=P(p0),P(p1),B(p0),B(p1)
            if not(a0 and a1): return None
            g=a1/a0-1; net=g-COST
            btc=(b1/b0-1) if (b0 and b1) else None
            return {'gross':round(g,6),'net':round(net,6),'btc':(round(btc,6) if btc is not None else None),
                    'excess_net':(round(net-btc,6) if btc is not None else None)}
        rows.append({'event_id':e['id'],'venue':e['venue'],'ticker':a['ticker'],'kind':e['kind'],
            'announce_date_jst':d,'time_precision':e['time_precision'],'first_in_jp':a.get('first_in_jp'),
            'ref':a['ref'],'ref_symbol':sym,
            'day0_00to24JST':ret(t_open,t_close),
            'pre_day':ret(t_open-86400000,t_open),
            'endday->+1d':ret(t_close,t_close+86400000),
            'endday->+3d':ret(t_close,t_close+259200000)})
        print(rows[-1]['event_id'],a['ticker'],'day0=',(rows[-1]['day0_00to24JST'] or {}).get('excess_net'),
              'end->1d=',(rows[-1]['endday->+1d'] or {}).get('excess_net'),flush=True)
json.dump(rows,open('measurements_dateonly.json','w'),ensure_ascii=False,indent=1)
print("rows",len(rows))
