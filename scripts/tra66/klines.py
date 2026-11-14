import json,os,time,urllib.request,urllib.parse
CACHE='raw/kl'; os.makedirs(CACHE,exist_ok=True)
UA="Mozilla/5.0"
def _get(url):
    for attempt in range(5):
        try:
            r=urllib.request.Request(url,headers={'User-Agent':UA})
            return json.loads(urllib.request.urlopen(r,timeout=45).read())
        except Exception as e:
            if attempt==4: raise
            time.sleep(2*(attempt+1))
STEP={'1m':60000,'1h':3600000,'1d':86400000}
def fetch(symbol,start_ms,end_ms,interval='1m'):
    """Return list of klines covering [start_ms,end_ms), cached on disk."""
    out=[]; cur=start_ms
    while cur<end_ms:
        key=f"{CACHE}/{symbol}_{interval}_{cur}.json"
        if os.path.exists(key):
            ks=json.load(open(key))
        else:
            u=("https://api.binance.com/api/v3/klines?"+urllib.parse.urlencode(
                {'symbol':symbol,'interval':interval,'startTime':cur,'endTime':min(end_ms,cur+1000*STEP[interval]),'limit':1000}))
            ks=_get(u); json.dump(ks,open(key,'w')); time.sleep(0.25)
        if not ks: cur+=1000*STEP[interval]; continue
        out+=ks; cur=ks[-1][0]+STEP[interval]
    return out
def index(ks):
    return {k[0]:k for k in ks}
def price_at(idx,t_ms):
    """close of the 1m candle containing t_ms; (price, n_trades) or (None,None)"""
    ot=(t_ms//60000)*60000
    k=idx.get(ot)
    if not k: return None,None
    return float(k[4]), int(k[8])
