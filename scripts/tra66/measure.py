import json,datetime,os,urllib.request
import klines
COST=0.0029
ENTRIES=[('T+5m',300),('T+15m',900),('T+1h',3600)]
EXITS=[('T+4h',14400),('T+24h',86400),('T+72h',259200)]
LEVELS=[('T-24h',-86400),('T-1h',-3600),('T-5m',-300),('T',0),('T+5m',300),('T+15m',900),
        ('T+1h',3600),('T+4h',14400),('T+24h',86400),('T+72h',259200)]
def ms(iso):
    return int(datetime.datetime.fromisoformat(iso.replace('Z','+00:00')).timestamp()*1000)
HLCACHE='raw/hl'; os.makedirs(HLCACHE,exist_ok=True)
def hl_fetch(coin,a,b):
    key=f"{HLCACHE}/{coin}_{a}_{b}.json"
    if os.path.exists(key): return json.load(open(key))
    body=json.dumps({"type":"candleSnapshot","req":{"coin":coin,"interval":"1m","startTime":a,"endTime":b}}).encode()
    r=urllib.request.Request("https://api.hyperliquid.xyz/info",data=body,headers={'Content-Type':'application/json'})
    d=json.loads(urllib.request.urlopen(r,timeout=60).read())
    json.dump(d,open(key,'w')); return d
def series(ref,symbol,t0,t1):
    """returns idx: {minute_open_ms: (close, ntrades)}"""
    if ref=='binance':
        ks=klines.fetch(symbol,t0,t1)
        return {k[0]:(float(k[4]),int(k[8])) for k in ks}
    else:
        out={}
        cur=t0
        while cur<t1:
            end=min(t1,cur+5000*60000)
            d=hl_fetch(symbol,cur,end)
            if not d: break
            for x in d: out[x['t']]=(float(x['c']),int(x.get('n',0)))
            cur=d[-1]['t']+60000
        return out
def pat(idx,t_ms):
    ot=(t_ms//60000)*60000
    return idx.get(ot,(None,None))
def measure(ev,asset,btc_idx=None):
    T=ms(ev['announce_utc'])
    t0=T-86400000-600000; t1=T+259200000+600000
    idx=series(asset['ref'],asset['ref_symbol'],t0,t1)
    if btc_idx is None: btc_idx=series('binance','BTCUSDT',t0,t1)
    res={'event_id':ev['id'],'venue':ev['venue'],'ticker':asset['ticker'],
         'ref':asset['ref'],'ref_symbol':asset['ref_symbol'],
         'announce_utc':ev['announce_utc'],'first_in_jp':asset.get('first_in_jp'),
         'is_migration':ev.get('is_migration',False),'levels':{},'trades':{},'returns':{},'flags':[]}
    # coverage / tradability guard
    mins=[T+o*1000 for o in range(-3600,14400,60)]
    have=sum(1 for m in mins if pat(idx,m)[0] is not None)
    res['coverage_T-1h_T+4h']=round(have/len(mins),4)
    nt=[pat(idx,T+s*1000)[1] for s in range(0,900,60)]
    res['trades_T_to_T+15m']=sum(x for x in nt if x)
    if res['coverage_T-1h_T+4h']<0.9: res['flags'].append('coverage<90%')
    if not res['trades_T_to_T+15m']: res['flags'].append('no_trades_T..T+15m')
    for name,off in LEVELS:
        p,n=pat(idx,T+off*1000); res['levels'][name]=p; res['trades'][name]=n
    exits=list(EXITS)
    if ev.get('start_utc'):
        exits=exits+[('start',int((ms(ev['start_utc'])-T)/1000))]
    for en,eo in ENTRIES:
        pe,_=pat(idx,T+eo*1000)
        be,_=pat(btc_idx,T+eo*1000)
        for xn,xo in exits:
            px,_=pat(idx,T+xo*1000); bx,_=pat(btc_idx,T+xo*1000)
            k=f"{en}->{xn}"
            if pe and px:
                g=px/pe-1.0; net=g-COST
                btc=(bx/be-1.0) if (be and bx) else None
                res['returns'][k]={'gross':round(g,6),'net':round(net,6),
                    'btc':(round(btc,6) if btc is not None else None),
                    'excess_net':(round(net-btc,6) if btc is not None else None),
                    'exit_offset_s':xo}
            else:
                res['returns'][k]=None
    # pre-announcement drift T-24h -> T-5m
    a,_=pat(idx,T-86400000); b,_=pat(idx,T-300000)
    ab,_=pat(btc_idx,T-86400000); bb,_=pat(btc_idx,T-300000)
    res['pre_T-24h_to_T-5m']={'gross':(round(b/a-1,6) if a and b else None),
        'btc':(round(bb/ab-1,6) if ab and bb else None),
        'excess':(round((b/a-1)-(bb/ab-1),6) if (a and b and ab and bb) else None)}
    return res,btc_idx

def hl_fetch_iv(coin,a,b,iv):
    key=f"{HLCACHE}/{coin}_{iv}_{a}_{b}.json"
    if os.path.exists(key): return json.load(open(key))
    body=json.dumps({"type":"candleSnapshot","req":{"coin":coin,"interval":iv,"startTime":a,"endTime":b}}).encode()
    r=urllib.request.Request("https://api.hyperliquid.xyz/info",data=body,headers={'Content-Type':'application/json'})
    d=json.loads(urllib.request.urlopen(r,timeout=60).read())
    json.dump(d,open(key,'w')); return d
def hl_series_1h(coin,t0,t1):
    """1h candles mapped onto every minute of the hour (hour-granularity fallback)."""
    d=hl_fetch_iv(coin,t0,t1,'1h'); out={}
    for x in d:
        c=float(x['c']); n=int(x.get('n',0)); h=x['t']
        for k in range(60): out[h+k*60000]=(c,n)
    return out
