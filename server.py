from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import socket, sys

class QuietServer(ThreadingHTTPServer):
    """Same as ThreadingHTTPServer, except it doesn't print a scary traceback when a browser tab just cancels a poll mid-response (normal when a page refreshes or a newer request replaces an older one) - a real error still prints normally."""
    def handle_error(self,request,client_address):
        exc=sys.exc_info()[1]
        if isinstance(exc,(ConnectionAbortedError,ConnectionResetError,BrokenPipeError,socket.timeout)): return
        super().handle_error(request,client_address)
from pathlib import Path
import json, math, random, csv, io, time, os, sqlite3, ctypes, hmac, threading
from contextlib import closing
from datetime import date, datetime, time as dt_time, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
# Cloud: point MIRA_DATA_DIR at a persistent disk (e.g. /var/data on Render) so learning survives restarts and redeploys.
_pd=os.environ.get("MIRA_DATA_DIR","").strip()
if _pd and Path(_pd).resolve()!=DATA_DIR.resolve():
    import shutil
    _pd=Path(_pd); _pd.mkdir(parents=True,exist_ok=True)
    if DATA_DIR.exists():
        for _src in DATA_DIR.rglob("*"):
            _dst=_pd/_src.relative_to(DATA_DIR)
            if _src.is_dir(): _dst.mkdir(parents=True,exist_ok=True)
            elif not _dst.exists(): _dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(_src,_dst)   # first run only: seed; never overwrite learned files
    DATA_DIR=_pd
MODEL = None
TASK = "bank"
ACTIVE_CSV = ""
ACTIVE_TARGET = ""
LAST_RESULT = None
OLLAMA_MODEL = "qwen3.5:4b"
NASDAQ_RESULT = None
ENGINE_STATUS = {"state":"idle","detail":"Ready for a local research task.","progress":0.0,"events":[],"network":None}
MARKET_API_KEY = os.environ.get("ALPACA_API_KEY", "")
MARKET_API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
MARKET_CACHE = {"at":0.0,"data":None}

def credential_db_path():
    base=Path(os.environ.get("LOCALAPPDATA",str(Path.home()/"AppData"/"Local")))
    folder=base/"MIRA"
    folder.mkdir(parents=True,exist_ok=True)
    return folder/"credentials.sqlite3"

class _DataBlob(ctypes.Structure):
    _fields_=[("cbData",ctypes.c_uint32),("pbData",ctypes.POINTER(ctypes.c_ubyte))]

def _dpapi(data, protect):
    if os.name!="nt": return data  # cloud (Linux): no DPAPI; prefer ALPACA_API_KEY/ALPACA_API_SECRET env vars
    crypt=ctypes.WinDLL("Crypt32.dll",use_last_error=True); kernel=ctypes.WinDLL("Kernel32.dll",use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]; kernel.LocalFree.restype=ctypes.c_void_p
    source_buf=(ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    source=_DataBlob(len(data),ctypes.cast(source_buf,ctypes.POINTER(ctypes.c_ubyte))); output=_DataBlob()
    if protect:
        fn=crypt.CryptProtectData
        fn.argtypes=[ctypes.POINTER(_DataBlob),ctypes.c_wchar_p,ctypes.POINTER(_DataBlob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(_DataBlob)]
        ok=fn(ctypes.byref(source),"MIRA market-data credentials",None,None,None,1,ctypes.byref(output))
    else:
        description=ctypes.c_wchar_p(); fn=crypt.CryptUnprotectData
        fn.argtypes=[ctypes.POINTER(_DataBlob),ctypes.POINTER(ctypes.c_wchar_p),ctypes.POINTER(_DataBlob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(_DataBlob)]
        ok=fn(ctypes.byref(source),ctypes.byref(description),None,None,None,1,ctypes.byref(output))
        if description: kernel.LocalFree(description)
    if not ok: raise ctypes.WinError(ctypes.get_last_error())
    try: return ctypes.string_at(output.pbData,output.cbData)
    finally: kernel.LocalFree(output.pbData)

def save_market_credentials(key,secret):
    protected_key=_dpapi(key.encode("utf-8"),True); protected_secret=_dpapi(secret.encode("utf-8"),True)
    with closing(sqlite3.connect(credential_db_path())) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS credentials (name TEXT PRIMARY KEY, value BLOB NOT NULL)")
            db.executemany("INSERT INTO credentials(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",[("alpaca_key",protected_key),("alpaca_secret",protected_secret)])

def load_market_credentials():
    try:
        with closing(sqlite3.connect(credential_db_path())) as db:
            row=db.execute("SELECT name,value FROM credentials WHERE name IN ('alpaca_key','alpaca_secret')").fetchall()
        values={name:_dpapi(value,False).decode("utf-8") for name,value in row}
        return values.get("alpaca_key",""),values.get("alpaca_secret","")
    except (OSError,sqlite3.Error,ValueError): return "",""

def clear_market_credentials():
    path=credential_db_path()
    try:
        with closing(sqlite3.connect(path)) as db:
            with db: db.execute("DROP TABLE IF EXISTS credentials")
            db.execute("VACUUM")
        path.unlink(missing_ok=True)
    except (OSError,sqlite3.Error): pass

if not MARKET_API_KEY or not MARKET_API_SECRET:
    MARKET_API_KEY,MARKET_API_SECRET=load_market_credentials()

def visible_status(stage, detail, progress=None, network=None):
    global ENGINE_STATUS
    events=list(ENGINE_STATUS.get("events",[]))
    events.append({"time":time.strftime("%H:%M:%S"),"stage":stage,"detail":str(detail)[:220]})
    ENGINE_STATUS.update({"state":stage,"detail":str(detail)[:220],"events":events[-50:]})
    if progress is not None: ENGINE_STATUS["progress"]=max(0.0,min(1.0,float(progress)))
    if network is not None: ENGINE_STATUS["network"]=network

def nth_sunday(year, month, n):
    first=date(year,month,1)
    first_sunday=1+(6-first.weekday())%7
    return date(year,month,first_sunday+7*(n-1))

def eastern_now(now_utc=None):
    """Eastern time without an external tzdata dependency (works on Windows)."""
    utc=now_utc or datetime.now(timezone.utc)
    if utc.tzinfo is None: utc=utc.replace(tzinfo=timezone.utc)
    start=datetime.combine(nth_sunday(utc.year,3,2),dt_time(7),tzinfo=timezone.utc)
    end=datetime.combine(nth_sunday(utc.year,11,1),dt_time(6),tzinfo=timezone.utc)
    daylight=start<=utc<end
    zone=timezone(timedelta(hours=-4 if daylight else -5),"EDT" if daylight else "EST")
    return utc.astimezone(zone)

def market_session(now=None):
    now = now or eastern_now()
    weekday, clock = now.weekday(), now.time().replace(tzinfo=None)
    if weekday == 5: return "closed"
    if weekday == 6:
        if clock < dt_time(20): return "closed"
        return "overnight"
    if dt_time(0) <= clock < dt_time(4): return "overnight"
    if dt_time(4) <= clock < dt_time(9,30): return "pre-market"
    if dt_time(9,30) <= clock < dt_time(16): return "regular session"
    if dt_time(16) <= clock < dt_time(20): return "after-hours"
    if weekday < 4 and clock >= dt_time(20): return "overnight"
    return "closed"


MARKET_STREAM={"connected":False,"status":"not started","trade":None,"quote":None,
               "bar":None,"bars":[],"last_message_at":None,"last_error":None}
MARKET_BARS_CACHE={"at":0.0,"bars":[]}
MARKET_STREAM_LOCK=threading.Lock()
MARKET_STREAM_STARTED=False

def market_stream_snapshot():
    """Return a fresh regular-session IEX stream snapshot, otherwise None."""
    if market_session()!="regular session":
        return None
    with MARKET_STREAM_LOCK:
        trade=dict(MARKET_STREAM.get("trade") or {})
        quote=dict(MARKET_STREAM.get("quote") or {})
        connected=MARKET_STREAM.get("connected",False)
        stream_bars=[dict(b) for b in MARKET_STREAM.get("bars",[])][-240:]
    if not connected or not trade.get("t") or trade.get("p") is None:
        return None
    try:
        stamp=datetime.fromisoformat(str(trade["t"]).replace("Z","+00:00"))
        age=max(0,int((datetime.now(stamp.tzinfo)-stamp).total_seconds()))
        price=float(trade["p"])
        if age>10 or price<=0 or not math.isfinite(price): return None
    except (ValueError,TypeError):
        return None
    previous=None
    cached=MARKET_CACHE.get("data") or {}
    try: previous=float(cached.get("previous_close")) if cached.get("previous_close") else None
    except (TypeError,ValueError): previous=None
    change=(price/previous-1) if previous and previous>0 else None
    return {"connected":True,"configured":True,"provider":"Alpaca IEX WebSocket · live single-venue stream",
            "feed":"iex-websocket","symbol":"QQQ","instrument":"Invesco QQQ ETF · Nasdaq-100 proxy",
            "session":"regular session","freshness":"LIVE" if age<=3 else "RECENT",
            "last":price,"price_label":"Latest streamed trade","trade_at":trade.get("t"),
            "age_seconds":age,"bid":quote.get("bp"),"ask":quote.get("ap"),
            "quote_at":quote.get("t"),"quote_age_seconds":None,
            "day_change_pct":change,"previous_close":previous,
            "market_time":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "streaming":True,"minute_bars":stream_bars,"minute_bar":stream_bars[-1] if stream_bars else None,
            "disclaimer":"Live IEX single-venue quote/trade stream. It is not a consolidated all-exchange feed and is not itself a new model forecast."}

def start_market_stream():
    """Start an optional WebSocket feed. Requires websocket-client; REST polling remains the fallback."""
    global MARKET_STREAM_STARTED
    if MARKET_STREAM_STARTED: return
    if not (MARKET_API_KEY and MARKET_API_SECRET):
        MARKET_STREAM["status"]="waiting for Alpaca credentials"; return
    MARKET_STREAM_STARTED=True
    def worker():
        try:
            import websocket
        except ImportError:
            MARKET_STREAM["status"]="websocket-client not installed; using Alpaca REST polling"
            MARKET_STREAM["last_error"]="Install websocket-client to enable the optional stream."
            return
        url="wss://stream.data.alpaca.markets/v2/iex"
        while True:
            try:
                def on_open(ws):
                    MARKET_STREAM["status"]="authenticating"
                    ws.send(json.dumps({"action":"auth","key":MARKET_API_KEY,"secret":MARKET_API_SECRET}))
                def on_message(ws,message):
                    try: items=json.loads(message)
                    except (TypeError,json.JSONDecodeError): return
                    if isinstance(items,dict): items=[items]
                    for item in items:
                        kind=item.get("T")
                        if kind=="success" and item.get("msg")=="authenticated":
                            ws.send(json.dumps({"action":"subscribe","trades":["QQQ"],"quotes":["QQQ"],"bars":["QQQ"]}))
                            MARKET_STREAM["status"]="authenticated; subscribing"
                        elif kind=="subscription":
                            MARKET_STREAM["connected"]=True
                            MARKET_STREAM["status"]="subscribed to QQQ trades, quotes and minute bars"
                        elif kind=="t":
                            with MARKET_STREAM_LOCK:
                                MARKET_STREAM["trade"]={"t":item.get("t"),"p":item.get("p"),"s":item.get("s")}
                                MARKET_STREAM["last_message_at"]=datetime.now(timezone.utc).isoformat(timespec="seconds")
                            MARKET_STREAM["connected"]=True
                        elif kind=="q":
                            with MARKET_STREAM_LOCK:
                                MARKET_STREAM["quote"]={"t":item.get("t"),"bp":item.get("bp"),"ap":item.get("ap")}
                                MARKET_STREAM["last_message_at"]=datetime.now(timezone.utc).isoformat(timespec="seconds")
                            MARKET_STREAM["connected"]=True
                        elif kind in ("b","u"):
                            bar={"t":item.get("t"),"o":item.get("o"),"h":item.get("h"),
                                 "l":item.get("l"),"c":item.get("c"),"v":item.get("v")}
                            with MARKET_STREAM_LOCK:
                                MARKET_STREAM["bar"]=bar
                                bars=MARKET_STREAM.setdefault("bars",[])
                                # Alpaca 'u' updates the current minute; 'b' starts a new minute.
                                if bars and bars[-1].get("t")==bar.get("t"):
                                    bars[-1]=bar
                                else:
                                    bars.append(bar)
                                if len(bars)>480: del bars[:-480]
                                MARKET_STREAM["last_message_at"]=datetime.now(timezone.utc).isoformat(timespec="seconds")
                def on_error(ws,error):
                    MARKET_STREAM["last_error"]=str(error)[:180]
                def on_close(ws,code,message):
                    MARKET_STREAM["connected"]=False
                    MARKET_STREAM["status"]="disconnected; reconnecting"
                app=websocket.WebSocketApp(url,on_open=on_open,on_message=on_message,
                                           on_error=on_error,on_close=on_close)
                app.run_forever(ping_interval=20,ping_timeout=10)
            except Exception as exc:
                MARKET_STREAM["connected"]=False
                MARKET_STREAM["last_error"]=str(exc)[:180]
                MARKET_STREAM["status"]="stream reconnect pending"
            time.sleep(3)
    threading.Thread(target=worker,daemon=True).start()
    MARKET_STREAM["status"]="stream worker started"

def fetch_market_snapshot(force=False):
    global MARKET_CACHE
    if not MARKET_API_KEY or not MARKET_API_SECRET:
        return {"connected":False,"configured":False,"provider":"Alpaca Basic · free IEX","symbol":"QQQ","error":"Connect free Alpaca market-data keys to enable live quotes."}
    if not force:
        streamed=market_stream_snapshot()
        if streamed:
            MARKET_CACHE={"at":time.time(),"data":streamed}
            return streamed
        if MARKET_CACHE["data"] and time.time()-MARKET_CACHE["at"] < 3:
            return MARKET_CACHE["data"]
    now=eastern_now(); session=market_session(now)
    feed="overnight" if session=="overnight" else "iex"
    req=Request("https://data.alpaca.markets/v2/stocks/QQQ/snapshot?feed="+feed,headers={"APCA-API-KEY-ID":MARKET_API_KEY,"APCA-API-SECRET-KEY":MARKET_API_SECRET,"Accept":"application/json"})
    try:
        with urlopen(req,timeout=8) as response: raw=json.loads(response.read())
    except HTTPError as exc:
        code=exc.code
        message="Alpaca rejected the key or free-feed entitlement (HTTP %d)."%code if code in (401,403) else "Market-data provider returned HTTP %d."%code
        return {"connected":False,"configured":True,"provider":"Alpaca Basic","symbol":"QQQ","error":message}
    except (URLError,TimeoutError) as exc:
        reason=exc.reason if isinstance(exc,URLError) else exc
        return {"connected":False,"configured":True,"provider":"Alpaca Basic","symbol":"QQQ","error":"Could not reach Alpaca market data: "+str(reason)}
    except Exception:
        return {"connected":False,"configured":True,"provider":"Alpaca Basic","symbol":"QQQ","error":"The market-data response could not be read."}
    trade=raw.get("latestTrade") or {}; quote=raw.get("latestQuote") or {}
    day=raw.get("dailyBar") or {}; prev=raw.get("prevDailyBar") or {}
    stamp=trade.get("t"); quote_stamp=quote.get("t"); age=None; quote_age=None
    for value, key in ((stamp,"trade"),(quote_stamp,"quote")):
        if value:
            try:
                parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
                seconds=max(0,int((datetime.now(parsed.tzinfo)-parsed).total_seconds()))
                if key=="trade": age=seconds
                else: quote_age=seconds
            except (ValueError,TypeError): pass
    feed_note="Overnight indicative quotes · trades delayed 15 min" if feed=="overnight" else "Real-time IEX single-venue feed"
    age_for_status=quote_age if feed=="overnight" else age
    freshness="LIVE" if session!="closed" and age_for_status is not None and age_for_status<=90 else ("RECENT" if age_for_status is not None and age_for_status<=900 else ("CLOSED" if session=="closed" else "STALE"))
    trade_price=trade.get("p"); bid=quote.get("bp"); ask=quote.get("ap"); previous=prev.get("c")
    display_price=((float(bid)+float(ask))/2) if feed=="overnight" and bid is not None and ask is not None else trade_price
    change=(float(display_price)/float(previous)-1) if display_price is not None and previous not in (None,0) else None
    result={"connected":True,"configured":True,"provider":"Alpaca Basic · "+feed_note,"feed":feed,"symbol":"QQQ","instrument":"Invesco QQQ ETF · Nasdaq-100 proxy","session":session,"freshness":freshness,"last":display_price,"price_label":"Indicative midpoint" if feed=="overnight" else "Latest trade","trade_at":stamp,"age_seconds":age,"bid":bid,"ask":ask,"quote_at":quote_stamp,"quote_age_seconds":quote_age,"day_change_pct":change,"day_high":day.get("h"),"day_low":day.get("l"),"previous_close":previous,"market_time":now.isoformat(timespec="seconds"),"disclaimer":"Quote feed is separate from the daily-trained neural forecast; the model does not use this intraday quote to generate its probability."}
    MARKET_CACHE={"at":time.time(),"data":result}
    return result

def make_case(kind, ident):
    x = [random.uniform(-1, 1) for _ in range(4)]
    if kind == "bank":
        y = int(x[0] > .62 or (x[1] > .73 and x[2] > .15) or x[3] < -.82)
    else:
        y = int(.6*x[0] - .35*x[1] + .3*x[2] + .18*x[3] + random.uniform(-.4,.4) > 0)
    return {"x": x, "y": y, "id": ident}

def sigmoid(z):
    z = max(-25, min(25, z))
    return 1 / (1 + math.exp(-z))

def forward(model, x):
    hidden = [math.tanh(w[0] + sum(a*b for a,b in zip(x,w[1:]))) for w in model["w1"]]
    p = sigmoid(model["w2"][0] + sum(a*b for a,b in zip(hidden,model["w2"][1:])))
    return hidden, p

def predict(model, x):
    members=model.get("members") if isinstance(model,dict) else None
    if members: return sum(forward(member,x)[1] for member in members)/len(members)
    return forward(model,x)[1]

def cross_entropy(model, data):
    total = 0
    for o in data:
        p = max(1e-7, min(1-1e-7, predict(model,o["x"])))
        total -= o["y"]*math.log(p) + (1-o["y"])*math.log(1-p)
    return total / max(1,len(data))

def train_pass(model, data, rate):
    for o in data:
        x, y = o["x"], o["y"]
        hidden, p = forward(model,x)
        delta = p-y
        # Preserve the pre-update output weights for the hidden-layer gradient.
        old_out = model["w2"][1:].copy()
        model["w2"][0] -= rate*delta
        for j in range(len(hidden)):
            model["w2"][j+1] -= rate*delta*hidden[j]
            d = delta*old_out[j]*(1-hidden[j]*hidden[j])
            model["w1"][j][0] -= rate*d
            for k,v in enumerate(x):
                model["w1"][j][k+1] -= rate*d*v

def evaluate(model, data, threshold=0.5):
    c = {"tp":0,"fp":0,"tn":0,"fn":0}
    trace = []
    for o in data:
        p = predict(model,o["x"]); pred = int(p >= threshold)
        c["tp" if o["y"] and pred else "fn" if o["y"] else "fp" if pred else "tn"] += 1
        trace.append({"id":o["id"],"x":[round(v,3) for v in o["x"]],"actual":o["y"],"p":round(p,5),"result":"OK" if pred==o["y"] else "ERROR"})
    n=len(data); div=lambda a,b:a/b if b else 0
    return {"n":n,"counts":c,"threshold":threshold,"accuracy":div(c["tp"]+c["tn"],n),"precision":div(c["tp"],c["tp"]+c["fp"]),"recall":div(c["tp"],c["tp"]+c["fn"]),"fpr":div(c["fp"],c["fp"]+c["tn"]),"fnr":div(c["fn"],c["fn"]+c["tp"]),"trace":trace}

def choose_threshold_f2(model, validation):
    """Choose a high-recall decision cutoff using validation data only."""
    positives=sum(row["y"] for row in validation)
    if positives==0: return 0.5, {"f2":0.0,"validation_positive_rows":0}
    best_threshold=0.5; best_score=-1.0; best_metrics=None
    for step in range(200,-1,-1):
        threshold=step/200
        scored=evaluate(model,validation,threshold)
        precision,recall=scored["precision"],scored["recall"]
        denom=4*precision+recall
        f2=(5*precision*recall/denom) if denom else 0.0
        if f2>best_score:
            best_score,best_threshold,best_metrics=f2,threshold,scored
    return best_threshold,{"f2":best_score,"validation_positive_rows":positives,"validation_precision":best_metrics["precision"],"validation_recall":best_metrics["recall"]}

def train(payload):
    global MODEL, TASK, LAST_RESULT
    visible_status("preparing","Preparing labeled examples for the sandbox network.",0.02)
    TASK = payload.get("task","bank")
    size=max(200,min(5000,int(payload.get("size",1200))))
    epochs=max(10,min(200,int(payload.get("epochs",50))))
    random.seed()
    source=payload.get("csv","")
    if source.strip():
        reader=csv.DictReader(io.StringIO(source.lstrip("\ufeff")))
        if reader.fieldnames: reader.fieldnames=[name.strip() for name in reader.fieldnames]
        records=list(reader)
        target=payload.get("target") or (reader.fieldnames[-1] if reader.fieldnames else "")
        if not records or target not in (reader.fieldnames or []): raise ValueError("CSV needs headers, data rows, and a selected target column.")
        features=[]
        for name in reader.fieldnames:
            if name==target: continue
            try: float(next(row[name] for row in records if row.get(name,"")))
            except (ValueError,StopIteration,TypeError): continue
            features.append(name)
        if not features: raise ValueError("CSV needs at least one numeric feature column.")
        data=[]
        for i,row in enumerate(records):
            try:
                label=float(row[target]); x=[float(row[k]) for k in features]
                if label in (0,1) and all(math.isfinite(v) for v in x): data.append({"x":x,"y":int(label),"id":i+1})
            except (ValueError,TypeError,KeyError): pass
        if len(data)<20: raise ValueError("Need at least 20 complete rows with target labels 0 or 1.")
        TASK="uploaded CSV · target="+target
    else:
        data=[make_case(TASK,i) for i in range(size)]
    cut=int(len(data)*.8); training=data[:cut]; test=data[cut:]
    if not test: raise ValueError("Dataset is too small for a holdout split.")
    dimensions=len(training[0]["x"])
    means=[sum(row["x"][j] for row in training)/len(training) for j in range(dimensions)]
    scales=[math.sqrt(sum((row["x"][j]-means[j])**2 for row in training)/len(training)) or 1 for j in range(dimensions)]
    for row in data: row["x"]=[(v-means[j])/scales[j] for j,v in enumerate(row["x"])]
    model={"w1":[[random.uniform(-.3,.3) for _ in range(dimensions+1)] for _ in range(8)],"w2":[random.uniform(-.3,.3) for _ in range(9)]}
    logs=[]
    step=max(1,epochs//20)
    for epoch in range(epochs):
        random.shuffle(training)
        train_pass(model,training,.025/(1+epoch*.01))
        if epoch % step == 0 or epoch == epochs-1:
            logs.append({"epoch":epoch+1,"loss":cross_entropy(model,test)})
        if epoch % max(1,epochs//12)==0 or epoch==epochs-1:
            visible_status("training","Sandbox MLP · epoch %d/%d · holdout scoring"%(epoch+1,epochs),0.05+0.9*(epoch+1)/epochs,{"label":TASK,"epoch":epoch+1,"epochs":epochs,"model":model,"sample":test[0]["x"]})
    MODEL=model
    result=evaluate(model,test)
    result["logs"]=logs
    result["environment"]=TASK
    result["architecture"]=str(dimensions)+" inputs → 8 tanh hidden units → 1 sigmoid output"
    result["train_rows"]=len(training)
    return result

def scan():
    if TASK.startswith("uploaded CSV"):
        raise ValueError("Sandbox exploration is available only in generated environments. Review uploaded data in the holdout trace.")
    if MODEL is None: raise ValueError("Train the Python model first.")
    data=[make_case(TASK,"S%03d"%(i+1)) for i in range(100)]
    result=evaluate(MODEL,data)
    result["environment"]=TASK
    return result


TOOLS = [
 {"type":"function","function":{"name":"inspect_dataset","description":"Inspect current CSV schema, row count, binary target counts, and invalid or missing rows. Does not modify data.","parameters":{"type":"object","properties":{},"required":[]}}},
 {"type":"function","function":{"name":"train_predictor","description":"Train the local Python neural network on the supplied labeled CSV, or the selected generated sandbox if no CSV is supplied.","parameters":{"type":"object","properties":{},"required":[]}}},
 {"type":"function","function":{"name":"inspect_holdout","description":"Read metrics from the last held-out evaluation, including confusion counts, false-positive and false-negative rates.","parameters":{"type":"object","properties":{},"required":[]}}},
 {"type":"function","function":{"name":"inspect_market_evaluation","description":"Use for questions about MIRA Nasdaq forecast accuracy, market-model false-positive rates, false negatives, baselines, or holdout scores. Returns the real chronological market-model holdout metrics, not synthetic sandbox results.","parameters":{"type":"object","properties":{},"required":[]}}},
 {"type":"function","function":{"name":"forecast_nasdaq","description":"Train/evaluate next-session direction and 20-session drawdown-risk models on bundled Nasdaq-100 daily history, or on supplied historical prices. Produces an experimental latest-data signal.","parameters":{"type":"object","properties":{},"required":[]}}},
 {"type":"function","function":{"name":"explore_simulation","description":"Run 100 new generated cases in the simulated bank or market environment. Never accesses external systems.","parameters":{"type":"object","properties":{},"required":[]}}},
 {"type":"function","function":{"name":"forecast_overnight_intraday","description":"Tests whether MIRA's QQQ return has come mainly from overnight holding or intraday trading, on a chronological holdout (not hindsight). Use for questions about day-trading, intraday edge, or whether trading during the day adds value.","parameters":{"type":"object","properties":{},"required":[]}}}
]

def inspect_market_evaluation():
    report_path=DATA_DIR/"models"/"nasdaq100_training_report.json"
    try: report=json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return {"error":"No saved market evaluation is available. Run a fresh market evaluation first."}
    def compact(task):
        return {key:task.get(key) for key in ("holdout_accuracy","majority_baseline_accuracy","precision","recall","false_positive_rate","false_negative_rate") if key in task}
    metrics={"instrument":report.get("instrument"),"as_of":report.get("data_as_of"),"holdout_rows":report.get("holdout_rows"),"direction":compact(report.get("direction",{})),"stress_drawdown":compact(report.get("stress",{})),"volatility_regime":compact(report.get("volatility_regime",{})),"warning":"These are three different prediction tasks. False-positive rate is FP/(FP+TN). Do not substitute synthetic sandbox metrics."}
    return metrics

def parse_date(value):
    from datetime import datetime
    value=value.strip()
    try: return datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError:
        for fmt in ("%m/%d/%Y","%Y%m%d","%d-%b-%Y"):
            try: return datetime.strptime(value,fmt)
            except ValueError: pass
    raise ValueError("Could not parse date: "+value)


def _clip_probability(p):
    return min(1.0-1e-6,max(1e-6,float(p)))

def _logit(p):
    p=_clip_probability(p)
    return math.log(p/(1.0-p))

def _sigmoid_safe(z):
    z=max(-30.0,min(30.0,float(z)))
    return 1.0/(1.0+math.exp(-z))

def _logloss(ps,ys):
    if not ps: return None
    return -sum(y*math.log(_clip_probability(p))+(1-y)*math.log(1-_clip_probability(p))
                for p,y in zip(ps,ys))/len(ps)

def _fit_platt(probabilities,labels,steps=1800,rate=0.025):
    """Fit a two-parameter logistic calibrator on a chronological calibration slice."""
    if len(probabilities)<30 or len(set(labels))<2:
        return {"slope":1.0,"intercept":0.0,"fitted":False}
    xs=[_logit(p) for p in probabilities]; a=1.0; b=0.0; n=len(xs)
    for _ in range(steps):
        ga=0.0; gb=0.0
        for x,y in zip(xs,labels):
            e=_sigmoid_safe(a*x+b)-y
            ga+=e*x; gb+=e
        # Mild regularization keeps tiny validation samples from producing extreme slopes.
        a-=rate*(ga/n+0.01*(a-1.0))
        b-=rate*(gb/n+0.01*b)
    return {"slope":a,"intercept":b,"fitted":True}

def _apply_platt(p,cal):
    return _sigmoid_safe(cal.get("slope",1.0)*_logit(p)+cal.get("intercept",0.0))

def _trend_bucket_probabilities(rows, labels, trend_states, alpha=12.0):
    """Beta-smoothed P(up | trend state), fitted only on the calibration slice."""
    out={}
    for state in (0,1):
        ys=[y for y,s in zip(labels,trend_states) if s==state]
        out[str(state)]=(sum(ys)+alpha*0.5)/(len(ys)+alpha)
    return out

def _fuse_probabilities(p_model,p_trend,p_base,weights):
    wm=float(weights.get("direction",0.0)); wt=float(weights.get("trend",0.0))
    total=wm+wt
    if total<=0: return _clip_probability(p_base)
    wm/=total; wt/=total
    base=_logit(p_base)
    return _sigmoid_safe(base+wm*(_logit(p_model)-base)+wt*(_logit(p_trend)-base))

def _block_edge_interval(predictions,labels,baseline_accuracy,block=5,reps=600,seed=2718):
    """Paired moving-block bootstrap for accuracy edge vs majority baseline."""
    n=len(labels)
    if n<40: return None
    rng=random.Random(seed); starts=list(range(max(1,n-block+1)))
    vals=[]
    for _ in range(reps):
        sampled=[]
        while len(sampled)<n:
            st=rng.choice(starts)
            sampled.extend(range(st,min(n,st+block)))
        sampled=sampled[:n]
        acc=sum(int(predictions[i]==labels[i]) for i in sampled)/n
        vals.append(acc-baseline_accuracy)
    vals.sort()
    return [vals[int(0.025*(reps-1))],vals[int(0.975*(reps-1))]]

def build_directional_fusion(eval_model,valid_rows,test_rows,raw_validation,raw_test,
                             close,full_model,latest_features):
    """
    Combine the independently trained directional MLP and a long/defensive
    trend state. Calibration/weights are fitted on an early validation slice;
    model selection is judged on the later untouched holdout. Drawdown models
    are deliberately not treated as directional votes.
    """
    if len(valid_rows)<60 or len(test_rows)<40:
        return {"available":False,"edge_validated":False,"signal":"HOLD / INSUFFICIENT VALIDATION",
                "reason":"Not enough chronological validation and holdout observations to validate a fused directional signal."}
    nval=len(valid_rows); cut=max(30,int(nval*0.60))
    if nval-cut<20: cut=nval-20
    cal_rows=valid_rows[:cut]; score_rows=valid_rows[cut:]
    cal_raw=raw_validation[:cut]; score_raw=raw_validation[cut:]
    # Fixed, interpretable trend rule. Its weights are learned from the same
    # calibration slice and evaluated only on later validation/holdout rows.
    trend_n=min(200,max(100,len(close)//4))
    trend_pos=trend_positions(close,trend_n,0.01)
    # The caller's raw rows carry dates; map them back to the price index.
    # `close_dates` is attached below by the caller as row["index"] when possible.
    # Fallback mapping uses the training example order, which is chronological.
    def state_for(row,offset):
        idx=row.get("_price_index")
        if idx is None:
            # Each example begins at price index 20 and then advances one row.
            idx=20+offset
        if 0<=idx<len(trend_pos) and trend_pos[idx] is not None:
            return int(trend_pos[idx])
        return None
    # Build the row-index map from the source dates without assuming calendar spacing.
    # The training rows are contiguous daily observations after invalid rows are removed.
    # Use date strings from raw examples to resolve indices exactly.
    # A date->index map is populated by the caller on each raw row.
    cal_states=[state_for(r,i) for i,r in enumerate(cal_raw)]
    usable=[i for i,s in enumerate(cal_states) if s is not None]
    if len(usable)<20:
        return {"available":False,"edge_validated":False,"signal":"HOLD / INSUFFICIENT TREND HISTORY",
                "reason":"The trend indicator does not have enough warm-up history."}
    cal_ps=[predict(eval_model,r["x"]) for r in cal_rows]
    cal_ys=[r["y"] for r in cal_rows]
    calibrator=_fit_platt(cal_ps,cal_ys)
    base_rate=(sum(cal_ys)+1.0)/(len(cal_ys)+2.0)
    # Independent directional expert: regularized logistic regression optimized
    # with reverse-mode AD gradients and an exact Hessian Newton system.
    logistic_cal=_q_fit_logistic_newton(cal_rows)
    trend_states=[cal_states[i] for i in usable]
    trend_labels=[cal_ys[i] for i in usable]
    trend_map=_trend_bucket_probabilities(usable,trend_labels,trend_states)
    model_score_ps=[_apply_platt(predict(eval_model,r["x"]),calibrator) for r in score_rows]
    logistic_score_ps=[_q_logistic_predict(logistic_cal,r["x"]) for r in score_rows]
    score_ys=[r["y"] for r in score_rows]
    score_states=[state_for(r,cut+i) for i,r in enumerate(score_raw)]
    score_trend_ps=[trend_map[str(int(st))] if st is not None else base_rate for st in score_states]
    score_base=[base_rate]*len(score_ys)
    base_loss=_logloss(score_base,score_ys) or 0.693
    model_loss=_logloss(model_score_ps,score_ys) or base_loss
    logistic_loss=_logloss(logistic_score_ps,score_ys) or base_loss
    trend_loss=_logloss(score_trend_ps,score_ys) or base_loss
    # Every expert earns weight only for validation log-loss skill over the
    # same base rate. Risk models are intentionally excluded from direction votes.
    skills={"neural":max(0.0,base_loss-model_loss-0.002),
            "logistic_newton":max(0.0,base_loss-logistic_loss-0.002),
            "trend":max(0.0,base_loss-trend_loss-0.002)}
    total=sum(skills.values())
    weights=({k:v/total for k,v in skills.items()} if total>0 else {"neural":0.0,"logistic_newton":0.0,"trend":0.0})
    test_model_ps=[_apply_platt(predict(eval_model,r["x"]),calibrator) for r in test_rows]
    test_logistic_ps=[_q_logistic_predict(logistic_cal,r["x"]) for r in test_rows]
    test_ys=[r["y"] for r in test_rows]
    test_states=[state_for(r,cut+len(score_raw)+i) for i,r in enumerate(raw_test)]
    test_trend_ps=[trend_map[str(int(st))] if st is not None else base_rate for st in test_states]
    fused_test=[]
    for pm,pl,pt in zip(test_model_ps,test_logistic_ps,test_trend_ps):
        expert_probs={"neural":pm,"logistic_newton":pl,"trend":pt}
        fused_test.append(sum(weights.get(k,0.0)*v for k,v in expert_probs.items()) if total>0 else base_rate)
    # Baseline is fixed from the earlier calibration slice; do not use holdout labels
    # to choose the baseline class.
    baseline_accuracy=max(base_rate,1.0-base_rate)
    preds=[int(p>=0.5) for p in fused_test]
    accuracy=sum(int(p==y) for p,y in zip(preds,test_ys))/len(test_ys)
    tp=sum(1 for p,y in zip(preds,test_ys) if p==1 and y==1)
    fp=sum(1 for p,y in zip(preds,test_ys) if p==1 and y==0)
    tn=sum(1 for p,y in zip(preds,test_ys) if p==0 and y==0)
    fn=sum(1 for p,y in zip(preds,test_ys) if p==0 and y==1)
    precision=tp/(tp+fp) if tp+fp else 0.0
    recall=tp/(tp+fn) if tp+fn else 0.0
    fpr=fp/(fp+tn) if fp+tn else 0.0
    balanced_accuracy=(recall+(1-fpr))/2
    edge=accuracy-baseline_accuracy
    ci=_block_edge_interval(preds,test_ys,baseline_accuracy)
    # Require positive lower 95% block-bootstrap bound, not merely a green point estimate.
    validated=bool(ci and ci[0]>0 and total>0)
    latest_raw=predict(full_model,latest_features)
    latest_model=_apply_platt(latest_raw,calibrator)
    # Refit the logistic expert on the full validation history for serving;
    # its evaluation above remains restricted to later validation + holdout.
    logistic_serving=_q_fit_logistic_newton(valid_rows)
    latest_logistic=_q_logistic_predict(logistic_serving,latest_features)
    latest_state=trend_pos[-1] if trend_pos and trend_pos[-1] is not None else None
    latest_trend=trend_map.get(str(int(latest_state)),base_rate) if latest_state is not None else base_rate
    latest_experts={"neural":latest_model,"logistic_newton":latest_logistic,"trend":latest_trend}
    latest_fused=sum(weights.get(k,0.0)*v for k,v in latest_experts.items()) if total>0 else base_rate
    if not validated:
        signal="HOLD / NO VALIDATED DIRECTIONAL EDGE"
        reason="The later chronological holdout does not show a statistically positive edge over the majority baseline. MIRA reports probabilities but withholds BUY/SELL."
    elif latest_fused>=0.55:
        signal="BUY"
        reason="The calibrated directional ensemble has a validated positive holdout edge and the fused next-session probability is above 55%."
    elif latest_fused<=0.45:
        signal="SELL"
        reason="The calibrated directional ensemble has a validated positive holdout edge and the fused next-session probability is below 45%."
    else:
        signal="HOLD / PROBABILITY TOO CLOSE"
        reason="The directional model has a validated holdout edge, but the current fused probability is too close to neutral."
    return {"available":True,"edge_validated":validated,"signal":signal,"reason":reason,
            "as_of":None,"next_session_up_probability":latest_fused,
            "direction_model_probability":latest_model,"logistic_newton_probability":latest_logistic,"trend_conditional_probability":latest_trend,
            "expert_probabilities":latest_experts,"expert_validation_logloss":{"neural":model_loss,"logistic_newton":logistic_loss,"trend":trend_loss},
            "logistic_optimizer":logistic_cal.get("optimizer"),"logistic_iterations":logistic_cal.get("iterations"),
            "logistic_model":logistic_serving,
            "trend_state":("BUY" if latest_state==1 else "SELL" if latest_state==0 else "UNAVAILABLE"),
            "weights":weights,"validation_logloss":{"baseline":base_loss,"neural":model_loss,"logistic_newton":logistic_loss,"trend":trend_loss},
            "holdout_accuracy":accuracy,"holdout_majority_baseline":baseline_accuracy,
            "balanced_accuracy":balanced_accuracy,"precision":precision,"recall":recall,
            "false_positive_rate":fpr,"false_negative_rate":(fn/(fn+tp) if fn+tp else 0.0),
            "confusion":{"tp":tp,"fp":fp,"tn":tn,"fn":fn},
            "holdout_edge_pp":edge*100,"holdout_edge_ci_pp":[x*100 for x in ci] if ci else None,
            "holdout_rows":len(test_rows),"calibration":calibrator,"trend_probability_map":trend_map,
            "calibration_base_rate":base_rate,"trend_lookback":trend_n,"method":"Platt-calibrated neural expert + AD/Hessian Newton logistic expert + beta-smoothed trend expert; validation log-loss skill weights; untouched chronological holdout and 5-session block-bootstrap edge interval."}

def forecast_nasdaq(payload):
    global NASDAQ_RESULT, MODEL
    raw=payload.get("csv","")
    if not raw.strip():
        with (DATA_DIR/"nasdaq100_daily.csv").open(encoding="utf-8-sig") as f: raw=f.read()
        payload={**payload,"instrument":payload.get("instrument","Nasdaq-100 (FRED/Nasdaq daily series)")}
    reader=csv.DictReader(io.StringIO(raw.lstrip("\ufeff")))
    if reader.fieldnames: reader.fieldnames=[x.strip() for x in reader.fieldnames]
    rows=list(reader); names=reader.fieldnames or []
    date_col=next((n for n in names if n.lower() in ("date","datetime","timestamp","time","observation_date")),None)
    close_col=payload.get("close_col") or next((n for n in names if n.lower() in ("close","close/last","last","adj close","adj_close","adjclose","ndx","qqq","nasdaq100")),None)
    volume_col=next((n for n in names if n.lower() in ("volume","vol")),None)
    if not date_col or not close_col or close_col not in names:
        raise ValueError("Historical CSV needs a date column and a close/NDX/NASDAQ100 price column.")
    prices=[]
    for row in rows:
        try:
            raw_price=row[close_col].replace(",","").replace("$","").strip()
            volume=float(row[volume_col].replace(",","").strip()) if volume_col and row.get(volume_col) else 0.0
            prices.append((parse_date(row[date_col]),float(raw_price),volume))
        except (ValueError,TypeError,KeyError,AttributeError): continue
    prices.sort(key=lambda r:r[0])
    if len(prices)<160: raise ValueError("Need at least 160 valid daily observations; longer histories improve regime coverage.")
    close=[p[1] for p in prices]
    if any(v<=0 or not math.isfinite(v) for v in close): raise ValueError("Close prices must be positive finite numbers.")
    daily=[0.0]+[close[i]/close[i-1]-1 for i in range(1,len(close))]
    def std(vals):
        avg=sum(vals)/len(vals); return math.sqrt(sum((v-avg)**2 for v in vals)/len(vals))
    # Causal EMA/MACD features. All derivatives are finite differences of
    # information available at or before session i (never future prices).
    ema12=[close[0]]; ema26=[close[0]]; macd=[]
    a12=2.0/13.0; a26=2.0/27.0
    for value in close[1:]:
        ema12.append(a12*value+(1-a12)*ema12[-1])
        ema26.append(a26*value+(1-a26)*ema26[-1])
    for i in range(len(close)):
        macd.append((ema12[i]-ema26[i])/close[i])
    macd_signal=[macd[0]]; a9=2.0/10.0
    for value in macd[1:]:
        macd_signal.append(a9*value+(1-a9)*macd_signal[-1])
    def features(i):
        volchange=0.0
        if volume_col and prices[i][2]>0 and prices[i-1][2]>0:
            volchange=max(-3,min(3,math.log(prices[i][2]/prices[i-1][2])))
        ema_slope=(ema12[i]/ema12[i-3]-1) if i>=3 else 0.0
        prior_slope=(ema12[i-3]/ema12[i-6]-1) if i>=6 else 0.0
        ema_acceleration=ema_slope-prior_slope
        macd_histogram=macd[i]-macd_signal[i]
        return [daily[i],close[i]/close[i-2]-1,close[i]/close[i-5]-1,
                close[i]/close[i-10]-1,close[i]/close[i-20]-1,
                std(daily[i-4:i+1]),std(daily[i-19:i+1]),volchange,
                ema_slope,ema_acceleration,macd_histogram]
    examples=[{"x":features(i),"up":int(close[i+1]>close[i]),"risk":int(min(close[i+1:i+21])<=close[i]*.90),"volatility":int(std(daily[i+1:i+21])>max(std(daily[i-19:i+1])*1.35,1e-6)),"id":prices[i][0].date().isoformat()} for i in range(20,len(close)-20)]
    cut=int(len(examples)*.8)
    # The stress label looks 20 sessions into the future. Leave a 20-row gap
    # at both chronological boundaries so those future labels cannot cross
    # from fit to validation or from validation to the untouched holdout.
    validation_start=int(cut*.8); validation_end=max(validation_start,cut-20)
    fit_end=max(1,validation_start-20)
    raw_fit=examples[:fit_end]
    raw_validation=examples[validation_start:validation_end]
    raw_test=examples[cut:]
    _price_index_by_date={p[0].date().isoformat():i for i,p in enumerate(prices)}
    for _row in raw_validation+raw_test:
        _row["_price_index"]=_price_index_by_date.get(_row["id"])
    if not raw_test: raise ValueError("Not enough dated examples to form a chronological holdout.")
    if len(raw_fit)<30 or len(raw_validation)<30:
        # Small uploaded histories cannot support a separate threshold set.
        raw_fit=examples[:max(1,cut-20)]; raw_validation=[]
    mean=[sum(o["x"][j] for o in raw_fit)/len(raw_fit) for j in range(len(raw_fit[0]["x"]))]
    scale=[math.sqrt(sum((o["x"][j]-mean[j])**2 for o in raw_fit)/len(raw_fit)) or 1 for j in range(len(raw_fit[0]["x"]))]
    def scaled(items,label,center=None,spread=None):
        center=mean if center is None else center; spread=scale if spread is None else spread
        return [{"x":[(v-center[j])/spread[j] for j,v in enumerate(o["x"])],"y":o[label],"id":o["id"]} for o in items]
    train_up,test_up=scaled(raw_fit,"up"),scaled(raw_test,"up")
    train_risk,test_risk=scaled(raw_fit,"risk"),scaled(raw_test,"risk")
    train_vol,test_vol=scaled(raw_fit,"volatility"),scaled(raw_test,"volatility")
    valid_up=scaled(raw_validation,"up")
    valid_risk=scaled(raw_validation,"risk")
    valid_vol=scaled(raw_validation,"volatility")
    def fit(data,epochs=55,label="direction",base=0.2,span=0.18,validation=None,patience=8):
        m={"w1":[[random.uniform(-.15,.15) for _ in range(len(data[0]["x"])+1)] for _ in range(8)],"w2":[random.uniform(-.15,.15) for _ in range(9)]}
        logs=[];step=max(1,epochs//10);best_loss=float("inf");best_epoch=0;stale=0;best_model=None
        for ep in range(epochs):
            random.shuffle(data);train_pass(m,data,.008/(1+ep*.01))
            train_loss=cross_entropy(m,data)
            validation_loss=cross_entropy(m,validation) if validation else None
            monitor_loss=validation_loss if validation else train_loss
            if validation:
                if monitor_loss<best_loss-1e-5:
                    best_loss=monitor_loss;best_epoch=ep+1;stale=0
                    best_model={"w1":[row.copy() for row in m["w1"]],"w2":m["w2"].copy()}
                else: stale+=1
            else: best_epoch=ep+1
            if ep%step==0 or ep==epochs-1 or (validation and stale>=patience):
                logs.append({"epoch":ep+1,"loss":train_loss,"validation_loss":validation_loss})
                visible_status("training","%s neural network · epoch %d/%d"%(label,ep+1,epochs),base+span*(ep+1)/epochs,{"label":label,"epoch":ep+1,"epochs":epochs,"model":m,"sample":data[-1]["x"]})
            if validation and ep+1>=10 and stale>=patience: break
        return (best_model if validation else m),logs,best_epoch or epochs
    def fit_ensemble(data,epochs=55,label="direction",base=0.2,span=0.18,validation=None):
        seeds=(17,41,73); members=[]; best_epochs=[]; first_logs=[]
        for index,seed in enumerate(seeds):
            random.seed(seed)
            member_data=[{**row,"x":row["x"].copy()} for row in data]
            member_validation=[{**row,"x":row["x"].copy()} for row in validation] if validation else None
            member_epochs=epochs[index] if isinstance(epochs,(list,tuple)) else epochs
            model,logs,best_epoch=fit(member_data,epochs=member_epochs,label=label+" ensemble %d/3"%(index+1),base=base+span*index/3,span=span/3,validation=member_validation)
            members.append(model); best_epochs.append(best_epoch)
            if index==0: first_logs=logs
        # Keep one representative topology for the weight visualizer; predictions average all three.
        return {"members":members,"w1":members[0]["w1"],"w2":members[0]["w2"]},first_logs,best_epochs
    visible_status("preparing","Building chronological examples and normalizing 11 price/derivative features.",0.08)
    eval_up,logs,direction_best_epochs=fit_ensemble(train_up,label="direction validation fit",base=0.12,span=0.16,validation=valid_up or None)
    eval_risk,_,stress_best_epochs=fit_ensemble(train_risk,label="drawdown-risk validation fit",base=0.36,span=0.16,validation=valid_risk or None)
    eval_vol,_,volatility_best_epochs=fit_ensemble(train_vol,label="volatility-regime validation fit",base=0.42,span=0.10,validation=valid_vol or None)
    stress_threshold,threshold_selection=choose_threshold_f2(eval_risk,valid_risk) if valid_risk else (0.5,{"f2":None,"validation_positive_rows":0})
    up=evaluate(eval_up,test_up);risk=evaluate(eval_risk,test_risk,stress_threshold);volatility=evaluate(eval_vol,test_vol)
    up_base=max(sum(o["y"] for o in test_up),len(test_up)-sum(o["y"] for o in test_up))/len(test_up)
    risk_base=max(sum(o["y"] for o in test_risk),len(test_risk)-sum(o["y"] for o in test_risk))/len(test_risk)
    direction_balanced=(up["recall"]+1-up["fpr"])/2
    stress_balanced=(risk["recall"]+1-risk["fpr"])/2
    # Refit serving models with normalization from their full training history.
    full_mean=[sum(o["x"][j] for o in examples)/len(examples) for j in range(len(examples[0]["x"]))]
    full_scale=[math.sqrt(sum((o["x"][j]-full_mean[j])**2 for o in examples)/len(examples)) or 1 for j in range(len(examples[0]["x"]))]
    full_up,_,_=fit_ensemble(scaled(examples,"up",full_mean,full_scale),epochs=direction_best_epochs,label="direction final fit",base=0.52,span=0.12)
    full_risk,_,_=fit_ensemble(scaled(examples,"risk",full_mean,full_scale),epochs=stress_best_epochs,label="drawdown-risk final fit",base=0.64,span=0.12)
    full_vol,_,_=fit_ensemble(scaled(examples,"volatility",full_mean,full_scale),epochs=volatility_best_epochs,label="volatility-regime final fit",base=0.76,span=0.12)
    latest=[(v-full_mean[j])/full_scale[j] for j,v in enumerate(features(len(close)-1))]
    p_up_raw=predict(full_up,latest);p_risk=predict(full_risk,latest);p_volatility=predict(full_vol,latest)
    fusion=build_directional_fusion(eval_up,valid_up,test_up,raw_validation,raw_test,
                                    close,full_up,latest)
    fusion["as_of"]=prices[-1][0].date().isoformat()
    signal=fusion["signal"]
    p_up=fusion.get("next_session_up_probability",p_up_raw)
    try:
        event_rows=list(csv.DictReader((DATA_DIR/"historical_stress_events.csv").open(encoding="utf-8")))
        episode_count=sum(1 for e in event_rows if e["asset"]=="Nasdaq-100" and "drawdown" in e["event_type"])
    except OSError: episode_count=0
    model_dir=DATA_DIR/"models";model_dir.mkdir(exist_ok=True)
    saved={"model_name":"MIRA — Market Insight & Risk Assistant","instrument":payload.get("instrument","Nasdaq-100"),"as_of":prices[-1][0].date().isoformat(),"features":["return_1d","return_2d","return_5d","return_10d","return_20d","volatility_5d","volatility_20d","volume_log_change","ema12_slope_3d","ema12_acceleration_3d","normalized_macd_histogram"],"mean":full_mean,"scale":full_scale,"direction_model":full_up,"drawdown_risk_model":full_risk,"volatility_regime_model":full_vol,"stress_threshold":stress_threshold,"train_rows":len(examples),"architecture":"9-member neural direction/risk/volatility MLP ensembles + reverse-mode-AD / exact-Hessian logistic directional expert + validation-weighted trend expert + Gaussian HMM / Kalman / GARCH regime-volatility layer","ensemble_members":3,"validation_best_epochs":{"direction":direction_best_epochs,"stress":stress_best_epochs,"volatility_regime":volatility_best_epochs},"fusion":fusion,"quantitative_intelligence":quantitative_intelligence_snapshot(close)}
    (model_dir/"nasdaq100_model.json").write_text(json.dumps(saved,indent=2),encoding="utf-8")
    MODEL=full_up
    NASDAQ_RESULT={"instrument":saved["instrument"],"as_of":saved["as_of"],"signal":signal,"next_session_up_probability":round(p_up,5),"raw_direction_model_probability":round(p_up_raw,5),"direction_model_probability":fusion.get("direction_model_probability"),"fusion":fusion,"quantitative_intelligence":saved.get("quantitative_intelligence"),"next20_session_10pct_drawdown_risk":round(p_risk,5),"next20_session_high_volatility_probability":round(p_volatility,5),"holdout_direction_accuracy":fusion.get("holdout_accuracy",up["accuracy"]),"raw_model_holdout_direction_accuracy":up["accuracy"],"raw_model_holdout_direction_balanced_accuracy":direction_balanced,"holdout_direction_balanced_accuracy":fusion.get("balanced_accuracy",direction_balanced),"direction_majority_baseline":fusion.get("holdout_majority_baseline",up_base),"direction_edge_vs_baseline_pp":fusion.get("holdout_edge_pp"),"holdout_direction_precision":fusion.get("precision",up["precision"]),"holdout_direction_recall":fusion.get("recall",up["recall"]),"holdout_direction_fpr":fusion.get("false_positive_rate",up["fpr"]),"holdout_direction_fnr":fusion.get("false_negative_rate",up["fnr"]),"direction_confusion":fusion.get("confusion",up["counts"]),"holdout_stress_accuracy":risk["accuracy"],"holdout_stress_balanced_accuracy":stress_balanced,"stress_majority_baseline":risk_base,"stress_precision":risk["precision"],"stress_recall":risk["recall"],"stress_fpr":risk["fpr"],"stress_fnr":risk["fnr"],"stress_confusion":risk["counts"],"volatility_regime_holdout_accuracy":volatility["accuracy"],"volatility_regime_balanced_accuracy":(volatility["recall"]+1-volatility["fpr"])/2,"volatility_regime_baseline_accuracy":max(sum(o["y"] for o in test_vol),len(test_vol)-sum(o["y"] for o in test_vol))/len(test_vol),"stress_threshold":stress_threshold,"stress_threshold_validation_f2":threshold_selection["f2"],"stress_validation_precision":threshold_selection.get("validation_precision"),"stress_validation_recall":threshold_selection.get("validation_recall"),"validation_rows":len(raw_validation),"holdout_rows":len(raw_test),"training_rows":len(raw_fit),"purged_boundary_rows":20,"validation_best_epochs":{"direction":direction_best_epochs,"stress":stress_best_epochs,"volatility_regime":volatility_best_epochs},"historical_nasdaq100_20pct_drawdowns":episode_count,"target":"up/down: next close; 20-session 10% drawdown; elevated forward volatility relative to trailing volatility","learning_trace":logs,"holdout_trace":up["trace"][-30:],"saved_model":"data/models/nasdaq100_model.json","probability_warning":"Directional probability is calibrated on a separate validation slice; fusion weights are validation-selected and assessed on an untouched chronological holdout. This does not establish a guaranteed or permanent edge. Research output only."}
    confusion=lambda counts:{"true_positive":counts["tp"],"false_positive":counts["fp"],"true_negative":counts["tn"],"false_negative":counts["fn"]}
    vol_base=max(sum(o["y"] for o in test_vol),len(test_vol)-sum(o["y"] for o in test_vol))/len(test_vol)
    report={"model_name":saved["model_name"],"instrument":saved["instrument"],"data_as_of":saved["as_of"],"trained_on":datetime.now().date().isoformat(),"signal":signal,"next_session_up_probability":round(p_up,5),"fusion":fusion,"next20_session_10pct_drawdown_risk":round(p_risk,5),"next20_session_high_volatility_probability":round(p_volatility,5),"direction":{"holdout_accuracy":fusion.get("holdout_accuracy",up["accuracy"]),"majority_baseline_accuracy":fusion.get("holdout_majority_baseline",up_base),"edge_vs_baseline_pp":fusion.get("holdout_edge_pp"),"edge_ci_95_pp":fusion.get("holdout_edge_ci_pp"),"balanced_accuracy":fusion.get("balanced_accuracy",direction_balanced),"precision":fusion.get("precision",up["precision"]),"recall":fusion.get("recall",up["recall"]),"false_positive_rate":fusion.get("false_positive_rate",up["fpr"]),"false_negative_rate":fusion.get("false_negative_rate",up["fnr"]),"confusion":fusion.get("confusion",confusion(up["counts"])),"balanced_accuracy_raw_model":direction_balanced,"precision_raw_model":up["precision"],"recall_raw_model":up["recall"],"false_positive_rate_raw_model":up["fpr"],"false_negative_rate_raw_model":up["fnr"],"confusion_raw_model":confusion(up["counts"]),"raw_model":{"holdout_accuracy":up["accuracy"],"majority_baseline_accuracy":up_base,"balanced_accuracy":direction_balanced,"precision":up["precision"],"recall":up["recall"],"false_positive_rate":up["fpr"],"false_negative_rate":up["fnr"],"confusion":confusion(up["counts"])},"fusion":{"holdout_accuracy":fusion.get("holdout_accuracy"),"majority_baseline_accuracy":fusion.get("holdout_majority_baseline"),"edge_vs_baseline_pp":fusion.get("holdout_edge_pp"),"edge_ci_95_pp":fusion.get("holdout_edge_ci_pp"),"weights":fusion.get("weights"),"edge_validated":fusion.get("edge_validated")}},"stress":{"holdout_accuracy":risk["accuracy"],"majority_baseline_accuracy":risk_base,"balanced_accuracy":stress_balanced,"precision":risk["precision"],"recall":risk["recall"],"false_positive_rate":risk["fpr"],"false_negative_rate":risk["fnr"],"confusion":confusion(risk["counts"]),"decision_threshold":stress_threshold,"threshold_selection":"F2 on separate chronological validation block","validation_precision":threshold_selection.get("validation_precision"),"validation_recall":threshold_selection.get("validation_recall")},"volatility_regime":{"holdout_accuracy":volatility["accuracy"],"majority_baseline_accuracy":vol_base,"balanced_accuracy":(volatility["recall"]+1-volatility["fpr"])/2,"precision":volatility["precision"],"recall":volatility["recall"],"false_positive_rate":volatility["fpr"],"false_negative_rate":volatility["fnr"],"confusion":confusion(volatility["counts"]),"target":"next 20-session realized volatility exceeds 1.35 times the trailing 20-session realized volatility"},"train_rows":len(raw_fit),"validation_rows":len(raw_validation),"holdout_rows":len(raw_test),"purged_boundary_rows":20,"validation_best_epochs":{"direction":direction_best_epochs,"stress":stress_best_epochs,"volatility_regime":volatility_best_epochs},"ensemble_members_per_task":3,"quantitative_intelligence":saved.get("quantitative_intelligence"),"features":["1/2/5/10/20-session returns","5/20-session volatility","volume change","EMA12 slope","EMA12 acceleration","normalized MACD histogram"],"target_definitions":{"direction":"next recorded close higher than current close","stress":"any close in next 20 sessions at least 10% below current close"},"crash_event_counts":{"nasdaq100_daily_20pct_drawdowns":episode_count},"training_method":"Three independently seeded neural networks per task plus an independently fitted ridge-logistic directional expert optimized by reverse-mode automatic differentiation and exact-Hessian damped Newton; validation-skill-weighted neural/logistic/trend directional fusion; two-state Gaussian HMM estimated by Baum-Welch EM; Kalman and GARCH volatility diagnostics.","limitation":"One chronological holdout after a 20-session purge. Direction probabilities are Platt-calibrated on a separate validation slice; fusion weights are selected by validation log-loss skill and judged on the untouched holdout. Holdout results do not guarantee future edge. Research output only."}
    (model_dir/"nasdaq100_training_report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    visible_status("complete","Forecast complete · fused signal %s · P(up) %.1f%% · 20-session stress risk %.1f%%"%(signal,p_up*100,p_risk*100),1,{"label":"direction forecast","epoch":55,"epochs":55,"model":full_up,"sample":latest})
    return NASDAQ_RESULT


NASDAQ_JOB={"running":False,"message":"Not started","error":None,"trained_through":None}
NASDAQ_LOCK=threading.Lock()

def _nasdaq_training_worker():
    try:
        NASDAQ_JOB.update({"message":"Training directional/risk/volatility ensembles on the latest completed daily dataset.","error":None})
        result=forecast_nasdaq({})
        NASDAQ_JOB.update({"message":"Directional fusion refreshed through %s."%result.get("as_of"),"trained_through":result.get("as_of")})
    except Exception as exc:
        NASDAQ_JOB.update({"message":"Directional ensemble refresh failed: "+str(exc)[:160],"error":str(exc)[:300]})
    finally:
        NASDAQ_JOB["running"]=False

def start_nasdaq_training(force=False):
    """Train asynchronously; never retrain on every quote or every second."""
    with NASDAQ_LOCK:
        if NASDAQ_JOB["running"]:
            return {"started":False,"message":"Directional ensemble training is already running."}
        if not force:
            try:
                saved=json.loads((DATA_DIR/"models"/"nasdaq100_model.json").read_text(encoding="utf-8"))
                dates,_=_load_csv_series()
                idx={d:i for i,d in enumerate(dates)}
                old=idx.get(saved.get("as_of"),-10**9)
                saved_fusion=saved.get("fusion") or {}
                # Older model snapshots may contain the risk MLPs but no serving-ready
                # directional calibration/fusion. Treat that as a rebuild requirement,
                # even when the underlying daily history has not advanced 21 sessions.
                fusion_missing=(not saved_fusion.get("available") or
                                not saved_fusion.get("calibration") or
                                not saved_fusion.get("weights") or
                                not (saved_fusion.get("logistic_model") or {}).get("available"))
                if len(dates)-1-old<21 and not fusion_missing:
                    return {"started":False,"message":"Directional ensemble is current; fewer than 21 new completed sessions."}
            except (OSError,json.JSONDecodeError,ValueError):
                pass
        NASDAQ_JOB["running"]=True
        NASDAQ_JOB["message"]="Starting directional ensemble refresh."
    threading.Thread(target=_nasdaq_training_worker,daemon=True).start()
    return {"started":True,"message":NASDAQ_JOB["message"]}


def inspect_dataset():
    if not ACTIVE_CSV.strip(): return {"source":"generated sandbox","note":"No CSV supplied to this agent turn."}
    reader=csv.DictReader(io.StringIO(ACTIVE_CSV.lstrip("\ufeff")))
    if reader.fieldnames: reader.fieldnames=[x.strip() for x in reader.fieldnames]
    rows=list(reader); target=ACTIVE_TARGET or (reader.fieldnames[-1] if reader.fieldnames else "")
    numeric=[]
    for name in (reader.fieldnames or []):
        if name==target: continue
        try: float(next(row[name] for row in rows if row.get(name,"")))
        except (ValueError,StopIteration,TypeError,KeyError): continue
        numeric.append(name)
    labels={"0":0,"1":0}; invalid=0
    for row in rows:
        try:
            y=float(row[target]); [float(row[k]) for k in numeric]
            if y in (0,1): labels[str(int(y))]+=1
            else: invalid+=1
        except (ValueError,TypeError,KeyError): invalid+=1
    return {"rows":len(rows),"target":target,"numeric_features":numeric,"binary_target_counts":labels,"invalid_or_missing_rows":invalid}

# ---- MIRA chat decision layer v2: Final BUY / SELL from a self-checking trend engine ----
# The small language model never decides. Python computes the call from price history, grades its own past calls,
# re-tests its settings on all history, and only switches settings when a challenger clearly beats the champion.
# Customer questions are logged for improving the chat wording only; they are NEVER used to train the market forecast.
import re
LEARN_DIR = DATA_DIR / "learning"
TREND_GRID = [(n, b) for n in (100, 150, 200, 250) for b in (0.0, 0.01, 0.02)]
DEFAULT_RULE = (200, 0.01)
HOLDOUT_DAYS = 504

def is_decision_question(prompt):
    p=prompt.lower()
    wants=re.search(r"\b(buy|sell|hold|invest|trade|outlook|predict|bullish|bearish|verdict|final)\b|should i|what should|good time",p)
    metric=re.search(r"false positive|false negative|accuracy|error rate|holdout|train|simulat|sandbox|csv|dataset|explore",p)
    return bool(wants) and not metric

def load_market_report():
    try: return json.loads((DATA_DIR/"models"/"nasdaq100_training_report.json").read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return None

# ---- Live price updates: keep the history current without anyone editing the CSV ----
# 1) Official Nasdaq-100 closes come from FRED (same series as data/nasdaq100_daily.csv, no key needed) and are appended to the CSV.
# 2) If free Alpaca keys are connected, days newer than the CSV are estimated from live QQQ prices, chained onto the last official close.
import threading
PRICE_STATE={"checked_at":0.0,"fred":"not checked yet","alpaca":"not checked yet","extra":[],"appended":0,"last_csv_date":None,"updated":None}
PRICE_LOCK=threading.Lock()

def _csv_path(): return DATA_DIR/"nasdaq100_daily.csv"

def _load_csv_series():
    path=_csv_path()
    with path.open(encoding="utf-8-sig") as f:
        reader=csv.DictReader(f)
        names=[n.strip() for n in (reader.fieldnames or [])]; reader.fieldnames=names
        date_col=next((n for n in names if n.lower() in ("date","observation_date","datetime","timestamp")),None)
        close_col=next((n for n in names if n.lower() in ("nasdaq100","close","close/last","adj close","adj_close","ndx","qqq","last")),None)
        if not date_col or not close_col: raise ValueError("data/nasdaq100_daily.csv needs a date column and a close column.")
        rows={}
        for row in reader:
            try:
                value=float(str(row[close_col]).replace(",","").replace("$","").strip())
                if value>0 and math.isfinite(value): rows[parse_date(row[date_col]).date().isoformat()]=value
            except (ValueError,TypeError,KeyError,AttributeError): continue
    dates=sorted(rows); closes=[rows[d] for d in dates]
    if len(closes)<400: raise ValueError("Need at least 400 daily closes in data/nasdaq100_daily.csv.")
    return dates,closes

def load_price_series():
    dates,closes=_load_csv_series()
    for d,v,_src in (PRICE_STATE.get("extra") or []):
        if d>dates[-1]: dates.append(d); closes.append(v)
    return dates,closes

FRED_API_KEY=os.environ.get("FRED_API_KEY","").strip()
def _fred_api(series_id,start):
    url="https://api.stlouisfed.org/fred/series/observations?series_id=%s&file_type=json&observation_start=%s&api_key=%s"%(series_id,start,FRED_API_KEY)
    last_exc=None
    for attempt in range(3):
        try:
            with urlopen(Request(url,headers={"User-Agent":"MIRA-research/1.0"}),timeout=30) as response: raw=json.loads(response.read())
            break
        except Exception as exc: last_exc=exc; time.sleep(2*(attempt+1))
    else: raise last_exc
    out=[]
    for o in raw.get("observations",[]):
        try: v=float(o["value"])
        except (ValueError,TypeError,KeyError): continue
        if math.isfinite(v): out.append((o["date"],v))
    out.sort()
    return out

def _fred_rows(last_date):
    if not FRED_API_KEY: return _fred_rows_old(last_date)
    start=(date.fromisoformat(last_date)-timedelta(days=10)).isoformat()
    return [(d,v) for d,v in _fred_api("NASDAQ100",start) if v>0]

def _fred_rows_old(last_date):
    start=(date.fromisoformat(last_date)-timedelta(days=10)).isoformat()
    req=Request("https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQ100&cosd="+start,headers={"User-Agent":"MIRA-research/1.0"})
    last_exc=None
    for attempt in range(3):                      # free hosts can be slow right after waking up; retry before giving up
        try:
            with urlopen(req,timeout=25) as response: text=response.read().decode("utf-8-sig")
            break
        except Exception as exc:
            last_exc=exc; time.sleep(2*(attempt+1))
    else: raise last_exc
    out=[]; reader=csv.reader(io.StringIO(text)); next(reader,None)
    for rec in reader:
        if len(rec)<2: continue
        try: d=parse_date(rec[0]).date().isoformat(); v=float(rec[1])      # FRED marks holidays with "." or blank
        except (ValueError,TypeError): continue
        if v>0 and math.isfinite(v): out.append((d,v))
    return out

def _append_to_csv(new_rows):
    path=_csv_path()
    with path.open(encoding="utf-8-sig") as f: header=next(csv.reader(f))
    names=[n.strip() for n in header]
    date_col=next(i for i,n in enumerate(names) if n.lower() in ("date","observation_date","datetime","timestamp"))
    close_col=next(i for i,n in enumerate(names) if n.lower() in ("nasdaq100","close","close/last","adj close","adj_close","ndx","qqq","last"))
    with path.open(encoding="utf-8-sig") as f: body=f.read().rstrip("\r\n")
    buf=io.StringIO(); writer=csv.writer(buf,lineterminator="\n")
    for d,v in new_rows:
        rec=[""]*len(names); rec[date_col]=d; rec[close_col]="%.3f"%v; writer.writerow(rec)
    tmp=path.with_suffix(".csv.tmp"); tmp.write_text(body+"\n"+buf.getvalue(),encoding="utf-8"); os.replace(tmp,path)

def _alpaca_chain(last_date,last_close):
    start=(date.fromisoformat(last_date)-timedelta(days=6)).isoformat()
    req=Request("https://data.alpaca.markets/v2/stocks/QQQ/bars?timeframe=1Day&adjustment=raw&feed=iex&limit=30&start="+start,headers={"APCA-API-KEY-ID":MARKET_API_KEY,"APCA-API-SECRET-KEY":MARKET_API_SECRET,"Accept":"application/json"})
    with urlopen(req,timeout=10) as response: raw=json.loads(response.read())
    bars={b["t"][:10]:float(b["c"]) for b in (raw.get("bars") or []) if b.get("c")}
    if last_date not in bars: raise ValueError("QQQ has no bar for %s to anchor on"%last_date)
    return [(d,last_close*bars[d]/bars[last_date],"alpaca") for d in sorted(bars) if d>last_date]

def refresh_prices(force=False):
    with PRICE_LOCK:
        stale_cooldown = 90 if PRICE_STATE.get("fred","").startswith("unavailable") else 600   # retry soon after a failure instead of waiting the full interval
        if not force and time.time()-PRICE_STATE["checked_at"]<stale_cooldown: return PRICE_STATE
        PRICE_STATE["checked_at"]=time.time()
        try: dates,closes=_load_csv_series()
        except Exception as exc: PRICE_STATE["fred"]="CSV unreadable: "+str(exc)[:80]; return PRICE_STATE
        last=dates[-1]
        try:
            fresh=sorted((d,v) for d,v in _fred_rows(last) if d>last); keep=[]; prev=closes[-1]
            for d,v in fresh:
                if abs(v/prev-1)<0.25: keep.append((d,v)); prev=v        # ignore implausible one-day jumps
            if keep:
                _append_to_csv(keep); PRICE_STATE["appended"]+=len(keep); dates,closes=_load_csv_series(); last=dates[-1]
                visible_status("preparing","Price update: added %d new official close%s through %s."%(len(keep),"" if len(keep)==1 else "s",last))
            PRICE_STATE["fred"]="ok, %d new close%s"%(len(keep),"" if len(keep)==1 else "s")
        except Exception as exc: PRICE_STATE["fred"]="unavailable (%s)"%(str(exc)[:60] or exc.__class__.__name__)
        extra=[]
        if MARKET_API_KEY and MARKET_API_SECRET:
            try: extra=_alpaca_chain(last,closes[-1]); PRICE_STATE["alpaca"]="ok, %d live day%s"%(len(extra),"" if len(extra)==1 else "s")
            except Exception as exc: PRICE_STATE["alpaca"]="unavailable (%s)"%(str(exc)[:60] or exc.__class__.__name__)
        else: PRICE_STATE["alpaca"]="not connected"
        PRICE_STATE["extra"]=extra; PRICE_STATE["last_csv_date"]=last; PRICE_STATE["updated"]=datetime.now().isoformat(timespec="seconds")
        return PRICE_STATE

def official_data_age_days(dates=None):
    """Calendar age of the last official daily close used by the predictive models."""
    if dates is None:
        dates, _ = _load_csv_series()
    latest = date.fromisoformat(dates[-1])
    return max(0, (eastern_now().date() - latest).days)

def price_data_note():
    official_dates,_=_load_csv_series(); csv_last=PRICE_STATE.get("last_csv_date") or official_dates[-1]; et=eastern_now()
    ex=PRICE_STATE.get("extra") or []; note="Data: official daily closes through %s"%csv_last
    if ex:
        live=ex[-1][0]; is_today=(live==et.date().isoformat() and market_session(et)!="closed" and et.time().replace(tzinfo=None)<dt_time(16))
        note+=", plus %d day%s estimated from live QQQ prices through %s%s"%(len(ex),"" if len(ex)==1 else "s",live," (that trading day is not finished, so this estimate can still change)" if is_today else "")
    elif PRICE_STATE["alpaca"]=="not connected": note+=" (connect free Alpaca keys on the Live market data panel to include today's live move)"
    if PRICE_STATE.get("updated"): note+="; auto-update last ran %s (official closes: %s)"%(PRICE_STATE["updated"].replace("T"," "),PRICE_STATE["fred"])
    lag=(et.date()-date.fromisoformat(official_dates[-1])).days
    if lag>4: note+=". WARNING: the newest price is %d days old, so check your internet connection; this answer may be out of date"%lag
    return note+"."

# ---- Hourly learning: save QQQ hourly bars from Alpaca, learn each hour's typical move, and test whether hourly data improves the networks ----
INTRADAY_STATE={"status":"not started","bars":0,"first":None,"last":None,"days":0,"ab":None,"collected_at":None}
INTRADAY_LOCK=threading.Lock()

def intraday_path(): return DATA_DIR/"intraday_qqq_1h.csv"

def intraday_load():
    path=intraday_path(); out=[]
    if not path.exists(): return out
    with path.open(encoding="utf-8") as f:
        reader=csv.reader(f); next(reader,None)
        for r in reader:
            try: out.append((r[0],float(r[1]),float(r[2]),float(r[3]),float(r[4])))
            except (ValueError,IndexError): continue
    return out


OVERNIGHT_RESULT = None

def forecast_overnight_intraday():
    """
    Tests a real, well-documented structural pattern on YOUR OWN intraday
    data: most of an index ETF's return tends to happen overnight
    (yesterday's close -> today's open), not during the trading day
    (today's open -> today's close). This is not a price-direction model -
    it does not predict up/down. It measures, honestly and out-of-sample,
    whether staying positioned overnight has been a structurally stronger
    source of return than actively trading during the day - i.e. it is a
    test of WHETHER intraday trading activity has had anything to add, not
    a signal to trade on.
    """
    global OVERNIGHT_RESULT
    bars = intraday_load()
    if len(bars) < 300:
        raise ValueError("Not enough hourly bars yet (need ~300+); let intraday_collect() run longer, or widen the Alpaca export's date range.")

    def is_regular(ts):
        hh = int(ts[11:13])
        return 13 <= hh <= 20

    by_day = {}
    for ts, o, h, l, c in bars:
        if not is_regular(ts):
            continue
        day = ts[:10]
        by_day.setdefault(day, []).append((ts, o, c))

    days = sorted(by_day)
    daily = []
    for d in days:
        session = sorted(by_day[d])
        daily.append({"date": d, "open": session[0][1], "close": session[-1][2]})

    n = len(daily)
    if n < 300:
        raise ValueError("Only %d regular-session trading days found; need at least 300." % n)

    def split_cagr(lo, hi):
        overnight_cum, intraday_cum = 1.0, 1.0
        for i in range(max(lo, 1), hi):
            overnight_cum *= (1 + (daily[i]["open"]/daily[i-1]["close"] - 1))
            intraday_cum *= (1 + (daily[i]["close"]/daily[i]["open"] - 1))
        years = (hi - lo) / 252
        return {
            "overnight_cagr": overnight_cum ** (1/years) - 1,
            "intraday_cagr": intraday_cum ** (1/years) - 1,
            "overnight_multiple": overnight_cum,
            "intraday_multiple": intraday_cum,
            "years": years,
            "start": daily[lo]["date"], "end": daily[hi-1]["date"],
        }

    full = split_cagr(0, n)
    cut = int(n * 0.8)
    fit = split_cagr(0, cut)
    holdout = split_cagr(cut, n)

    overnight_wins_in_fit = fit["overnight_cagr"] > fit["intraday_cagr"]
    overnight_wins_in_holdout = holdout["overnight_cagr"] > holdout["intraday_cagr"]
    confirmed = overnight_wins_in_fit and overnight_wins_in_holdout

    if confirmed:
        insight = ("Across both the training period and a genuinely unseen holdout period, staying positioned "
                    "overnight has earned more than the intraday session did, and in the holdout period the gap "
                    "was large. This does not mean day-trading loses money, and it is not a buy/sell signal for "
                    "any single day - it means the data gives no evidence that actively trading in and out during "
                    "the day has added return beyond just holding the position.")
    else:
        insight = ("The overnight-vs-intraday split did not hold up consistently between the training period and "
                    "the holdout period, so this is not a reliable pattern in this data right now.")

    OVERNIGHT_RESULT = {
        "instrument": "QQQ (hourly, regular session only)",
        "trading_days": n,
        "full_history": full,
        "fit_period": fit,
        "holdout_period": holdout,
        "overnight_wins_in_fit": overnight_wins_in_fit,
        "overnight_wins_in_holdout": overnight_wins_in_holdout,
        "confirmed_out_of_sample": confirmed,
        "insight": insight,
        "not_a_trade_signal": True,
        "method": "Chronological 80/20 split, no lookahead. Compares close[i-1]->open[i] (overnight) against open[i]->close[i] (intraday) compounded returns, annualized.",
    }
    return OVERNIGHT_RESULT


def intraday_collect(max_pages=8):
    """Backfill hourly QQQ bars (Alpaca free IEX feed, roughly 2016 onward) on the first run, then append only new ones."""
    if not (MARKET_API_KEY and MARKET_API_SECRET): INTRADAY_STATE["status"]="needs Alpaca keys to collect hourly data"; return 0
    bars=intraday_load(); last=bars[-1][0] if bars else None
    start=(datetime.fromisoformat(last.replace("Z","+00:00"))+timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ") if last else "2016-01-04T00:00:00Z"
    token=None; added=[]; pages=0
    while pages<max_pages:
        url="https://data.alpaca.markets/v2/stocks/QQQ/bars?timeframe=1Hour&adjustment=raw&feed=iex&sort=asc&limit=10000&start="+start+(("&page_token="+token) if token else "")
        req=Request(url,headers={"APCA-API-KEY-ID":MARKET_API_KEY,"APCA-API-SECRET-KEY":MARKET_API_SECRET,"Accept":"application/json"})
        with urlopen(req,timeout=20) as response: raw=json.loads(response.read())
        for b in (raw.get("bars") or []):
            try: added.append((b["t"],float(b["o"]),float(b["h"]),float(b["l"]),float(b["c"]),float(b.get("v",0))))
            except (KeyError,ValueError,TypeError): continue
        token=raw.get("next_page_token"); pages+=1
        if not token: break
    if added:
        path=intraday_path(); new=not path.exists()
        with path.open("a",encoding="utf-8",newline="") as f:
            w=csv.writer(f,lineterminator="\n")
            if new: w.writerow(["t","o","h","l","c","v"])
            w.writerows(added)
    allb=intraday_load(); INTRADAY_STATE.update({"days":len(intraday_features(allb)),"bars":len(allb),"first":allb[0][0] if allb else None,"last":allb[-1][0] if allb else None,"collected_at":datetime.now().isoformat(timespec="seconds"),"status":("collected %d new hourly bars"%len(added)) if added else "up to date"})
    if token: INTRADAY_STATE["status"]+=" (history download continues next cycle)"
    return len(added)

def _et_of(t_iso): return eastern_now(datetime.fromisoformat(t_iso.replace("Z","+00:00")))

def intraday_groups(bars):
    groups={}
    for t,o,h,l,c in bars:
        if o<=0: continue
        et=_et_of(t)
        if 9<=et.hour<=15: groups.setdefault(et.date().isoformat(),[]).append((et.hour,o,h,l,c))
    return groups

def intraday_features(bars=None):
    """Six numbers per trading day from that day's hourly bars: move, hourly volatility, range, last-3-hours move, share of up hours, overnight gap."""
    groups=intraday_groups(bars if bars is not None else intraday_load()); out={}; prev=None
    for d in sorted(groups):
        g=sorted(groups[d])
        if len(g)<5: continue
        o0=g[0][1]; cl=[x[4] for x in g]; rets=[cl[0]/o0-1]+[cl[i]/cl[i-1]-1 for i in range(1,len(g))]
        mean=sum(rets)/len(rets); vol=math.sqrt(sum((x-mean)**2 for x in rets)/len(rets))
        out[d]=[cl[-1]/o0-1,vol,(max(x[2] for x in g)-min(x[3] for x in g))/o0,cl[-1]/cl[-4]-1,sum(1 for x in rets if x>0)/len(rets),(o0/prev-1) if prev else 0.0]
        prev=cl[-1]
    return out

def intraday_profile(bars=None):
    stats={}
    for t,o,h,l,c in (bars if bars is not None else intraday_load()):
        if o<=0: continue
        et=_et_of(t)
        if 9<=et.hour<=15:
            s=stats.setdefault(et.hour,[0.0,0,0]); r=c/o-1; s[0]+=r; s[1]+=int(r>0); s[2]+=1
    return [{"hour":hh,"avg_pct":s[0]/s[2]*100,"up_share":s[1]/s[2],"n":s[2]} for hh,s in sorted(stats.items()) if s[2]>=200]

def intraday_ab_test(dates,closes):
    """Same days, same split, same networks: daily features only versus daily plus hourly features. Scored on days neither has seen."""
    extra=intraday_features(); full=brain_feature_rows(dates,closes); base=[select_features(r,BRAIN_FEATURES) if r is not None else None for r in full]; ys=brain_labels(closes)
    rows=[i for i in range(len(closes)) if base[i] is not None and ys[i] is not None and dates[i] in extra]
    if len(rows)<600: return {"status":"waiting","days":len(rows),"message":"Hourly history covers %d usable days so far; the test needs 600."%len(rows)}
    n=len(rows); a=int(n*.6); b=int(n*.8); fit,val,hold=rows[:a-BRAIN_GAP],rows[a:b-BRAIN_GAP],rows[b:]; auc={}
    for name,use in (("daily_only",False),("with_hourly",True)):
        feats=[None]*len(closes)
        for i in rows: feats[i]=base[i]+(extra[dates[i]] if use else [])
        mean,scale=brain_stats([feats[i] for i in fit]); trn=brain_scaled(feats,ys,fit,mean,scale); vl=brain_scaled(feats,ys,val,mean,scale)
        members=[brain_fit_member(trn,vl,seed,8,60,"Hourly-data test (%s)"%name,0.5,0.0)[0] for seed in BRAIN_SEEDS]
        p=brain_predict_ids(brain_ensemble(members),mean,scale,feats,hold); auc[name]=auc_score([p[i] for i in hold],[ys[i] for i in hold])
    if auc["daily_only"] is None or auc["with_hourly"] is None: return {"status":"waiting","days":n,"message":"The unseen test period had no drops to score yet."}
    diff=auc["with_hourly"]-auc["daily_only"]; verdict="helps" if diff>=0.02 else ("hurts" if diff<=-0.02 else "no clear gain")
    return {"status":"done","tested_on":dates[-1],"days":n,"holdout_days":len(hold),"holdout_from":dates[hold[0]],"auc_daily_only":auc["daily_only"],"auc_with_hourly":auc["with_hourly"],"difference":diff,"verdict":verdict}

def intraday_autopilot():
    with INTRADAY_LOCK:
        try: intraday_collect()
        except Exception as exc: INTRADAY_STATE["status"]="collector problem: "+(str(exc)[:80] or exc.__class__.__name__)
        path=LEARN_DIR/"intraday_ab.json"
        if INTRADAY_STATE["ab"] is None:
            try: INTRADAY_STATE["ab"]=json.loads(path.read_text(encoding="utf-8"))
            except (OSError,json.JSONDecodeError): pass
        ab=INTRADAY_STATE["ab"]
        due=INTRADAY_STATE["days"]>=600 and (ab is None or ab.get("status")!="done" or (date.today()-date.fromisoformat(ab["tested_on"])).days>=7)
        if due:
            dates,closes=_load_csv_series(); INTRADAY_STATE["ab"]=intraday_ab_test(dates,closes)
            try: LEARN_DIR.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(INTRADAY_STATE["ab"]),encoding="utf-8")
            except OSError: pass

def intraday_note(h):
    s=INTRADAY_STATE; ab=s.get("ab"); parts=[]
    prof=intraday_profile() if (h<=5 and s["bars"]) else []
    if prof:
        parts.append("Hourly pattern learned from %d days of QQQ hourly data (average move of each hour, US Eastern): %s. These averages are tiny next to normal day-to-day noise, so they are not a trading signal on their own."%(s["days"],", ".join("%d:00 %+.3f%%"%(p["hour"],p["avg_pct"]) for p in prof)))
    if ab and ab.get("status")=="done":
        verb={"helps":"improved","hurts":"worsened","no clear gain":"did not clearly change"}[ab["verdict"]]
        parts.append("Hourly-data test (days neither version had seen, from %s): adding hourly features %s the networks' risk score, %.3f without versus %.3f with (0.50 is a coin flip). %s"%(ab["holdout_from"],verb,ab["auc_daily_only"],ab["auc_with_hourly"],"It is not used in the call yet." if ab["verdict"]!="helps" else "It gained enough to be worth wiring into the call."))
    elif s["days"]>0: parts.append("Hourly learning: %d usable days collected so far; the accuracy test starts at 600."%s["days"])
    else: parts.append("Hourly learning: %s."%s["status"])
    return " ".join(parts)

AUTOPILOT={"logged_for":None,"last_run":None,"note":""}

# ---- Paper trading (SIMULATED MONEY ONLY): lets the model's own calls place real orders on your free Alpaca PAPER account ----
# Safety: the host below is hardcoded to Alpaca's paper-trading endpoint. There is no setting anywhere in this file that points
# it at Alpaca's live-money endpoint (api.alpaca.markets) - switching this to real trading would require editing this code.
PAPER_HOST="https://paper-api.alpaca.markets"
PAPER_SYMBOL="QQQ"
PAPER_TARGET_PCT=0.9     # when BUY: put this fraction of paper account equity into QQQ. When SELL: go flat (0%).
TRADE_LOG=LEARN_DIR/"paper_trades.json"

def paper_state_path(): return LEARN_DIR/"paper_trading.json"

def paper_state():
    try: st=json.loads(paper_state_path().read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): st={"enabled":False}
    st.setdefault("trade_pct",PAPER_TARGET_PCT)   # fraction of paper equity to put into QQQ on a BUY call - owner-set, asked for once in the UI, not guessed
    st.setdefault("take_profit_pct",None)         # e.g. 0.05 = sell everything the moment unrealized profit on the open position hits 5%, regardless of what the model's own call says
    st.setdefault("stop_loss_pct",None)           # e.g. 0.04 = sell everything the moment an open position is DOWN 4%, to cap the loss instead of riding it out until the model's call flips
    return st

def paper_set_enabled(on):
    LEARN_DIR.mkdir(parents=True,exist_ok=True)
    st=paper_state(); st["enabled"]=bool(on); st["changed_at"]=datetime.now().isoformat(timespec="seconds")
    paper_state_path().write_text(json.dumps(st),encoding="utf-8")

def paper_set_config(trade_pct=None,take_profit_pct=None,stop_loss_pct=None):
    """Owner sets how much of the paper account each BUY call uses, and (optionally) automatic take-profit / stop-loss %s. These are informational settings the owner picks - the networks never see or react to them."""
    st=paper_state()
    if trade_pct is not None:
        tp=max(0.05,min(1.0,float(trade_pct))); st["trade_pct"]=tp
    if take_profit_pct is not None:
        st["take_profit_pct"]=(max(0.005,min(1.0,float(take_profit_pct))) if float(take_profit_pct)>0 else None)
    if stop_loss_pct is not None:
        st["stop_loss_pct"]=(max(0.005,min(1.0,float(stop_loss_pct))) if float(stop_loss_pct)>0 else None)
    paper_state_path().write_text(json.dumps(st),encoding="utf-8")
    return paper_summary()

def paper_request(method,path,body=None):
    if not (MARKET_API_KEY and MARKET_API_SECRET): raise ValueError("Connect your free Alpaca keys first (same keys used for market data also work for the paper account).")
    data=json.dumps(body).encode() if body is not None else None
    req=Request(PAPER_HOST+path,data=data,method=method,headers={"APCA-API-KEY-ID":MARKET_API_KEY,"APCA-API-SECRET-KEY":MARKET_API_SECRET,"Content-Type":"application/json","Accept":"application/json"})
    try:
        with urlopen(req,timeout=15) as r: return json.loads(r.read() or b"{}")
    except HTTPError as exc:
        detail=exc.read().decode("utf-8","ignore")[:200]
        if exc.code==404: return None
        if exc.code in (401,403): raise ValueError("Alpaca rejected these keys for paper trading. In your Alpaca dashboard, use the PAPER key pair (Paper Trading tab), not a data-only key.")
        raise ValueError("Alpaca paper API error %d: %s"%(exc.code,detail))

def paper_account(): return paper_request("GET","/v2/account")
def paper_position(symbol=PAPER_SYMBOL): return paper_request("GET","/v2/positions/"+symbol)

def paper_log(entry):
    try: log=json.loads(TRADE_LOG.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): log=[]
    log.append(entry); LEARN_DIR.mkdir(parents=True,exist_ok=True)
    TRADE_LOG.write_text(json.dumps(log[-500:]),encoding="utf-8")

def paper_sync_to_verdict(v=None):
    """Called automatically after each new daily close (only if the owner turned trading on). Moves the PAPER account to match the model's current call, using the owner's own chosen trade size."""
    st=paper_state()
    if not st.get("enabled"): return {"skipped":"paper trading is off"}
    v=v or final_verdict("next month")
    if v.get("interim"): return {"skipped":"brain still training"}
    if v.get("verdict")=="HOLD": return {"skipped":"MIRA returned HOLD; no paper order placed"}
    want_long = v["verdict"]=="BUY"
    acct=paper_account()
    if acct is None: return {"error":"could not read the paper account"}
    equity=float(acct.get("equity",0)); pos=paper_position()
    held_qty=float(pos.get("qty",0)) if pos else 0.0
    trade_pct=st.get("trade_pct",PAPER_TARGET_PCT)
    order=None
    if want_long and held_qty<=0:
        order=paper_request("POST","/v2/orders",{"symbol":PAPER_SYMBOL,"notional":round(equity*trade_pct,2),"side":"buy","type":"market","time_in_force":"day"})
    elif not want_long and held_qty>0:
        order=paper_request("POST","/v2/orders",{"symbol":PAPER_SYMBOL,"qty":str(held_qty),"side":"sell","type":"market","time_in_force":"day"})
    result={"as_of":v.get("as_of"),"verdict":v["verdict"],"confidence":v.get("confidence"),"equity_before":equity,"held_before":held_qty,"trade_pct_used":trade_pct,"order":order}
    if order: paper_log(result)
    return result

def paper_check_take_profit():
    """Runs on its own, independent of the daily model call: if the owner set a take-profit and/or stop-loss %, sell the whole position the moment either is hit - locking in a gain or capping a loss - instead of waiting (possibly weeks) for the model's own call to flip."""
    st=paper_state()
    if not st.get("enabled"): return {"skipped":"paper trading is off"}
    tp_target=st.get("take_profit_pct"); sl_target=st.get("stop_loss_pct")
    if not tp_target and not sl_target: return {"skipped":"no take-profit or stop-loss set - position will only change when the model's own call flips"}
    try:
        pos=paper_position()
        if not pos or float(pos.get("qty",0))<=0: return {"skipped":"no open position"}
        plpc=float(pos.get("unrealized_plpc",0) or 0)
        hit=None
        if tp_target and plpc>=tp_target: hit=("TAKE_PROFIT_SELL","reached %.2f%% profit (target was %.2f%%) - sold to lock it in"%(plpc*100,tp_target*100))
        elif sl_target and plpc<=-sl_target: hit=("STOP_LOSS_SELL","down %.2f%% (stop-loss was %.2f%%) - sold to cap the loss, this does not guarantee no loss, only limits it"%(plpc*100,sl_target*100))
        if not hit: return {"skipped":"at %.2f%% (take-profit %s, stop-loss %s)"%(plpc*100,("%.2f%%"%(tp_target*100)) if tp_target else "off",("-%.2f%%"%(sl_target*100)) if sl_target else "off")}
        order=paper_request("POST","/v2/orders",{"symbol":PAPER_SYMBOL,"qty":pos.get("qty"),"side":"sell","type":"market","time_in_force":"day"})
        result={"as_of":datetime.now().isoformat(timespec="seconds"),"verdict":hit[0],"reason":hit[1]+", independent of the model's own call","order":order}
        if order: paper_log(result)
        return result
    except ValueError as exc: return {"error":str(exc)}

def paper_summary():
    st=paper_state()
    out={"enabled":st.get("enabled",False),"symbol":PAPER_SYMBOL,"trade_pct":st.get("trade_pct",PAPER_TARGET_PCT),"take_profit_pct":st.get("take_profit_pct"),"stop_loss_pct":st.get("stop_loss_pct"),"endpoint":"Alpaca PAPER (simulated money only)"}
    if not (MARKET_API_KEY and MARKET_API_SECRET): out["error"]="Connect your free Alpaca keys first."; return out
    try:
        acct=paper_account()
        if acct is None: out["error"]="No paper account found for these keys."; return out
        pos=paper_position()
        out["account"]={"equity":float(acct.get("equity",0)),"cash":float(acct.get("cash",0)),"buying_power":float(acct.get("buying_power",0)),"start_equity":float(acct.get("last_equity",0))}
        out["position"]={"qty":float(pos.get("qty",0)),"market_value":float(pos.get("market_value",0)),"unrealized_pl":float(pos.get("unrealized_pl",0)),"unrealized_plpc":float(pos.get("unrealized_plpc",0) or 0)} if pos else None
        try: out["trades"]=json.loads(TRADE_LOG.read_text(encoding="utf-8"))[-25:]
        except (OSError,json.JSONDecodeError): out["trades"]=[]
    except ValueError as exc: out["error"]=str(exc)
    return out

def explain_graded_call(e,dates,closes,brain):
    """A real, numbers-only explanation of one past call: what was predicted, what happened, and which inputs looked most unusual that day. No invented reasoning."""
    i={d:k for k,d in enumerate(dates)}.get(e.get("as_of"))
    if i is None: return None
    out={"as_of":e["as_of"],"verdict":e["verdict"],"risk_score":e.get("risk_score"),"alarm_level":e.get("alarm_level")}
    if e.get("dropped_8pct_21d") is not None:
        end=min(i+BRAIN_HORIZON,len(closes)-1); worst=min(closes[i+1:end+1])/closes[i]-1 if end>i else None
        out["actual_move_pct"]=round(worst*100,1) if worst is not None else None
        out["dropped_8pct_21d"]=e["dropped_8pct_21d"]
        out["correct"]=(e["verdict"]=="SELL")==e["dropped_8pct_21d"]
    if brain and brain.get("eval"):
        names=brain.get("features",BRAIN_FEATURES)
        full=brain_feature_rows(dates,closes)
        if i<len(full) and full[i] is not None:
            row=select_features(full[i],names)
            mean,scale=brain["eval"]["mean"],brain["eval"]["scale"]
            z=[(row[j]-mean[j])/(scale[j] or 1) for j in range(len(names))]
            ranked=sorted(zip(names,row,z),key=lambda t:-abs(t[2]))[:3]
            out["most_unusual_inputs"]=[{"feature":n,"value":round(v,4),"how_unusual":round(abs(zz),1)} for n,v,zz in ranked]
    parts=["On %s the call was %s, with a drop-risk score of %s against an alarm level of %s."%(out["as_of"],out["verdict"],("%.0f%%"%(100*out["risk_score"])) if out.get("risk_score") is not None else "n/a",("%.0f%%"%(100*out["alarm_level"])) if out.get("alarm_level") is not None else "n/a")]
    if "actual_move_pct" in out and out["actual_move_pct"] is not None:
        parts.append("Over the following month the worst point was %+.1f%%, which %s a drop of 8%% or more, so the call was %s."%(out["actual_move_pct"],"was" if out["dropped_8pct_21d"] else "was not","right" if out["correct"] else "wrong"))
    if out.get("most_unusual_inputs"):
        parts.append("The most unusual inputs that day: "+", ".join("%s (%.1f standard deviations from normal)"%(m["feature"],m["how_unusual"]) for m in out["most_unusual_inputs"])+".")
    parts.append("This is a description of what the numbers were, not the network explaining its own thinking - the networks themselves don't reason in words.")
    out["explanation"]=" ".join(parts)
    return out

def recent_mistakes(n=5):
    try: ledger=json.loads((LEARN_DIR/"ledger.json").read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return []
    wrong=[e for e in ledger if "dropped_8pct_21d" in e and (e["verdict"]=="SELL")!=e["dropped_8pct_21d"]]
    dates,closes=load_price_series(); brain=brain_load()
    return [x for x in (explain_graded_call(e,dates,closes,brain) for e in wrong[-n:]) if x]

def target_path(): return LEARN_DIR/"target.json"

def target_get():
    try: return json.loads(target_path().read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return None

def target_set(pct,by_date,note=""):
    LEARN_DIR.mkdir(parents=True,exist_ok=True)
    t={"target_pct":float(pct),"by_date":by_date,"note":note[:200],"set_at":datetime.now().isoformat(timespec="seconds")}
    target_path().write_text(json.dumps(t),encoding="utf-8"); return t

def target_progress():
    """Informational only - purely descriptive, never changes any verdict, position size, or trading decision."""
    t=target_get()
    if not t: return None
    out=dict(t)
    try:
        st=paper_summary(); acct=st.get("account")
        if acct and acct.get("start_equity"): out["paper_return_pct"]=round(100*(acct["equity"]/acct["start_equity"]-1),2)
    except Exception: pass
    dates,closes=load_price_series()
    try:
        start_i=next(i for i,d in enumerate(dates) if d>=t["set_at"][:10])
        out["buy_and_hold_pct"]=round(100*(closes[-1]/closes[start_i]-1),2)
    except StopIteration: pass
    out["note_fixed"]="This target is informational only. It does not change the model's verdict, confidence, or any position size - the networks never trade harder or take more risk to try to catch up to a target."
    return out

def autopilot_once():
    """Refresh both model families asynchronously after new completed daily data arrives."""
    dates,closes=_load_csv_series(); brain=brain_load()
    brain_due=(brain is None or brain.get("data_basis")!="official_daily_closes_v1" or
               len(closes)-brain.get("checked_rows",brain.get("rows_at_training",len(closes)))>=BRAIN_GAP)
    # Serialize training jobs: the risk brain and nine-MLP directional ensemble
    # should not compete for a small Render CPU at the same time.
    if brain_due and not BRAIN_JOB["running"] and not NASDAQ_JOB["running"]:
        start_brain_job(False)
    elif not BRAIN_JOB["running"] and not NASDAQ_JOB["running"]:
        start_nasdaq_training(False)
    try: intraday_autopilot()
    except Exception as exc: INTRADAY_STATE["status"]="hourly learning problem: "+str(exc)[:80]
    update_directional_ledger(dates,closes,None)  # grade prior forecasts after the next close arrives
    fusion=load_saved_directional_fusion(dates[-1])
    if fusion:
        update_directional_ledger(dates,closes,fusion)  # log today's fresh inference exactly once
    official=PRICE_STATE.get("last_csv_date")
    if brain is not None and official and AUTOPILOT["logged_for"]!=official:
        v=final_verdict("tomorrow"); AUTOPILOT["logged_for"]=official
        AUTOPILOT["note"]="Logged decision %s; neural risk classification %s; %s confidence; data %s."%(v.get("verdict"),v.get("risk_state",v.get("brain_verdict")),v.get("confidence"),v.get("model_data_as_of",v.get("as_of")))
        try: paper_sync_to_verdict(v)
        except Exception as exc: AUTOPILOT["note"]+=" Paper-trade sync problem: "+str(exc)[:100]
    AUTOPILOT["last_run"]=datetime.now().isoformat(timespec="seconds")

def price_updater_loop():
    while True:
        try: refresh_prices(force=True); autopilot_once()
        except Exception as exc: AUTOPILOT["note"]="Autopilot error: "+str(exc)[:100]
        time.sleep(300 if market_session()=="regular session" else 1800)

def paper_watch_loop():
    """Separate, fast loop just for the take-profit/stop-loss check: it only reads your Alpaca PAPER position (already live-marked by Alpaca itself), so it's cheap enough to run every few seconds instead of waiting on the heavier daily-data refresh above. This is what actually catches a fast intraday move - a crash in one minute doesn't have to wait on the 5-minute cycle."""
    while True:
        gap=5 if market_session()=="regular session" else 60   # every 5s while the market's open, every 60s otherwise (nothing fills outside market hours anyway)
        try: paper_check_take_profit()
        except Exception: pass
        time.sleep(gap)

def trend_positions(closes,n,band):
    """1 = invested (BUY), 0 = defensive (SELL). Switches only when price is clearly beyond the band around its n-day average."""
    cs=[0.0]
    for v in closes: cs.append(cs[-1]+v)
    pos=[]; cur=1
    for i in range(len(closes)):
        if i+1<n: pos.append(None); continue
        ma=(cs[i+1]-cs[i+1-n])/n; x=closes[i]/ma-1
        if x>band: cur=1
        elif x<-band: cur=0
        pos.append(cur)
    return pos

def evaluate_positions(closes,pos,start,end,cost=0.0002):
    """Decide at the close of day i-1, earn day i's return: no look-ahead. cost = 2 basis points per switch."""
    eq=1.0; peak=1.0; mdd=0.0; rets=[]
    for i in range(max(start,2),end):
        p=pos[i-1]
        if p is None: continue
        q=pos[i-2] if pos[i-2] is not None else p
        r=p*(closes[i]/closes[i-1]-1)-cost*abs(p-q)
        rets.append(r); eq*=1+r; peak=max(peak,eq); mdd=min(mdd,eq/peak-1)
    if len(rets)<50: return None
    mean=sum(rets)/len(rets); sd=math.sqrt(sum((x-mean)**2 for x in rets)/len(rets))
    years=len(rets)/252
    return {"cagr":eq**(1/years)-1,"sharpe":(mean/sd*math.sqrt(252)) if sd>0 else 0.0,"max_drawdown":mdd,"days":len(rets)}

def horizon_hit_rate(closes,pos,start,end,h=20):
    hits=n=ups=0
    for i in range(max(start,0),end-h):
        if pos[i] is None: continue
        up=closes[i+h]>closes[i]; n+=1; ups+=int(up); hits+=int((pos[i]==1)==up)
    return {"n":n,"rule_hit_rate":hits/n,"always_buy_rate":ups/n} if n else None

def eval_start(dates):
    return max(300,next((i for i,d in enumerate(dates) if d>="2000-01-01"),300))

def self_improve(dates,closes):
    """Re-test every rule setting on all history except the newest 2 years, keep the champion unless a challenger clearly wins."""
    split=len(closes)-HOLDOUT_DAYS; start=eval_start(dates); scored={}
    for n,b in TREND_GRID:
        s=evaluate_positions(closes,trend_positions(closes,n,b),start,split)
        if s: scored[(n,b)]=s["sharpe"]+0.5*s["max_drawdown"]   # reward steadiness, punish deep drops
    best=max(scored,key=scored.get)
    path=LEARN_DIR/"champion.json"
    try: champ=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): champ=None
    chosen=best; note="First selection: best of %d rule settings on history before the last 2 years."%len(scored)
    if champ and (champ.get("n"),champ.get("band")) in scored:
        key=(champ["n"],champ["band"])
        if scored[best]>scored[key]+0.03 and best!=key:
            note="Promoted a new setting (%d-day, %.0f%% band) because it beat the old one by a clear margin."%(best[0],best[1]*100)
        else: chosen=key; note="Kept the current setting; no challenger beat it by a clear margin (%d settings tested)."%len(scored)
    record={"n":chosen[0],"band":chosen[1],"score":round(scored[chosen],4),"as_of":dates[-1],"note":note}
    if not champ or (champ.get("n"),champ.get("band"),champ.get("as_of"))!=(record["n"],record["band"],record["as_of"]):
        try: LEARN_DIR.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(record,indent=2),encoding="utf-8")
        except OSError: pass
    return chosen,note,len(scored)

def update_ledger(dates,closes,verdict,confidence,p_risk,threshold):
    """Log each call, then grade it against what the market actually did over the next month."""
    path=LEARN_DIR/"ledger.json"
    try: ledger=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): ledger=[]
    index={d:i for i,d in enumerate(dates)}
    if not any(e.get("as_of")==dates[-1] for e in ledger):
        ledger.append({"as_of":dates[-1],"verdict":verdict,"confidence":confidence,"risk_score":p_risk,"alarm_level":threshold})
    for e in ledger:
        i=index.get(e.get("as_of"))
        if i is None: continue
        if i+1<len(closes): e["next_day_up"]=closes[i+1]>closes[i]
        if i+BRAIN_HORIZON<len(closes): e["dropped_8pct_21d"]=min(closes[i+1:i+BRAIN_HORIZON+1])/closes[i]-1<=-BRAIN_DROP
    try: LEARN_DIR.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(ledger[-2000:],indent=1),encoding="utf-8")
    except OSError: pass
    g=[e for e in ledger if "dropped_8pct_21d" in e]
    graded={"n":len(g)}
    if g:
        graded["hit_rate"]=sum(int((e["verdict"]=="SELL")==e["dropped_8pct_21d"]) for e in g)/len(g)
        graded["always_buy_rate"]=sum(int(not e["dropped_8pct_21d"]) for e in g)/len(g)
    return {"calls_logged":len(ledger),"graded":graded}

def log_question(prompt,verdict):
    try:
        LEARN_DIR.mkdir(parents=True,exist_ok=True)
        with (LEARN_DIR/"questions.jsonl").open("a",encoding="utf-8") as f:
            f.write(json.dumps({"at":datetime.now().isoformat(timespec="seconds"),"question":prompt[:300],"verdict":verdict})+"\n")
    except OSError: pass

def parse_horizon(prompt):
    """Work out the time frame the person is asking about; default to about one month."""
    p=prompt.lower()
    m=re.search(r"(\d+(?:\.\d+)?)\s*[- ]?\s*(day|week|month|year|yr)s?\b",p)
    if m:
        k=float(m.group(1)); unit=m.group(2); days=int(k*{"day":1,"week":5,"month":21,"year":252,"yr":252}[unit])
        return max(1,min(days,1260)),"the next %s %s%s"%(m.group(1),"year" if unit=="yr" else unit,"" if k==1 else "s"),False
    for pattern,days,label in ((r"next year|this year|a year|12 months|long.?term|retire|years",252,"the next year"),(r"6 months|six months|half.?year",126,"the next 6 months"),(r"quarter|3 months|three months",63,"the next 3 months"),(r"month",21,"the next month"),(r"week",5,"the next week"),(r"tomorrow|next session|tonight|day.?trad|intraday|next day",1,"tomorrow (the next trading day)"),(r"\btoday\b|right now|\bnow\b",1,"today (judged on the next trading day)")):
        if re.search(pattern,p): return days,label,False
    return 21,"about the next month",True

def horizon_stats(closes,pos,start,h,state):
    ups=0; rets=[]
    for i in range(start,len(closes)-h):
        if pos[i] is None or (state is not None and pos[i]!=state): continue
        r=closes[i+h]/closes[i]-1; rets.append(r); ups+=int(r>0)
    if len(rets)<100: return None
    rets.sort(); m=len(rets)
    return {"n":m,"p_up":ups/m,"median":rets[m//2],"p5":rets[m//20],"worst":rets[0]}

# ---- Neural risk brain: the networks decide the call ----
# Three small networks (an ensemble) read 13 price features and output the chance of an 8%+ drop within ~21 trading days.
# Risk >= alarm level -> SELL, otherwise BUY. Trained and tested with a chronological fit / validation / holdout split.
import threading
BRAIN_FEATURES=["return_1d","return_2d","return_5d","return_10d","return_20d","volatility_5d","volatility_20d","return_60d","volatility_60d","vs_50day_avg","vs_250day_avg","below_252d_high","vol_5_vs_60"]
BRAIN_HORIZON=21; BRAIN_DROP=0.08; BRAIN_WARMUP=252; BRAIN_GAP=21
BRAIN_SEEDS=(17,41,73); BRAIN_HIDDEN_CHOICES=(8,12,6)
MACRO_FEATURES=["yield_10y_chg_20d","credit_spread_level","recession_flag"]
ALL_FEATURES=BRAIN_FEATURES+MACRO_FEATURES
FEATURE_INDEX={name:i for i,name in enumerate(ALL_FEATURES)}

def select_features(row,names):
    """Picks out a candidate's own chosen inputs, in its own order, from the full feature row."""
    return [row[FEATURE_INDEX[n]] for n in names]

# ---- Macro/fundamental data (free, from FRED): 10-year Treasury yield, high-yield credit spread, NBER recession flag.
# Real new information for the networks, beyond price alone - fetched the same safe, read-only way as the price history.
import bisect
MACRO_SERIES_IDS={"yield_10y":"DGS10","credit_spread":"BAMLH0A0HYM2","recession":"USREC"}
MACRO_CACHE={"at":0.0,"series":{},"errors":{}}

def _fred_series(series_id,cosd="1985-01-01"):
    if FRED_API_KEY:
        rows=_fred_api(series_id,cosd)
        return [d for d,_ in rows],[v for _,v in rows]
    return _fred_series_old(series_id,cosd)

def _fred_series_old(series_id,cosd="1985-01-01"):
    req=Request("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s"%(series_id,cosd),headers={"User-Agent":"MIRA-research/1.0"})
    last_exc=None
    for attempt in range(3):
        try:
            with urlopen(req,timeout=25) as response: text=response.read().decode("utf-8-sig")
            break
        except Exception as exc: last_exc=exc; time.sleep(2*(attempt+1))
    else: raise last_exc
    out=[]; reader=csv.reader(io.StringIO(text)); next(reader,None)
    for rec in reader:
        if len(rec)<2: continue
        try: d=parse_date(rec[0]).date().isoformat(); v=float(rec[1])
        except (ValueError,TypeError): continue
        if math.isfinite(v): out.append((d,v))
    out.sort()
    return [d for d,_ in out],[v for _,v in out]

def refresh_macro(force=False):
    """Best-effort, cached for hours since these move slowly. Any feature using this data defaults to neutral if it is not available yet, so nothing breaks without it."""
    if not force and time.time()-MACRO_CACHE["at"]<21600: return MACRO_CACHE
    MACRO_CACHE["at"]=time.time()
    for name,sid in MACRO_SERIES_IDS.items():
        try: MACRO_CACHE["series"][name]=_fred_series(sid)
        except Exception as exc: MACRO_CACHE["errors"][name]=str(exc)[:100]
    return MACRO_CACHE

def _macro_value_on(name,d,default):
    ser=MACRO_CACHE["series"].get(name)
    if not ser or not ser[0]: return default
    dates,vals=ser; i=bisect.bisect_right(dates,d)-1
    return vals[i] if i>=0 else default

def macro_row(d):
    """Three macro inputs as of date d: how much the 10-year yield has moved in ~20 trading days, the current high-yield credit spread, and whether the US was officially in an NBER recession that month. Neutral (0.0) wherever data isn't available yet."""
    y_now=_macro_value_on("yield_10y",d,None)
    d_back=(date.fromisoformat(d)-timedelta(days=28)).isoformat()
    y_then=_macro_value_on("yield_10y",d_back,y_now)
    yield_chg=(y_now-y_then) if (y_now is not None and y_then is not None) else 0.0
    credit=_macro_value_on("credit_spread",d,0.0)
    rec=_macro_value_on("recession",d,0.0)
    return [yield_chg,credit,0.0]  # recession flag disabled: NBER announces dates months late, so using it same-day leaks the future

BRAIN_JOB={"running":False,"message":"","error":None,"started":None}
BRAIN_LOCK=threading.Lock()

def brain_path(): return DATA_DIR/"models"/"mira_risk_brain.json"

def brain_load():
    try: return json.loads(brain_path().read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return None

def brain_feature_rows(dates,closes):
    """Returns the full 16-wide feature row (13 price-based + 3 macro) for every day; None wherever there isn't enough price history yet. A model only ever sees the subset named in its own "features" list - see select_features()."""
    n=len(closes); daily=[0.0]+[closes[i]/closes[i-1]-1 for i in range(1,n)]
    cs=[0.0]
    for v in closes: cs.append(cs[-1]+v)
    def sd(i,k):
        seg=daily[i-k+1:i+1]; m=sum(seg)/k; return math.sqrt(sum((x-m)**2 for x in seg)/k)
    rows=[None]*n
    for i in range(BRAIN_WARMUP,n):
        v5,v20,v60=sd(i,5),sd(i,20),sd(i,60)
        rows[i]=[daily[i],closes[i]/closes[i-2]-1,closes[i]/closes[i-5]-1,closes[i]/closes[i-10]-1,closes[i]/closes[i-20]-1,v5,v20,closes[i]/closes[i-60]-1,v60,closes[i]/((cs[i+1]-cs[i-49])/50)-1,closes[i]/((cs[i+1]-cs[i-249])/250)-1,closes[i]/max(closes[i-251:i+1])-1,(v5/v60) if v60>0 else 1.0]+macro_row(dates[i])
    return rows

def brain_labels(closes):
    n=len(closes); return [(1 if min(closes[i+1:i+BRAIN_HORIZON+1])/closes[i]-1<=-BRAIN_DROP else 0) if i+BRAIN_HORIZON<n else None for i in range(n)]

def auc_score(ps,ys):
    pairs=sorted(zip(ps,ys)); n=len(pairs); pos=sum(y for _,y in pairs); neg=n-pos
    if pos==0 or neg==0: return None
    rank_sum=0.0; i=0
    while i<n:
        j=i
        while j+1<n and pairs[j+1][0]==pairs[i][0]: j+=1
        avg=(i+j)/2+1
        for k in range(i,j+1):
            if pairs[k][1]: rank_sum+=avg
        i=j+1
    return (rank_sum-pos*(pos+1)/2)/(pos*neg)

def auc_block_ci(ps,ys,block=21,n_boot=300,seed=7):
    """95% range for AUC using a block bootstrap (21-day blocks). Daily rows overlap, so a plain bootstrap would look far too certain."""
    m=len(ps)
    if m<block*4: return None
    rnd=random.Random(seed); starts=list(range(0,m,block)); vals=[]
    for _ in range(n_boot):
        bp=[]; by=[]
        for _b in range(len(starts)):
            s=rnd.choice(starts); bp.extend(ps[s:s+block]); by.extend(ys[s:s+block])
        a=auc_score(bp,by)
        if a is not None: vals.append(a)
    if len(vals)<50: return None
    vals.sort()
    return [vals[int(len(vals)*0.025)],vals[int(len(vals)*0.975)-1]]

def brain_fit_member(train,valid,seed,hidden,max_epochs,label,base,span):
    rnd=random.Random(seed); dim=len(train[0]["x"])
    m={"w1":[[rnd.uniform(-.15,.15) for _ in range(dim+1)] for _ in range(hidden)],"w2":[rnd.uniform(-.15,.15) for _ in range(hidden+1)]}
    data=list(train); best=None; best_loss=float("inf"); best_epoch=0; stale=0
    for ep in range(max_epochs):
        rnd.shuffle(data); train_pass(m,data,.01/(1+ep*.02))
        if valid:
            vl=cross_entropy(m,valid)
            if vl<best_loss-1e-5: best_loss=vl; best_epoch=ep+1; stale=0; best={"w1":[r.copy() for r in m["w1"]],"w2":m["w2"].copy()}
            else: stale+=1
        else: best_epoch=ep+1
        if ep%4==0 or ep==max_epochs-1:
            visible_status("training","%s · epoch %d/%d"%(label,ep+1,max_epochs),base+span*(ep+1)/max_epochs,{"label":label,"epoch":ep+1,"epochs":max_epochs,"model":m,"sample":data[-1]["x"]})
        if valid and ep+1>=8 and stale>=6: break
    return (best if valid else m),best_epoch

def brain_scaled(feats,ys,ids,mean,scale):
    return [{"x":[(v-mean[j])/scale[j] for j,v in enumerate(feats[i])],"y":ys[i],"id":i} for i in ids]

def brain_stats(vals):
    mean=[sum(v[j] for v in vals)/len(vals) for j in range(len(vals[0]))]
    scale=[math.sqrt(sum((v[j]-mean[j])**2 for v in vals)/len(vals)) or 1.0 for j in range(len(vals[0]))]
    return mean,scale

def brain_predict_ids(model,mean,scale,feats,ids):
    return {i:predict(model,[(v-mean[j])/scale[j] for j,v in enumerate(feats[i])]) for i in ids}

def brain_ensemble(members): return {"members":members,"w1":members[0]["w1"],"w2":members[0]["w2"]}

def brain_split(dates,closes):
    feats=brain_feature_rows(dates,closes); ys=brain_labels(closes)
    idx=[i for i in range(len(closes)) if feats[i] is not None and ys[i] is not None]
    m=len(idx); a=int(m*.6); b=int(m*.8)
    return feats,ys,idx,idx[:max(50,a-BRAIN_GAP)],idx[a:max(a+50,b-BRAIN_GAP)],idx[b:]

def brain_evaluate_block(closes,ids,probs,thr):
    """How well do these risk scores do on a block of days they have not seen?"""
    yv=[brain_labels_cache(closes)[i] for i in ids]; ps=[probs[i] for i in ids]
    n=len(closes); pos=[None]*n
    for i in ids: pos[i]=0.0 if probs[i]>=thr else 1.0
    strat=evaluate_positions(closes,pos,ids[0],ids[-1]+1); hold=evaluate_positions(closes,[1.0]*n,ids[0],ids[-1]+1)
    flagged=[y for p,y in zip(ps,yv) if p>=thr]; calm=[y for p,y in zip(ps,yv) if p<thr]
    return {"rows":len(ids),"auc":auc_score(ps,yv),"auc_ci":auc_block_ci(ps,yv),"base_rate":sum(yv)/len(yv),"flagged_drop_rate":(sum(flagged)/len(flagged)) if flagged else None,"calm_drop_rate":(sum(calm)/len(calm)) if calm else None,"flagged_share":len(flagged)/len(ids),"strategy":strat,"buy_and_hold":hold,"invested_share":sum(1 for i in ids if pos[i]==1.0)/len(ids)}

_LABEL_CACHE={}
def brain_labels_cache(closes):
    key=(len(closes),closes[-1])
    if key not in _LABEL_CACHE: _LABEL_CACHE.clear(); _LABEL_CACHE[key]=brain_labels(closes)
    return _LABEL_CACHE[key]

# ---------------------------------------------------------------------------
# MIRA Quantitative Intelligence Layer (pure Python; no mandatory dependencies)
# ---------------------------------------------------------------------------
def _q_log_returns(closes):
    out=[]
    for a,b in zip(closes[:-1],closes[1:]):
        if a and b and a>0 and b>0 and math.isfinite(a) and math.isfinite(b): out.append(math.log(b/a))
    return out

def _q_norm_cdf(x):
    return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))


# Reverse-mode automatic differentiation for scalar computational graphs.
# The directional Newton solver below uses its exact gradient and the exact
# logistic-regression Hessian (X'WX + ridge); no numerical-difference Hessian.
class _QAD:
    __slots__=("v","g","parents")
    def __init__(self,v,parents=()): self.v=float(v); self.g=0.0; self.parents=parents
    def __add__(self,o):
        o=o if isinstance(o,_QAD) else _QAD(o)
        return _QAD(self.v+o.v,((self,1.0),(o,1.0)))
    __radd__=__add__
    def __neg__(self): return _QAD(-self.v,((self,-1.0),))
    def __sub__(self,o): return self+(-o if isinstance(o,_QAD) else -float(o))
    def __rsub__(self,o): return (-self)+o
    def __mul__(self,o):
        o=o if isinstance(o,_QAD) else _QAD(o)
        return _QAD(self.v*o.v,((self,o.v),(o,self.v)))
    __rmul__=__mul__
    def backward(self):
        order=[]; seen=set()
        def visit(n):
            if id(n) in seen:return
            seen.add(id(n))
            for par,_ in n.parents:visit(par)
            order.append(n)
        visit(self); self.g=1.0
        for n in reversed(order):
            for par,local in n.parents:par.g+=n.g*local

def _q_sigmoid(z):
    if z>=0:
        e=math.exp(-min(z,60)); return 1/(1+e)
    e=math.exp(max(z,-60)); return e/(1+e)

def _q_solve_linear(a,b):
    """Pivoted Gauss-Jordan solve for the damped Newton system."""
    n=len(b); m=[list(map(float,a[i]))+[float(b[i])] for i in range(n)]
    for c in range(n):
        pivot=max(range(c,n),key=lambda r:abs(m[r][c]))
        if abs(m[pivot][c])<1e-12: raise ArithmeticError('singular Hessian')
        m[c],m[pivot]=m[pivot],m[c]; d=m[c][c]
        for j in range(c,n+1):m[c][j]/=d
        for r in range(n):
            if r==c:continue
            f=m[r][c]
            for j in range(c,n+1):m[r][j]-=f*m[c][j]
    return [m[i][n] for i in range(n)]

def _q_logistic_objective_ad(rows,theta,l2):
    params=[_QAD(v) for v in theta]; loss=_QAD(0.0)
    for row in rows:
        z=params[0]
        for j,x in enumerate(row['x']):z=z+params[j+1]*x
        # stable softplus(z)-y*z
        zv=z.v
        if zv>0:
            ev=math.exp(-min(zv,60.0))
            en=_QAD(ev,((z,-ev if zv<60.0 else 0.0),))
            ln=_QAD(math.log1p(ev),((en,1.0/(1.0+ev)),))
            sp=z+ln
        else:
            ev=math.exp(max(zv,-60.0))
            en=_QAD(ev,((z,ev if zv>-60.0 else 0.0),))
            sp=_QAD(math.log1p(ev),((en,1.0/(1.0+ev)),))
        loss=loss+sp-z*row['y']
    penalty=_QAD(0.0)
    for w in params[1:]:penalty=penalty+w*w*(0.5*l2)
    loss=(loss+penalty)*(1.0/max(1,len(rows)))
    loss.backward()
    return loss.v,[v.g for v in params]

def _q_fit_logistic_newton(rows,l2=0.08,max_iter=45):
    """Ridge logistic expert; reverse-mode AD gradient + exact damped Hessian."""
    if len(rows)<30:return {'available':False,'reason':'Insufficient calibration rows'}
    d=len(rows[0]['x']); theta=[0.0]*(d+1); theta[0]=math.log((sum(r['y'] for r in rows)+1)/(len(rows)-sum(r['y'] for r in rows)+1))
    converged=False; last=float('inf')
    for it in range(max_iter):
        loss,grad=_q_logistic_objective_ad(rows,theta,l2)
        h=[[0.0]*(d+1) for _ in range(d+1)]
        for row in rows:
            z=theta[0]+sum(theta[j+1]*x for j,x in enumerate(row['x']))
            q=_q_sigmoid(z); w=q*(1-q)/len(rows)
            xx=[1.0]+row['x']
            for a in range(d+1):
                for b in range(d+1):h[a][b]+=w*xx[a]*xx[b]
        for j in range(1,d+1):h[j][j]+=l2/len(rows)
        # Levenberg damping guarantees a descent-capable solve in near-collinear data.
        step=None
        for damping in (1e-8,1e-6,1e-4,1e-2,0.1,1.0):
            hh=[r.copy() for r in h]
            for j in range(d+1):hh[j][j]+=damping
            try:
                candidate=_q_solve_linear(hh,grad)
                if all(math.isfinite(v) for v in candidate):step=candidate;break
            except ArithmeticError:pass
        if step is None:break
        rate=1.0; accepted=False
        for _ in range(18):
            trial=[theta[j]-rate*step[j] for j in range(d+1)]
            newloss,_=_q_logistic_objective_ad(rows,trial,l2)
            if math.isfinite(newloss) and newloss<=loss+1e-10:
                theta=trial;last=newloss;accepted=True;break
            rate*=0.5
        if not accepted:break
        if max(abs(rate*v) for v in step)<1e-6:converged=True;break
    return {'available':True,'theta':theta,'iterations':it+1,'converged':converged,
            'objective':last if math.isfinite(last) else _q_logistic_objective_ad(rows,theta,l2)[0],
            'optimizer':'reverse-mode AD gradient + exact Hessian damped Newton','l2':l2}

def _q_logistic_predict(model,x):
    if not model or not model.get('available'):return 0.5
    t=model['theta']; return min(.999,max(.001,_q_sigmoid(t[0]+sum(t[j+1]*v for j,v in enumerate(x)))))

def _q_hmm_gaussian(returns,iterations=35):
    """Two-state Gaussian HMM; Baum-Welch EM + causal filtered terminal state."""
    x=list(returns[-1800:]); n=len(x)
    if n<120:return {'available':False,'reason':'At least 120 returns are required'}
    mu=sum(x)/n; var=max(sum((v-mu)**2 for v in x)/n,1e-8); sd=math.sqrt(var)
    means=[mu-0.45*sd,mu+0.45*sd]; vars_=[max(var*.55,1e-8),max(var*1.8,1e-8)]
    trans=[[.96,.04],[.08,.92]]; pi=[.5,.5]
    def emissions(v):
        out=[]
        for m,vv in zip(means,vars_):out.append(max(-700,-.5*(math.log(2*math.pi*vv)+(v-m)**2/vv)))
        mx=max(out); return [math.exp(q-mx) for q in out]
    for _ in range(iterations):
        emit=[emissions(v) for v in x]
        alpha=[[0.0,0.0] for _ in x]; scales=[0.0]*n
        for k in range(2):alpha[0][k]=pi[k]*emit[0][k]
        scales[0]=max(sum(alpha[0]),1e-300);alpha[0]=[v/scales[0] for v in alpha[0]]
        for t in range(1,n):
            for j in range(2):alpha[t][j]=emit[t][j]*sum(alpha[t-1][i]*trans[i][j] for i in range(2))
            scales[t]=max(sum(alpha[t]),1e-300);alpha[t]=[v/scales[t] for v in alpha[t]]
        beta=[[1.0,1.0] for _ in x]
        for t in range(n-2,-1,-1):
            for i in range(2):beta[t][i]=sum(trans[i][j]*emit[t+1][j]*beta[t+1][j] for j in range(2))/scales[t+1]
        gamma=[]
        for t in range(n):
            q=[alpha[t][k]*beta[t][k] for k in range(2)]; den=max(sum(q),1e-300);gamma.append([v/den for v in q])
        xi_sum=[[1e-3,1e-3],[1e-3,1e-3]]
        for t in range(n-1):
            q=[[alpha[t][i]*trans[i][j]*emit[t+1][j]*beta[t+1][j] for j in range(2)] for i in range(2)]
            den=max(sum(sum(r) for r in q),1e-300)
            for i in range(2):
                for j in range(2):xi_sum[i][j]+=q[i][j]/den
        trans=[[r[0]/sum(r),r[1]/sum(r)] for r in xi_sum]
        for k in range(2):
            den=max(sum(g[k] for g in gamma),1e-9)
            means[k]=sum(gamma[t][k]*x[t] for t in range(n))/den
            vars_[k]=max(sum(gamma[t][k]*(x[t]-means[k])**2 for t in range(n))/den,1e-8)
        # State 0 is defined as lower variance after every M-step.
        if vars_[0]>vars_[1]:
            means.reverse();vars_.reverse();trans=[[trans[1][1],trans[1][0]],[trans[0][1],trans[0][0]]];pi.reverse()
        pi=gamma[0]
    low_high=alpha[-1][1] if vars_[1]>=vars_[0] else alpha[-1][0]
    # Transition prediction one step beyond the final filtered observation.
    pred=[alpha[-1][0]*trans[0][j]+alpha[-1][1]*trans[1][j] for j in range(2)]
    return {'available':True,'low_state_volatility_pct':100*math.sqrt(vars_[0]),
            'high_state_volatility_pct':100*math.sqrt(vars_[1]),
            'high_volatility_probability_next':pred[1],
            'filtered_high_volatility_probability':low_high,
            'transition_matrix':trans,'state_means_daily_pct':[100*means[0],100*means[1]],
            'fit_rows':n,'iterations':iterations,'method':'two-state Gaussian HMM; Baum-Welch EM and forward filtering'}

def _q_kalman_trend(closes, process_level=1e-6, process_trend=1e-8, observation=2.5e-4):
    """Two-state local-linear-trend Kalman filter on log prices; causal filtering only."""
    if len(closes)<5: return {"available":False}
    z=[math.log(float(x)) for x in closes if x and x>0 and math.isfinite(x)]
    if len(z)<5: return {"available":False}
    level=z[0]; trend=(z[min(4,len(z)-1)]-z[0])/min(4,len(z)-1)
    p00=observation; p01=0.0; p11=observation
    for obs in z[1:]:
        pred_level=level+trend; pred_trend=trend
        a00=p00+p01*2+p11+process_level; a01=p01+p11; a11=p11+process_trend
        innovation=obs-pred_level; s=a00+observation
        if s<=0 or not math.isfinite(s): continue
        k0=a00/s; k1=a01/s
        level=pred_level+k0*innovation; trend=pred_trend+k1*innovation
        p00=(1-k0)*a00; p01=(1-k0)*a01; p11=a11-k1*a01
    return {"available":True,"log_level":level,"daily_log_trend":trend,
            "trend_pct_per_session":100*(math.exp(trend)-1),
            "innovation_sigma_pct":100*math.sqrt(max(observation,1e-12)),
            "method":"causal local-linear-trend Kalman filter"}

def _q_garch11(returns):
    """Small bounded grid fit for GARCH(1,1); avoids unstable unconstrained optimizers."""
    r=list(returns[-1200:]); n=len(r)
    if n<80: return {"available":False,"reason":"At least 80 returns are required."}
    mu=sum(r)/n; x=[v-mu for v in r]; var=sum(v*v for v in x)/n
    if var<=1e-12: return {"available":False,"reason":"Return variance is too small."}
    best=None
    for alpha in (0.03,0.06,0.09,0.12,0.15,0.18):
      for beta in (0.70,0.76,0.82,0.88,0.92,0.95):
        if alpha+beta>=0.985: continue
        omega=var*(1-alpha-beta); h=var; loss=0.0
        for e in x:
            h=max(h,1e-12)
            loss+=math.log(h)+e*e/h
            h=max(omega+alpha*e*e+beta*h,1e-12)
        if best is None or loss<best[0]: best=(loss,alpha,beta,omega,h)
    if best is None: return {"available":False}
    _,a,b,w,h=best
    return {"available":True,"alpha":a,"beta":b,"omega":w,
            "next_variance":h,
            "next_volatility_pct":100*math.sqrt(max(h,0)),
            "persistence":a+b,"fit_rows":n,"method":"bounded Gaussian GARCH(1,1) grid likelihood"}

def _q_two_regime(returns, iterations=24):
    """Two-state Gaussian volatility regime filter; state labels are low/high variance."""
    x=[abs(v) for v in returns[-1500:]]
    if len(x)<100: return {"available":False,"reason":"At least 100 returns are required."}
    s=sorted(x); m=len(s); lo=s[m//3]; hi=s[(2*m)//3]
    v0=max(lo*lo,1e-8); v1=max(hi*hi,v0*2)
    t00,t01,t10,t11=.96,.04,.08,.92
    gamma=[.5,.5]
    for _ in range(iterations):
        f=[]
        for v in x:
            # likelihoods of absolute returns under zero-mean Gaussian scale proxies
            l0=math.exp(max(-60,-.5*(math.log(v0)+v*v/v0))) / math.sqrt(v0)
            l1=math.exp(max(-60,-.5*(math.log(v1)+v*v/v1))) / math.sqrt(v1)
            f.append((max(l0,1e-30),max(l1,1e-30)))
        pred=[]; scales=[]
        p=[.5,.5]
        for a,b in f:
            p=[p[0]*t00+p[1]*t10,p[0]*t01+p[1]*t11]
            q=[p[0]*a,p[1]*b]; d=max(sum(q),1e-300); pred.append([q[0]/d,q[1]/d]); scales.append(d); p=pred[-1]
        # Smoothed transition counts using a one-step approximation.
        c00=c01=c10=c11=1.0
        for i in range(1,len(pred)):
            prev=pred[i-1]; cur=pred[i]
            c00+=prev[0]*cur[0]; c01+=prev[0]*cur[1]; c10+=prev[1]*cur[0]; c11+=prev[1]*cur[1]
        t00=c00/(c00+c01); t01=1-t00; t10=c10/(c10+c11); t11=1-t10
        n0=n1=0.0; a0=a1=0.0
        for v,pair in zip(x,pred):
            n0+=pair[0]; n1+=pair[1]; a0+=pair[0]*v*v; a1+=pair[1]*v*v
        v0=max(a0/max(n0,1e-9),1e-8); v1=max(a1/max(n1,1e-9),v0*1.05)
        gamma=pred[-1]
    high=gamma[1]
    return {"available":True,"high_volatility_probability":high,
            "low_state_volatility_pct":100*math.sqrt(v0),"high_state_volatility_pct":100*math.sqrt(v1),
            "transition_low_to_high":t01,"transition_high_to_low":t10,
            "fit_rows":len(x),"method":"two-state Gaussian volatility regime filter"}

def quantitative_intelligence_snapshot(closes):
    """Independent diagnostics. These are not BUY/SELL votes and never reverse direction."""
    r=_q_log_returns(closes)
    if len(r)<80: return {"version":1,"available":False,"reason":"Insufficient history for quantitative diagnostics."}
    recent=r[-20:]; mean=sum(recent)/len(recent); var=sum((v-mean)**2 for v in recent)/max(1,len(recent)-1)
    # Conjugate normal posterior with a conservative zero-drift prior.
    prior_sd=0.01; obs_var=max(var,1e-10); post_var=1/(1/(prior_sd*prior_sd)+len(recent)/obs_var)
    post_mean=post_var*(len(recent)*mean/obs_var)
    return {"version":2,"available":True,"as_of_returns":len(r),
            "bayesian_drift":{"posterior_mean_pct":100*post_mean,"probability_positive":_q_norm_cdf(post_mean/math.sqrt(post_var)),"posterior_sd_pct":100*math.sqrt(post_var),"prior":"zero-centred Normal(0, 1% daily)"},
            "kalman":_q_kalman_trend(closes),"garch":_q_garch11(r),"regime":_q_two_regime(r),"hmm":_q_hmm_gaussian(r),
            "derivative_features":{"first_difference_daily_log_return":r[-1],"second_difference_change_in_daily_log_return":(r[-1]-r[-2]) if len(r)>1 else None,"status":"causal finite differences; latest values use only observed closes"},
            "optimization":{"gradient":"reverse-mode automatic differentiation","curvature":"exact logistic Hessian","solver":"damped Newton with line search"},
            "guardrails":["diagnostics are separate from directional forecasts","no future observations used","GARCH/regime estimates are model-dependent","not an investment recommendation"]}


def train_brain(dates,closes,hidden=8,feature_names=None):
    feature_names=feature_names or BRAIN_FEATURES
    visible_status("preparing","Neural risk brain: building %d features (%s) and 21-day risk labels."%(len(feature_names),"price-only" if feature_names==BRAIN_FEATURES else "price + macro"),0.02)
    full,ys,idx,fit_ids,val_ids,hold_ids=brain_split(dates,closes)
    feats=[select_features(r,feature_names) if r is not None else None for r in full]
    mean,scale=brain_stats([feats[i] for i in fit_ids])
    fit=brain_scaled(feats,ys,fit_ids,mean,scale); val=brain_scaled(feats,ys,val_ids,mean,scale)
    members=[]; epochs=[]
    for k,seed in enumerate(BRAIN_SEEDS):
        mem,ep=brain_fit_member(fit,val,seed,hidden,60,"Risk brain test fit %d/3 (%d hidden)"%(k+1,hidden),0.05+0.2*k/3,0.2/3); members.append(mem); epochs.append(ep)
    ev=brain_ensemble(members)
    p_val=brain_predict_ids(ev,mean,scale,feats,val_ids); p_hold=brain_predict_ids(ev,mean,scale,feats,hold_ids)
    best_thr=None; best_score=-1e9
    for step in range(6,17):          # alarm levels 15% .. 40%
        thr=step*0.025; n=len(closes); pos=[None]*n
        for i in val_ids: pos[i]=0.0 if p_val[i]>=thr else 1.0
        s=evaluate_positions(closes,pos,val_ids[0],val_ids[-1]+1)
        if not s: continue
        score=s["sharpe"]+0.5*s["max_drawdown"]
        if score>best_score: best_score,best_thr=score,thr
    if best_thr is None: best_thr=0.25
    holdout=brain_evaluate_block(closes,hold_ids,p_hold,best_thr)
    # serving model: refit on every labelled day, using the epoch counts found above
    s_mean,s_scale=brain_stats([feats[i] for i in idx]); allrows=brain_scaled(feats,ys,idx,s_mean,s_scale); serving=[]
    for k,seed in enumerate(BRAIN_SEEDS):
        mem,_=brain_fit_member(allrows,None,seed,hidden,max(8,epochs[k]),"Risk brain final fit %d/3"%(k+1),0.28+0.1*k/3,0.1/3); serving.append(mem)
    quant_layer=quantitative_intelligence_snapshot(closes)
    return {"version":3,"data_basis":"official_daily_closes_v1","created":datetime.now().isoformat(timespec="seconds"),"trained_through":dates[-1],"rows_at_training":len(closes),"checked_rows":len(closes),"hidden":hidden,"features":feature_names,"horizon_days":BRAIN_HORIZON,"drop":BRAIN_DROP,"threshold":best_thr,"mean":s_mean,"scale":s_scale,"members":serving,"validation_epochs":epochs,"eval":{"mean":mean,"scale":scale,"members":members,"holdout_start":dates[hold_ids[0]],"holdout_end":dates[hold_ids[-1]]},"holdout":holdout,"quantitative_intelligence":quant_layer,"history":[]}

def brain_risk_now(brain,dates,closes):
    x=select_features(brain_feature_rows(dates,closes)[-1],brain.get("features",BRAIN_FEATURES))
    return predict(brain_ensemble(brain["members"]),[(v-brain["mean"][j])/brain["scale"][j] for j,v in enumerate(x)])

def brain_save(brain):
    path=brain_path(); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(brain),encoding="utf-8")

def brain_job(force=False):
    """Train the first brain, or retrain on newer data and promote the new version only if it holds up on unseen days."""
    try:
        dates,closes=_load_csv_series(); champ=brain_load()
        if champ is None:
            BRAIN_JOB["message"]="Training the first neural risk brain..."
            brain=train_brain(dates,closes,8); brain["history"]=[{"date":dates[-1],"event":"first brain trained","holdout_auc":brain["holdout"]["auc"]}]
            brain_save(brain); BRAIN_JOB["message"]="First brain trained."
        elif not force and len(closes)-champ.get("checked_rows",champ.get("rows_at_training",len(closes)))<BRAIN_GAP:
            BRAIN_JOB["message"]="Brain is up to date."
        else:
            BRAIN_JOB["message"]="Self-improvement: training challengers on newer data..."
            order=list(BRAIN_HIDDEN_CHOICES); alt=order[(order.index(champ["hidden"])+1)%len(order)] if champ["hidden"] in order else 12
            full,ys,idx,_,_,hold_ids=brain_split(dates,closes)
            feats=[select_features(r,champ.get("features",BRAIN_FEATURES)) if r is not None else None for r in full]
            ce=champ["eval"]; p_champ=brain_predict_ids(brain_ensemble(ce["members"]),ce["mean"],ce["scale"],feats,hold_ids)
            yh=[ys[i] for i in hold_ids]; champ_auc=auc_score([p_champ[i] for i in hold_ids],yh)
            champ_features=champ.get("features",BRAIN_FEATURES)
            try: refresh_macro()
            except Exception: pass
            candidates=[(champ["hidden"],"refresh on newer data",-0.015,champ_features),(alt,"alternative size (%d hidden units)"%alt,0.01,champ_features)]
            if champ_features!=ALL_FEATURES and MACRO_CACHE.get("series"):
                candidates.append((champ["hidden"],"with macro features (10-year yield change, credit spread, recession flag)",0.01,ALL_FEATURES))
            results=[]
            for hidden,kind,tol,feature_names in candidates:
                cand=train_brain(dates,closes,hidden,feature_names); results.append((cand,kind,tol,cand["holdout"]["auc"]))
            note=[]; winner=None; win_auc=-1
            for cand,kind,tol,auc in results:
                ok=(auc is not None and champ_auc is not None and auc>=champ_auc+tol)
                note.append({"candidate":kind,"auc":auc,"champion_auc_same_days":champ_auc,"promoted_if":">= champion %+.3f"%tol,"qualified":bool(ok)})
                if ok and auc>win_auc: winner,win_auc=cand,auc
            event={"date":dates[-1],"event":"self-improvement check","champion_auc_same_days":champ_auc,"candidates":note,"promoted":winner is not None}
            hist=(champ.get("history") or [])+[event]
            if winner is not None:
                winner["history"]=hist[-20:]; brain_save(winner); BRAIN_JOB["message"]="New brain promoted (holdout AUC %.3f vs %.3f)."%(win_auc,champ_auc or 0)
            else:
                champ["history"]=hist[-20:]; champ["checked_rows"]=len(closes); brain_save(champ); BRAIN_JOB["message"]="Kept the current brain; challengers did not qualify."
        visible_status("complete",BRAIN_JOB["message"],1)
    except Exception as exc:
        BRAIN_JOB["error"]=str(exc); BRAIN_JOB["message"]="Brain job failed: "+str(exc); visible_status("error",str(exc),0.0)
    finally:
        BRAIN_JOB["running"]=False

def start_brain_job(force=False):
    with BRAIN_LOCK:
        if BRAIN_JOB["running"]: return {"started":False,"message":"A brain training job is already running."}
        BRAIN_JOB.update({"running":True,"error":None,"message":"Starting...","started":datetime.now().isoformat(timespec="seconds")})
    threading.Thread(target=brain_job,args=(force,),daemon=True).start()
    return {"started":True,"message":"Brain job started; watch the live network panel for progress."}

def brain_status():
    b=brain_load()
    return {"trained":b is not None,"data_basis":(b or {}).get("data_basis"),"trained_through":(b or {}).get("trained_through"),"alarm_level":(b or {}).get("threshold"),"holdout":(b or {}).get("holdout"),"history":(b or {}).get("history",[])[-5:],"quantitative_intelligence":(b or {}).get("quantitative_intelligence"),"job":dict(BRAIN_JOB)}

def monte_carlo_drop_risk(closes,horizon=None,drop=None,n_sims=50000,seed=12345,block_length=5):
    """Moving-block bootstrap of historical log returns.

    Resampling contiguous blocks preserves some short-run serial dependence and
    volatility clustering. The reported interval is Monte Carlo sampling error
    only; it is not a confidence interval for the true future market probability.
    """
    horizon=horizon or BRAIN_HORIZON; drop=drop or BRAIN_DROP
    if len(closes)<horizon+100 or n_sims<1000: return None
    log_returns=[]
    for i in range(1,len(closes)):
        if closes[i-1]<=0 or closes[i]<=0: continue
        r=math.log(closes[i]/closes[i-1])
        if math.isfinite(r): log_returns.append(r)
    block=max(1,min(int(block_length),horizon))
    starts=list(range(0,len(log_returns)-block+1))
    if len(starts)<50: return None
    rng=random.Random(seed); hits=0; final_returns=[]; max_losses=[]
    for _ in range(int(n_sims)):
        path=1.0; worst=1.0; used=0
        while used<horizon:
            st=rng.choice(starts)
            for lr in log_returns[st:st+min(block,horizon-used)]:
                path*=math.exp(lr); worst=min(worst,path); used+=1
                if used>=horizon: break
        final_returns.append(path-1.0)
        max_losses.append(worst-1.0)
        if worst<=1.0-drop: hits+=1
    p=hits/n_sims; se=math.sqrt(p*(1-p)/n_sims)
    final_sorted=sorted(final_returns); tail_n=max(1,int(0.05*n_sims))
    return {"probability":p,"simulations":n_sims,"horizon_days":horizon,
            "drop_threshold":drop,"confidence_95_low":max(0.0,p-1.96*se),
            "confidence_95_high":min(1.0,p+1.96*se),
            "final_return_q05":final_sorted[int(.05*(n_sims-1))],
            "final_return_median":final_sorted[n_sims//2],
            "final_return_q95":final_sorted[int(.95*(n_sims-1))],
            "expected_shortfall_5":sum(final_sorted[:tail_n])/tail_n,
            "block_length":block,"historical_returns":len(log_returns),
            "method":"moving-block bootstrap of historical log returns; contiguous %d-session blocks; dependence-preserving resampling"%block,
            "interval_semantics":"95% Monte Carlo simulation-error interval only; excludes parameter, regime and model uncertainty"}


def reconcile_signals(risk_state, trend_signal, horizon_days, data_fresh=True, model_fresh=True, fusion=None):
    """Use validated directional evidence; downside-risk scores remain a separate risk overlay.

    Direction is supplied by the calibrated MLP + trend fusion. The risk model
    estimates drawdown probability and is never treated as an opposite-direction
    vote or an automatic BUY veto.
    """
    if not data_fresh:
        return {"verdict":"HOLD","status":"STALE_MARKET_DATA",
                "reason":"Official market data is stale; MIRA will not publish a current directional decision."}
    if horizon_days != 1:
        return {"verdict":"HOLD","status":"HORIZON_UNSUPPORTED",
                "reason":"The fused directional model is trained for the next session only. MIRA will not relabel a one-session probability as a 21-session forecast."}
    if not fusion or not fusion.get("available"):
        return {"verdict":"HOLD","status":"DIRECTIONAL_FUSION_UNAVAILABLE",
                "reason":"The directional ensemble has not completed its calibrated validation and holdout evaluation."}
    if not fusion.get("edge_validated"):
        return {"verdict":"HOLD","status":"NO_VALIDATED_DIRECTIONAL_EDGE",
                "reason":fusion.get("reason","The holdout has not demonstrated a reliable directional edge over baseline.")}
    p=fusion.get("next_session_up_probability")
    try: p=float(p)
    except (TypeError,ValueError): p=float("nan")
    if not math.isfinite(p) or not 0.0<=p<=1.0:
        return {"verdict":"HOLD","status":"INVALID_FUSED_PROBABILITY",
                "reason":"The fused directional probability failed validation."}
    if p>=0.55:
        verdict="BUY"
    elif p<=0.45:
        verdict="SELL"
    else:
        return {"verdict":"HOLD","status":"PROBABILITY_TOO_CLOSE_TO_NEUTRAL",
                "reason":"The holdout shows directional skill, but the current fused probability is inside the neutral band."}
    risk_note=("Downside-risk alert is elevated; this increases risk awareness but does not reverse the directional forecast."
               if risk_state=="ELEVATED" else
               "Downside-risk alert is not elevated; this is a separate risk estimate, not confirmation of direction.")
    return {"verdict":verdict,"status":"FUSED_DIRECTIONAL_EDGE",
            "reason":fusion.get("reason","Validated directional fusion.")+" "+risk_note}


def directional_ledger_path():
    return LEARN_DIR/"directional_ledger.json"

def update_directional_ledger(dates,closes,fusion):
    """Grade yesterday's next-session forecast only after the next official close exists."""
    if fusion and (not fusion.get("available") or fusion.get("as_of")!=dates[-1]):
        fusion=None
    path=directional_ledger_path()
    try: ledger=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): ledger=[]
    if not isinstance(ledger,list): ledger=[]
    idx={d:i for i,d in enumerate(dates)}
    graded_now=0
    for entry in ledger:
        i=idx.get(entry.get("as_of"))
        if i is not None and i+1<len(closes) and "actual_up" not in entry:
            entry["actual_up"]=bool(closes[i+1]>closes[i])
            p=_clip_probability(entry.get("p_fused",0.5))
            y=int(entry["actual_up"])
            entry["brier"]=(p-y)**2
            entry["logloss"]=-(y*math.log(p)+(1-y)*math.log(1-p))
            if entry.get("signal") in ("BUY","SELL"):
                entry["direction_correct"]=bool((entry["signal"]=="BUY")==entry["actual_up"])
            graded_now+=1
    if fusion and not any(e.get("as_of")==fusion.get("as_of") for e in ledger):
        ledger.append({"as_of":fusion["as_of"],"p_fused":fusion.get("next_session_up_probability",0.5),
                       "p_model":fusion.get("direction_model_probability",0.5),
                       "p_logistic":fusion.get("logistic_newton_probability"),
                       "p_trend":fusion.get("trend_conditional_probability",0.5),
                       "signal":fusion.get("signal","HOLD"),
                       "weights":fusion.get("weights",{}),"edge_validated":fusion.get("edge_validated",False)})
    try:
        LEARN_DIR.mkdir(parents=True,exist_ok=True)
        tmp=path.with_suffix(".json.tmp");tmp.write_text(json.dumps(ledger[-5000:],indent=1),encoding="utf-8");os.replace(tmp,path)
    except OSError:
        return {"logged":False,"graded":graded_now}
    matured=[e for e in ledger if "actual_up" in e]
    return {"logged":True,"graded":graded_now,"matured":len(matured),
            "brier":(sum(e["brier"] for e in matured[-126:])/len(matured[-126:])) if matured else None}

def apply_live_directional_weight_adjustment(fusion):
    """Shrinkage-controlled online skill update for neural, logistic and trend experts."""
    try: ledger=json.loads(directional_ledger_path().read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return fusion
    matured=[e for e in ledger if "actual_up" in e and e.get("p_model") is not None and e.get("p_trend") is not None][-126:]
    if len(matured)<40:
        fusion["online_weight_update"]={"applied":False,"matured_observations":len(matured),
                                        "reason":"At least 40 matured next-session outcomes are required."}
        return fusion
    ys=[int(e["actual_up"]) for e in matured]
    base=(sum(ys)+1.0)/(len(ys)+2.0)
    lb=_logloss([base]*len(ys),ys) or 0.693
    expert_fields={"neural":"p_model","logistic_newton":"p_logistic","trend":"p_trend"}
    live_skill={}; live_losses={}; counts={}
    for name,field in expert_fields.items():
        rows=[e for e in matured if e.get(field) is not None]
        counts[name]=len(rows)
        if len(rows)<40: continue
        labels=[int(e["actual_up"]) for e in rows]
        b=(sum(labels)+1.0)/(len(labels)+2.0)
        loss=_logloss([e[field] for e in rows],labels)
        live_losses[name]=loss
        baseline_loss=_logloss([b]*len(labels),labels) or 0.693
        live_skill[name]=max(0.0,baseline_loss-loss-0.002)
    total=sum(live_skill.values())
    if total<=0:
        fusion["online_weight_update"]={"applied":False,"matured_observations":len(matured),
                                        "expert_observations":counts,
                                        "reason":"Recent live outcomes show no positive expert skill over the baseline."}
        return fusion
    live_w={k:v/total for k,v in live_skill.items()}
    prior=fusion.get("weights") or {"neural":0.0,"logistic_newton":0.0,"trend":0.0}
    available=set(live_w)
    # Remove weights for experts that do not have enough matured observations.
    prior_w={k:max(0.0,float(prior.get(k,0.0))) for k in available}
    prior_total=sum(prior_w.values())
    if prior_total>0: prior_w={k:v/prior_total for k,v in prior_w.items()}
    updated={k:0.75*prior_w.get(k,0.0)+0.25*live_w.get(k,0.0) for k in available}
    norm=sum(updated.values())
    if norm>0: updated={k:v/norm for k,v in updated.items()}
    fusion["weights"]=updated
    probs={"neural":fusion.get("direction_model_probability",0.5),
           "logistic_newton":fusion.get("logistic_newton_probability"),
           "trend":fusion.get("trend_conditional_probability",0.5)}
    usable={k:v for k,v in updated.items() if probs.get(k) is not None}
    den=sum(usable.values())
    p=sum(usable[k]*float(probs[k]) for k in usable)/den if den>0 else base
    fusion["next_session_up_probability"]=_clip_probability(p)
    fusion["expert_probabilities"]={k:probs[k] for k in probs if probs[k] is not None}
    if fusion.get("edge_validated"):
        fusion["signal"]="BUY" if p>=0.55 else "SELL" if p<=0.45 else "HOLD / PROBABILITY TOO CLOSE"
    fusion["online_weight_update"]={"applied":True,"matured_observations":len(matured),
                                    "expert_observations":counts,"live_weights":live_w,
                                    "validation_live_blend":updated,"live_logloss":{"baseline":lb,**live_losses},
                                    "shrinkage":"75% validation / 25% matured live outcomes"}
    return fusion


def load_saved_directional_fusion(as_of):
    """Infer today's next-session probability from the last validated saved model."""
    try:
        saved=json.loads((DATA_DIR/"models"/"nasdaq100_model.json").read_text(encoding="utf-8"))
        fusion=dict(saved.get("fusion") or {})
        if not fusion.get("available") or len(saved.get("features") or [])!=11:
            return None
        dates,closes=_load_csv_series()
        if dates[-1]!=as_of or len(closes)<252:
            return None
        index={d:i for i,d in enumerate(dates)}
        trained_index=index.get(saved.get("as_of"))
        if trained_index is None:
            return None
        rows_since=len(dates)-1-trained_index
        if rows_since>21:
            return None
        # Match the training-time volume feature where a usable volume column exists.
        volume=[0.0]*len(dates)
        try:
            with _csv_path().open(encoding="utf-8-sig") as f:
                reader=csv.DictReader(f)
                headers=[h.strip() for h in (reader.fieldnames or [])]
                vcol=next((h for h in headers if h.lower() in ("volume","vol")),None)
                dcol=next((h for h in headers if h.lower() in ("date","datetime","timestamp","time","observation_date")),None)
                if vcol and dcol:
                    by_date={}
                    for row in reader:
                        try: by_date[parse_date(row[dcol]).date().isoformat()]=float(str(row[vcol]).replace(",","").strip() or 0)
                        except (ValueError,TypeError,KeyError): pass
                    volume=[by_date.get(d,0.0) for d in dates]
        except OSError:
            pass
        daily=[0.0]+[closes[k]/closes[k-1]-1 for k in range(1,len(closes))]
        def std(vals):
            avg=sum(vals)/len(vals)
            return math.sqrt(sum((z-avg)**2 for z in vals)/len(vals))
        ema12=[closes[0]];ema26=[closes[0]]
        a12=2.0/13.0;a26=2.0/27.0
        for value in closes[1:]:
            ema12.append(a12*value+(1-a12)*ema12[-1])
            ema26.append(a26*value+(1-a26)*ema26[-1])
        macd=[(ema12[k]-ema26[k])/closes[k] for k in range(len(closes))]
        signal_line=[macd[0]];a9=2.0/10.0
        for value in macd[1:]:
            signal_line.append(a9*value+(1-a9)*signal_line[-1])
        k=len(closes)-1
        volchange=0.0
        if k>0 and volume[k]>0 and volume[k-1]>0:
            volchange=max(-3,min(3,math.log(volume[k]/volume[k-1])))
        ema_slope=(ema12[k]/ema12[k-3]-1) if k>=3 else 0.0
        prior_slope=(ema12[k-3]/ema12[k-6]-1) if k>=6 else 0.0
        x=[daily[k],closes[k]/closes[k-2]-1,closes[k]/closes[k-5]-1,
           closes[k]/closes[k-10]-1,closes[k]/closes[k-20]-1,
           std(daily[k-4:k+1]),std(daily[k-19:k+1]),volchange,
           ema_slope,ema_slope-prior_slope,macd[k]-signal_line[k]]
        mean=saved.get("mean") or [];scale=saved.get("scale") or []
        if len(x)!=len(mean) or len(x)!=len(scale): return None
        z=[(x[q]-mean[q])/(scale[q] or 1.0) for q in range(len(x))]
        p_raw=predict(saved["direction_model"],z)
        p_model=_apply_platt(p_raw,fusion.get("calibration") or {})
        logistic_saved=fusion.get("logistic_model") or {}
        p_logistic=_q_logistic_predict(logistic_saved,z) if logistic_saved.get("available") else None
        trend_n=int(fusion.get("trend_lookback",200))
        trend_state=trend_positions(closes,trend_n,0.01)[-1]
        base=float(fusion.get("calibration_base_rate",0.5))
        trend_map=fusion.get("trend_probability_map") or {}
        p_trend=trend_map.get(str(int(trend_state)),base) if trend_state is not None else base
        fusion["direction_model_probability"]=p_model
        fusion["raw_direction_model_probability"]=p_raw
        fusion["logistic_newton_probability"]=p_logistic
        fusion["trend_conditional_probability"]=p_trend
        fusion["expert_probabilities"]={"neural":p_model,"logistic_newton":p_logistic,"trend":p_trend} if p_logistic is not None else {"neural":p_model,"trend":p_trend}
        fusion["trend_state"]="BUY" if trend_state==1 else "SELL" if trend_state==0 else "UNAVAILABLE"
        fusion["as_of"]=as_of
        fusion["trained_through"]=saved.get("as_of")
        fusion["rows_since_training"]=rows_since
        fusion=apply_live_directional_weight_adjustment(fusion)
        weights=fusion.get("weights") or {}
        expert_probs={"neural":p_model,"logistic_newton":p_logistic,"trend":p_trend}
        active={k:float(w) for k,w in weights.items() if expert_probs.get(k) is not None and float(w)>0}
        weight_total=sum(active.values())
        p_fused=(sum(active[k]*float(expert_probs[k]) for k in active)/weight_total) if weight_total>0 else base
        fusion["expert_probabilities"]={k:v for k,v in expert_probs.items() if v is not None}
        fusion["next_session_up_probability"]=_clip_probability(p_fused)
        if fusion.get("edge_validated"):
            if p_fused>=0.55:
                fusion["signal"]="BUY";fusion["reason"]="Validated holdout edge; current calibrated fusion P(up) is at least 55%."
            elif p_fused<=0.45:
                fusion["signal"]="SELL";fusion["reason"]="Validated holdout edge; current calibrated fusion P(up) is at most 45%."
            else:
                fusion["signal"]="HOLD / PROBABILITY TOO CLOSE"
                fusion["reason"]="Validated holdout edge exists, but current P(up) is inside the neutral band."
        else:
            fusion["signal"]="HOLD / NO VALIDATED DIRECTIONAL EDGE"
            fusion["reason"]="The last untouched holdout did not establish a positive directional edge; current probability is shown for research, not as a directional call."
        return fusion
    except (OSError,json.JSONDecodeError,KeyError,TypeError,ValueError,IndexError,ZeroDivisionError):
        return None


def final_verdict(prompt=""):
    h,label,defaulted=parse_horizon(prompt)
    try:
        refresh_prices(False)
    except Exception:
        pass

    # Predictive models use official daily closes only. Intraday/estimated QQQ
    # quotes remain a separate display feed and are never mixed into daily features.
    dates,closes=_load_csv_series()
    data_age=official_data_age_days(dates)
    data_fresh=data_age <= 4
    brain=brain_load()
    rows_since_check=(len(closes)-brain.get("checked_rows",brain.get("rows_at_training",len(closes)))) if brain else None
    model_fresh=brain is not None and brain.get("data_basis")=="official_daily_closes_v1" and rows_since_check < BRAIN_GAP

    if brain is None:
        started=start_brain_job(False)
        fusion=load_saved_directional_fusion(dates[-1])
        decision=reconcile_signals("UNKNOWN",None,h,data_fresh=data_fresh,model_fresh=False,fusion=fusion)
        return {"as_of":dates[-1],"model_data_as_of":dates[-1],"model_data_source":"official daily closes only",
                "data_age_calendar_days":data_age,"data_fresh":data_fresh,"data_note":price_data_note(),
                "hourly_note":intraday_note(h),"horizon_days":h,"horizon_label":label,
                "horizon_defaulted":defaulted,"horizon_all":None,"trend_verdict":None,
                "trend_rule":None,"distance_from_average_pct":None,"distance_from_50day_pct":None,
                "self_check":"Neural downside-risk model is not trained.","brain":None,"monte_carlo":None,
                "verdict":decision["verdict"],"confidence":"Low","interim":False,
                "decision_status":decision["status"],"decision_reason":decision["reason"],
                "fusion":fusion,"brain_job":started["message"],"agree":None,"risk_state":"UNKNOWN",
                "risk_model_fresh":False}

    if not model_fresh and not BRAIN_JOB["running"]:
        start_brain_job(False)

    rule,learn_note,tested=self_improve(dates,closes)
    n,band=rule
    pos=trend_positions(closes,n,band)
    trend_verdict="BUY" if pos[-1]==1 else "SELL"
    dist=closes[-1]/(sum(closes[-n:])/n)-1
    d50=closes[-1]/(sum(closes[-50:])/50)-1
    start=eval_start(dates)
    everyday=horizon_stats(closes,[1.0]*len(closes),start,h,None)

    # If inputs/model are stale, don't compute or publish a seemingly current call.
    if not data_fresh:
        fusion=load_saved_directional_fusion(dates[-1])
        decision=reconcile_signals(None,trend_verdict,h,data_fresh=False,model_fresh=model_fresh,fusion=fusion)
        out={"as_of":dates[-1],"model_data_as_of":dates[-1],"model_data_source":"official daily closes only",
             "data_age_calendar_days":data_age,"data_fresh":data_fresh,"data_note":price_data_note(),
             "hourly_note":intraday_note(h),"horizon_days":h,"horizon_label":label,
             "horizon_defaulted":defaulted,"horizon_all":everyday,"trend_verdict":trend_verdict,
             "trend_rule":{"average_days":n,"band_pct":band*100},
             "distance_from_average_pct":round(dist*100,2),"distance_from_50day_pct":round(d50*100,2),
             "self_check":learn_note,"brain":None,"monte_carlo":None,"verdict":"HOLD",
             "confidence":"Low","interim":False,"agree":False,"decision_status":decision["status"],
             "decision_reason":decision["reason"],"risk_state":"UNKNOWN",
             "brain_job":dict(BRAIN_JOB),"live_record":chart_live_record()}
        return out

    try:
        p=float(brain_risk_now(brain,dates,closes))
        thr=float(brain["threshold"])
        if not (math.isfinite(p) and math.isfinite(thr) and 0.0 <= p <= 1.0 and 0.0 < thr < 1.0):
            raise ValueError("Risk model returned an invalid probability or threshold.")
    except Exception as exc:
        fusion=load_saved_directional_fusion(dates[-1])
        decision=reconcile_signals("UNKNOWN",trend_verdict,h,data_fresh=True,model_fresh=False,fusion=fusion)
        return {"as_of":dates[-1],"model_data_as_of":dates[-1],"model_data_source":"official daily closes only",
                "data_age_calendar_days":data_age,"data_fresh":True,"data_note":price_data_note(),
                "hourly_note":intraday_note(h),"horizon_days":h,"horizon_label":label,
                "horizon_defaulted":defaulted,"horizon_all":everyday,"trend_verdict":trend_verdict,
                "verdict":decision["verdict"],"confidence":"Low","interim":False,"agree":None,
                "decision_status":decision["status"],"decision_reason":decision["reason"],
                "risk_state":"UNKNOWN","fusion":fusion,"risk_model_error":str(exc)[:120],
                "live_record":chart_live_record(),"trend_rule":{"average_days":n,"band_pct":band*100},
                "distance_from_average_pct":round(dist*100,2),"distance_from_50day_pct":round(d50*100,2)}

    risk_state="ELEVATED" if p >= thr else "NORMAL"
    # Legacy field retained for existing dashboard consumers. This is a risk
    # classification only: SELL means elevated risk, BUY means normal risk.
    brain_verdict="SELL" if risk_state=="ELEVATED" else "BUY"
    fusion=load_saved_directional_fusion(dates[-1])
    decision=reconcile_signals(risk_state,trend_verdict,h,data_fresh=True,model_fresh=model_fresh,fusion=fusion)
    # Directional confidence is tied to the fused holdout evidence, not the risk score.
    confidence="Low"
    live=update_ledger(dates,closes,brain_verdict,confidence,p,thr)
    out={"as_of":dates[-1],"model_data_as_of":dates[-1],"model_data_source":"official daily closes only",
         "data_age_calendar_days":data_age,"data_fresh":True,"data_note":price_data_note(),
         "hourly_note":intraday_note(h),"horizon_days":h,"horizon_label":label,
         "horizon_defaulted":defaulted,"horizon_all":everyday,"trend_verdict":trend_verdict,
         "trend_rule":{"average_days":n,"band_pct":band*100},
         "distance_from_average_pct":round(dist*100,2),"distance_from_50day_pct":round(d50*100,2),
         "self_check":learn_note,"brain_verdict":brain_verdict,"risk_state":risk_state,
         "risk_score":p,"alarm_level":thr,"risk_horizon_days":BRAIN_HORIZON,
         "verdict":decision["verdict"],"confidence":confidence,"interim":False,
         "agree":decision["status"]!="RISK_VETO","risk_trend_alignment":("ELEVATED_RISK_WITH_POSITIVE_TREND" if risk_state=="ELEVATED" and trend_verdict=="BUY" else "ELEVATED_RISK_WITH_DEFENSIVE_TREND" if risk_state=="ELEVATED" and trend_verdict=="SELL" else "NO_DOWNSIDE_ALARM"),
         "decision_status":decision["status"],"decision_reason":decision["reason"],
         "brain":{"trained_through":brain.get("trained_through"),"hidden":brain.get("hidden"),
                  "holdout":brain.get("holdout"),"holdout_start":brain.get("eval",{}).get("holdout_start"),
                  "holdout_end":brain.get("eval",{}).get("holdout_end"),"history":brain.get("history",[])[-3:]},
         "live_record":live,"job":dict(BRAIN_JOB),
         "monte_carlo":monte_carlo_drop_risk(closes,BRAIN_HORIZON,BRAIN_DROP)}
    return out

def build_final_answer(v):
    pct=lambda x:"%.1f%%"%(x*100)
    p0=lambda x:"n/a" if x is None else "%.0f%%"%(x*100)
    h=v.get("horizon_days",21); label=v.get("horizon_label","the next month")
    parts=["Decision horizon: %s."%label]
    if v.get("horizon_defaulted"):
        parts.append("No horizon was specified, so MIRA used its default 21-trading-day window.")
    if v.get("interim") or (v.get("risk_state")=="UNKNOWN" and v.get("verdict")=="HOLD"):
        parts.append("Decision: HOLD. "+v.get("decision_reason","A required model is unavailable or invalid."))
        if v.get("trend_verdict"):
            parts.append("Trend context only: %s. This is not a final signal."%v["trend_verdict"])
        if v.get("brain_job"):
            parts.append("Training status: %s."%v["brain_job"])
        return " ".join(parts)+"\n\nSUMMARY | HOLD | Status: %s | Official model data: %s"%(v.get("decision_status","UNAVAILABLE"),v.get("model_data_as_of",v.get("as_of","unknown")))

    verdict=v.get("verdict","HOLD")
    parts.append("MIRA decision: %s."%verdict)
    parts.append("Reason: "+v.get("decision_reason","No additional reason was supplied."))
    parts.append("Trend engine context: %s."%v.get("trend_verdict","unavailable"))
    fusion=v.get("fusion") or {}
    if fusion:
        parts.append("Unified directional ensemble: calibrated next-session up probability %s; directional-model contribution %s; trend contribution %s; validation weights %s. Held-out edge versus majority baseline: %s percentage points (95%% block-bootstrap interval %s)."%(
            p0(fusion.get("next_session_up_probability")),
            p0(fusion.get("direction_model_probability")),
            p0(fusion.get("trend_conditional_probability")),
            fusion.get("weights","unavailable"),
            ("%.2f"%fusion.get("holdout_edge_pp")) if fusion.get("holdout_edge_pp") is not None else "n/a",
            fusion.get("holdout_edge_ci_pp","unavailable")))
    parts.append("Neural downside-risk state: %s. Estimated probability of an 8%%+ drawdown over %s trading days: %s; alarm threshold: %s."%(v.get("risk_state","UNKNOWN"),v.get("risk_horizon_days",BRAIN_HORIZON),p0(v.get("risk_score")),p0(v.get("alarm_level"))))
    if v.get("distance_from_average_pct") is not None:
        n=v.get("trend_rule",{}).get("average_days","n/a")
        parts.append("Price is %.2f%% %s its %s-day trend average and %.2f%% %s its 50-day average."%(abs(v["distance_from_average_pct"]),"above" if v["distance_from_average_pct"]>=0 else "below",n,abs(v["distance_from_50day_pct"]),"above" if v["distance_from_50day_pct"]>=0 else "below"))
    parts.append("Directional confidence is based on the calibrated fusion holdout, not on the downside-risk score. The risk model remains a separate risk estimator, not a directional forecaster.")
    parts.append("Model inputs: official daily closes through %s. Alpaca live quotes are displayed separately; the current daily-trained directional model does not claim to ingest them millisecond-by-millisecond."%v.get("model_data_as_of",v.get("as_of","unknown")))
    mc=v.get("monte_carlo")
    if mc:
        parts.append("Historical base-rate check (not a prediction): an 8%%+ drawdown occurred in %.1f%% of %d sampled %d-day historical windows (approximate 95%% interval %.1f%%–%.1f%%)."%(100*mc["probability"],mc["simulations"],mc["horizon_days"],100*mc["confidence_95_low"],100*mc["confidence_95_high"]))
    ho=(v.get("brain") or {}).get("holdout") or {}
    if ho.get("auc") is not None:
        parts.append("Neural risk-model holdout AUC: %.2f on %s–%s. This measures risk ranking, not BUY/SELL direction accuracy."%(ho["auc"],(v.get("brain") or {}).get("holdout_start"),(v.get("brain") or {}).get("holdout_end")))
    lr=(v.get("live_record") or {}).get("graded") or {}
    if lr.get("n",0):
        parts.append("Live risk-classification record: %d graded calls; %.1f%% correct. This grades the neural drawdown warning only, not the final directional decision."%(lr["n"],100*lr.get("hit_rate",0)))
    else:
        calls=(v.get("live_record") or {}).get("calls_logged",0)
        parts.append("Live risk-classification record: %d calls logged; not enough matured outcomes to report a graded rate."%calls)
    if v.get("data_note"): parts.append(v["data_note"])
    if v.get("hourly_note"): parts.append(v["hourly_note"])
    parts.append("Research output only; not a personalized investment recommendation.")
    return " ".join(parts)+"\n\nSUMMARY | Decision: %s | Status: %s | Trend: %s | Risk: %s (%s vs %s) | Data: %s"%(verdict,v.get("decision_status"),v.get("trend_verdict"),v.get("risk_state"),p0(v.get("risk_score")),p0(v.get("alarm_level")),v.get("model_data_as_of",v.get("as_of")))

def answer_decision(prompt):
    visible_status("tool_call","MIRA chat: neural risk brain is scoring today's market and grading past calls.")
    v=final_verdict(prompt)
    log_question(prompt,v["verdict"])
    answer=build_final_answer(v)
    visible_status("observation","FINAL %s (%s confidence) as of %s"%(v["verdict"],v["confidence"],v["as_of"]))
    visible_status("complete","Answer ready: FINAL "+v["verdict"],1)
    return {"answer":answer,"summary":v,"actions":[{"tool":"neural_risk_brain","observation":{k:v.get(k) for k in ("verdict","confidence","horizon_label","as_of","risk_score","alarm_level","risk_state","trend_verdict","decision_status","decision_reason","model_data_as_of","data_fresh","interim")}}],"model":"MIRA neural risk brain (self-improving)"}

# ---- Chat answers for every other question: built from MIRA's own saved results, so no Ollama is needed (works on a cloud server) ----
OLLAMA_PROBE={"at":0.0,"ok":False}
def ollama_up():
    if time.time()-OLLAMA_PROBE["at"]<30: return OLLAMA_PROBE["ok"]
    try:
        with urlopen(Request("http://127.0.0.1:11434/api/tags"),timeout=1.5) as r: ok=OLLAMA_MODEL in [m.get("name","") for m in json.loads(r.read()).get("models",[])]
    except Exception: ok=False
    OLLAMA_PROBE.update({"at":time.time(),"ok":ok}); return ok

def general_answer(prompt):
    p=prompt.lower(); parts=[]; tools=[]
    def has(*w): return any(x in p for x in w)
    brain=brain_load(); sb=chart_scoreboard(brain) if brain and brain.get("eval") else {"available":False}
    pc=lambda x: "n/a" if x is None else "%.1f%%"%(100*x)
    if has("ollama","qwen","qwen3","language model","llm","chatgpt"):
        parts.append("Qwen (run through Ollama) is a small language model that only runs on the owner's own PC. Its job was to talk: read MIRA's results and explain them in words. It never makes the market predictions - the neural networks do. On this cloud server Ollama is not installed, so the built-in answers are used instead. To get a real conversational assistant here, the owner adds an ANTHROPIC_API_KEY on the server and this chat will answer any question, using MIRA's own numbers.")
    elif has("accura","correct","how good","right","perform","track record","reliab","trust","score","win rate","success"):
        tools.append("scoreboard")
        if sb.get("available"):
            parts.append("On %s days the model had never seen, its downside-risk classifications (SELL=alarm, BUY=no alarm) were right %s of the time. Always predicting no alarm would have been right %s. Its risk ranking scores %.2f (0.50 is a coin flip). It warned before %s of the real 8%%+ drops, and %s of its alarms were followed by a drop (a drop happens %s of the time anyway)."%(sb["days"],pc(sb["call_accuracy"]),pc(sb["always_buy_accuracy"]),sb["auc"] or 0,pc(sb["drops_caught"]),pc(sb["alarm_precision"]),pc(sb["drop_base_rate"])))
            if sb.get("strategy") and sb.get("buy_and_hold"): parts.append("Following its calls returned %s a year with a worst fall of %s; buy-and-hold returned %s a year with a worst fall of %s."%(pc(sb["strategy"]["cagr"]),pc(sb["strategy"]["max_drawdown"]),pc(sb["buy_and_hold"]["cagr"]),pc(sb["buy_and_hold"]["max_drawdown"])))
        else: parts.append("The networks are still doing their first training, so there are no test results yet.")
        lr=chart_live_record()
        parts.append("Live record since this server began logging: %d calls, %d graded a month later (%d right)."%(lr["calls"],lr["month_graded"],lr["month_right"]))
    elif has("false positive","false negative","holdout","evaluation","precision","recall"):
        m=inspect_market_evaluation(); tools.append("inspect_market_evaluation")
        if m.get("error") and sb.get("available"): parts.append("On %s unseen days the risk brain flagged %s of days as dangerous. %s of those alarms were false alarms (no 8%%+ drop followed), and it missed %s of the real drops (it caught %s)."%(sb["days"],pc(brain["holdout"].get("flagged_share")),pc(1-sb["alarm_precision"]),pc(1-sb["drops_caught"]),pc(sb["drops_caught"])))
        elif m.get("error"): parts.append(m["error"])
        else:
            d=m.get("stress_drawdown",{})
            parts.append("Saved evaluation on %s unseen rows (data to %s). Drop-risk task: it caught %s of real drops (recall), %s of its alerts were right (precision), false-positive rate %s, missed %s."%(m.get("holdout_rows"),m.get("as_of"),pc(d.get("recall")),pc(d.get("precision")),pc(d.get("false_positive_rate")),pc(d.get("false_negative_rate"))))
            parts.append("Next-day direction: accuracy %s against a %s majority baseline, so there is no proven edge for predicting a single day."%(pc(m["direction"].get("holdout_accuracy")),pc(m["direction"].get("majority_baseline_accuracy"))))
    elif has("learn","improv","retrain","train","self","better","update") and not has("hour","intraday","alpaca"):
        tools.append("brain_status"); st=brain_status()
        if st["trained"]:
            parts.append("The neural risk brain was last trained on data through %s. About every 21 new trading days it trains challenger networks on all the newest data and replaces the current one only if it holds up on days it has not seen. Every call is also logged and graded a day and a month later."%st["trained_through"])
            h=st.get("history") or []
            if h: parts.append("Latest check: "+str(h[-1].get("event","training"))+" on "+str(h[-1].get("date"))+(", a retrained brain was promoted." if h[-1].get("promoted") else "."))
        else: parts.append("The brain is training for the first time right now; ask again in a few minutes.")
        if st["job"].get("running"): parts.append("A retraining job is running at the moment.")
        parts.append("Your questions are logged for review but are never used to train the networks, so nobody can steer it by asking.")
    elif has("data","fresh","live","real-time","realtime","current price","qqq","latest","how old","date"):
        tools.append("price_data_note")
        try: refresh_prices(False)
        except Exception: pass
        parts.append(price_data_note())
        try:
            L=fetch_market_snapshot()
            if L.get("connected"): parts.append("QQQ is %s right now (%s, %s)."%("%.2f"%L["last"],L.get("session"),("%+.2f%% on the day"%(100*L["day_change_pct"])) if L.get("day_change_pct") is not None else "change unavailable"))
            else: parts.append("Live quotes are off: "+str(L.get("error","connect Alpaca keys on the server.")))
        except Exception: pass
    elif has("hour","intraday","alpaca"):
        tools.append("intraday")
        parts.append((intraday_note(21) or "The hourly collector has not produced results yet.")+" Hourly price changes are saved and tested against the daily-only brain; they are used for predictions only if they improve the unseen-data score by a clear margin.")
    elif has("monte carlo","simulation","simulate","statistic","random sampl","bootstrap","equal probab"):
        tools.append("monte_carlo"); dates,closes=_load_csv_series(); mc=monte_carlo_drop_risk(closes)
        if mc:
            drop_pct="%.0f"%(100*mc["drop_threshold"])
            parts.append("A pure statistics check, with no neural network involved: I took %d random real stretches of the market's history, each %d trading days long and each picked with equal probability (1 in %d), and counted how often a drop of %s%% or more happened inside one. Answer: %.1f%% of the time, with a 95%% confidence range of %.1f%% to %.1f%%."%(mc["simulations"],mc["horizon_days"],mc["windows_available"],drop_pct,100*mc["probability"],100*mc["confidence_95_low"],100*mc["confidence_95_high"]))
            parts.append("Compare this to the networks' own estimate by asking for a buy/sell call - if the two disagree by a lot, that's worth noticing.")
        else: parts.append("Not enough price history yet to run the simulation reliably.")
    elif has("mistake","wrong","learn from","got it wrong","missed","fail"):
        tools.append("recent_mistakes"); miss=recent_mistakes(3)
        if miss: parts.append("Recent wrong calls, with the actual numbers: "+" | ".join(m["explanation"] for m in miss))
        else: parts.append("No graded calls have come back wrong yet (or none have been graded long enough to tell).")
        parts.append("New data, including days it got wrong, is included the next time the networks retrain (about every 21 trading days); a retrained version only replaces the current one if it proves better on data it has not seen. The networks don't reason about any one mistake in words - that explanation above is a description of the numbers, not the network's own thinking.")
    elif has("target","goal","how are you doing against","on track"):
        tools.append("target_progress"); tp=None
        if tp:
            pr="%+.1f%%"%tp["paper_return_pct"] if tp.get("paper_return_pct") is not None else "not available yet (paper trading isn't on, or hasn't run long enough)"
            bh="%+.1f%%"%tp["buy_and_hold_pct"] if tp.get("buy_and_hold_pct") is not None else "not available"
            parts.append("Target: %+.1f%% by %s (set %s). Paper account so far: %s. Buy-and-hold over the same period: %s. %s"%(tp["target_pct"],tp.get("by_date") or "an unspecified date",tp["set_at"][:10],pr,bh,tp["note_fixed"]))
        else: parts.append("Targets and paper-trading results are private to the owner and are not shown publicly. A target never changes the model's call.")
    elif has("risk","drawdown","crash","drop","fall","correction","danger","safe"):
        tools.append("neural_risk_brain"); v=final_verdict("next month")
        if v.get("interim") or v.get("risk_state")=="UNKNOWN": parts.append("MIRA returns HOLD because a required input is unavailable. "+v.get("decision_reason",""))
        else: parts.append("The neural model estimates an 8%%+ drawdown risk of %s against an alarm threshold of %s. Risk state: %s. Trend signal: %s. Reconciled decision: %s (%s)."%(pc(v["risk_score"]),pc(v["alarm_level"]),v["risk_state"],v["trend_verdict"],v["verdict"],v["decision_status"]))
    elif has("how","work","what are you","what is","explain","neural","network","model","why","who"):
        parts.append("I'm MIRA, a market research assistant. A small ensemble of neural networks, written in plain Python, reads 13 price-based signals from about 40 years (1986 onward) of Nasdaq-100 and index history and estimates the chance of a drop of 8% or more in the next month. At or above its alarm level the call is SELL, otherwise BUY. A simple trend rule gives a second opinion. Each call is logged and graded later, and the networks retrain themselves on new data and only replace themselves if they prove better on unseen days.")
        parts.append("I can't predict tomorrow's move reliably - nobody can - and I'm not financial advice.")
    else:
        try:
            v=final_verdict("tomorrow")
            if not v.get("interim"): parts.append("MIRA's current decision is %s (%s): trend %s; downside-risk state %s, score %s vs alarm %s."%(v["verdict"],v.get("decision_status"),v.get("trend_verdict"),v.get("risk_state"),pc(v.get("risk_score")),pc(v.get("alarm_level"))))
        except Exception: pass
        parts.append("You can ask me things like: \"should I buy or sell today / this week / next year?\", \"how accurate are you?\", \"how do you learn?\", \"how fresh is your data?\", \"what is the drop risk?\" or \"how do you work?\".")
    parts.append("Research only, not financial advice.")
    return {"answer":" ".join(parts),"actions":[{"tool":t,"observation":{"source":"saved MIRA results"}} for t in tools],"model":"MIRA built-in answers"}

# ---- Optional cloud language model: lets the chat answer ANY question, grounded in MIRA's own saved numbers ----
LLM_KEY=os.environ.get("ANTHROPIC_API_KEY","")
LLM_MODEL=os.environ.get("MIRA_LLM_MODEL","claude-haiku-5-5")
CHAT_HISTORY=[]

def mira_facts(private=False):
    brain=brain_load(); lines=[]
    if brain and brain.get("eval"):
        sb=chart_scoreboard(brain)
        if sb.get("available"):
            lines.append("Unseen-data test (%s days): downside-risk classifications right %.1f%%; always predicting no alarm would be right %.1f%%; risk-ranking AUC %.2f; caught %.0f%% of real 8%%+ drops; %.0f%% of alarms followed by a drop (base rate %.0f%%)."%(sb["days"],100*sb["call_accuracy"],100*sb["always_buy_accuracy"],sb["auc"] or 0,100*(sb["drops_caught"] or 0),100*sb["alarm_precision"],100*sb["drop_base_rate"]))
            if sb.get("strategy"): lines.append("Following its calls: %.1f%%/yr, worst fall %.0f%%; buy-and-hold: %.1f%%/yr, worst fall %.0f%%."%(100*sb["strategy"]["cagr"],100*sb["strategy"]["max_drawdown"],100*sb["buy_and_hold"]["cagr"],100*sb["buy_and_hold"]["max_drawdown"]))
        lines.append("Brain trained through %s."%brain.get("trained_through"))
        lines.append("Inputs used: %s."%("13 price-based features only" if brain.get("features",BRAIN_FEATURES)==BRAIN_FEATURES else "13 price-based features plus 3 macro features (10-year Treasury yield change, high-yield credit spread, recession flag)"))
    try:
        v=final_verdict("next month")
        if not v.get("interim"): lines.append("Current decision (next month): %s (%s), trend %s, downside risk %s (%.1f%% vs %.1f%% alarm), official data through %s."%(v["verdict"],v.get("decision_status"),v.get("trend_verdict"),v.get("risk_state"),100*v["risk_score"],100*v["alarm_level"],v.get("model_data_as_of",v["as_of"])))
    except Exception: pass
    try:
        mc=(v or {}).get("monte_carlo")
        if mc: lines.append("Pure-statistics check (no learning, historical bootstrap of %d real %d-day windows): a drop like this happened %.1f%% of the time (95%% range %.1f%%-%.1f%%)."%(mc["simulations"],mc["horizon_days"],100*mc["probability"],100*mc["confidence_95_low"],100*mc["confidence_95_high"]))
    except Exception: pass
    try: lines.append(price_data_note())
    except Exception: pass
    lr=chart_live_record(); lines.append("Live record: %d calls logged, %d graded a month later, %d right."%(lr["calls"],lr["month_graded"],lr["month_right"]))
    try:
        miss=recent_mistakes(1)
        if miss: lines.append("Most recent wrong call: "+miss[0]["explanation"])
    except Exception: pass
    try:
        tp=target_progress() if private else None
        if tp:
            pr="%+.1f%%"%tp["paper_return_pct"] if tp.get("paper_return_pct") is not None else "n/a"
            bh="%+.1f%%"%tp["buy_and_hold_pct"] if tp.get("buy_and_hold_pct") is not None else "n/a"
            lines.append("Owner-set target: %+.1f%% by %s. Actual paper account so far: %s. Buy-and-hold over the same period: %s. %s"%(tp["target_pct"],tp.get("by_date") or "n/a",pr,bh,tp["note_fixed"]))
    except Exception: pass
    return "\n".join(lines)

LLM_SYSTEM=("You are the chat assistant of MIRA (Market Insight & Risk Assistant). How MIRA works: an ensemble of 3 small neural networks written in plain Python (no libraries) reads 13 price-based signals from about 40 years (1986 onward) of Nasdaq-100/index history and estimates the chance of a drop of 8%+ within ~21 trading days; at or above an alarm level (chosen on validation data) the call is SELL, otherwise BUY. A simple trend rule (price vs moving average) is a second opinion. Every call is logged and graded 1 day and 1 month later; the networks retrain about every 21 new trading days and replace themselves only if better on unseen days. Live QQQ prices and hourly bars come from the free Alpaca IEX feed when keys are set; official daily closes come from FRED. The page /live shows candlesticks, the model's calls and whether they were right. The numbers are produced by MIRA's Python networks, never by you. "
"Qwen/Ollama: an optional local language model that only ran on the owner's own PC to explain results in chat; it never makes predictions. On the cloud server it is not installed, so this chat uses you instead. "
"Rules: answer the user's actual question directly and conversationally in plain words, short. For any statistic use ONLY the facts provided; never invent numbers. Be honest that the model does not beat always-BUY on call accuracy if asked about accuracy, and that no one can reliably predict markets. If asked for a buy/sell decision, give the current call from the facts and mention the time frame limits. This is research, not financial advice; mention that briefly only when giving a view. Questions unrelated to markets or MIRA: answer briefly if harmless, then offer market help.")

def llm_answer(prompt):
    facts=mira_facts()
    msgs=[]
    for q,a in (): msgs+= [{"role":"user","content":q},{"role":"assistant","content":a}]
    msgs.append({"role":"user","content":"MIRA's current facts:\n"+facts+"\n\nUser question: "+prompt})
    body=json.dumps({"model":LLM_MODEL,"max_tokens":500,"system":LLM_SYSTEM,"messages":msgs}).encode()
    req=Request("https://api.anthropic.com/v1/messages",data=body,headers={"x-api-key":LLM_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"})
    with urlopen(req,timeout=40) as r: out=json.loads(r.read())
    text="".join(b.get("text","") for b in out.get("content",[]) if b.get("type")=="text").strip()
    if not text: raise ValueError("empty answer")
    CHAT_HISTORY.append((prompt[:300],text[:800]))
    return {"answer":text,"actions":[{"tool":"mira_facts","observation":{"source":"saved MIRA results"}}],"model":"MIRA chat ("+LLM_MODEL+") grounded in MIRA's own numbers"}

# Any OpenAI-compatible chat service (Groq, Google Gemini's OpenAI endpoint, OpenRouter, ...) - many have free tiers.
FREE_LLM_URL=os.environ.get("MIRA_LLM_URL","").strip()      # e.g. https://api.groq.com/openai/v1/chat/completions
FREE_LLM_KEY=os.environ.get("MIRA_LLM_KEY","").strip()
FREE_LLM_NAME=os.environ.get("MIRA_LLM_NAME","").strip()    # model name exactly as the provider lists it

def free_llm_answer(prompt):
    msgs=[{"role":"system","content":LLM_SYSTEM}]
    for q,a in (): msgs+=[{"role":"user","content":q},{"role":"assistant","content":a}]
    msgs.append({"role":"user","content":"MIRA's current facts:\n"+mira_facts()+"\n\nUser question: "+prompt})
    body=json.dumps({"model":FREE_LLM_NAME,"messages":msgs,"max_tokens":500,"temperature":0.3}).encode()
    req=Request(FREE_LLM_URL,data=body,headers={"Authorization":"Bearer "+FREE_LLM_KEY,"Content-Type":"application/json"})
    with urlopen(req,timeout=40) as r: out=json.loads(r.read())
    text=(out["choices"][0]["message"]["content"] or "").strip()
    if not text: raise ValueError("empty answer")
    CHAT_HISTORY.append((prompt[:300],text[:800]))
    return {"answer":text,"actions":[{"tool":"mira_facts","observation":{"source":"saved MIRA results"}}],"model":"MIRA chat ("+FREE_LLM_NAME+") grounded in MIRA's own numbers"}

def ollama_chat(messages):
    body=json.dumps({"model":OLLAMA_MODEL,"messages":messages,"tools":TOOLS,"stream":False,"think":False,"options":{"num_predict":128,"temperature":0.1}}).encode()
    req=Request("http://127.0.0.1:11434/api/chat",data=body,headers={"Content-Type":"application/json"})
    with urlopen(req,timeout=300) as response: return json.loads(response.read())["message"]
def run_agent(payload):
    global ACTIVE_CSV, ACTIVE_TARGET, TASK
    ACTIVE_CSV=payload.get("csv",""); ACTIVE_TARGET=payload.get("target",""); TASK=payload.get("task","bank")
    prompt=payload.get("prompt","").strip()[:1000]
    if not prompt: raise ValueError("Enter a goal for the sandbox agent.")
    if is_decision_question(prompt): return answer_decision(prompt)
    if FREE_LLM_URL and FREE_LLM_KEY and FREE_LLM_NAME:
        try:
            visible_status("tool_call","MIRA chat is reading its saved results to answer.")
            out=free_llm_answer(prompt); log_question(prompt,"n/a"); visible_status("complete","Answer ready.",1); return out
        except Exception as exc: visible_status("observation","Free chat service unavailable ("+str(exc)[:80]+"); trying the next option.")
    if LLM_KEY:
        try:
            visible_status("tool_call","MIRA chat is reading its saved results to answer.")
            out=llm_answer(prompt); log_question(prompt,"n/a"); visible_status("complete","Answer ready.",1); return out
        except Exception as exc: visible_status("observation","Cloud chat unavailable ("+str(exc)[:80]+"); using built-in answers.")
    if not ollama_up():
        visible_status("tool_call","MIRA is answering from its saved results.")
        out=general_answer(prompt); log_question(prompt,"n/a"); visible_status("complete","Answer ready.",1); return out
    visible_status("interpreting","Reading your request and selecting from bounded local tools; private chain-of-thought is not exposed.")
    system=("You are the reasoning core of a local research agent. Understand the user's natural-language goal and use provided Python tools when useful. For ANY question about MIRA market forecast accuracy, market false-positive/false-negative rates, or market holdout metrics, you MUST call inspect_market_evaluation and use its returned values. Never use explore_simulation to answer a real market-model metric question. You may inspect the uploaded labeled dataset, train/evaluate the local neural predictor, inspect evaluation output, and explore the generated simulation when asked. Never claim to contact the internet, a real bank, a brokerage, or an exchange. Treat predictions as experiments, not financial advice. Give concise conclusions grounded in tool results. Do not reveal hidden chain-of-thought; report brief plans, actions, observations, and uncertainty.")
    messages=[{"role":"system","content":system},{"role":"user","content":prompt}]
    actions=[]
    for round_index in range(6):
        visible_status("reasoning","Local model is interpreting the request (round %d/6); internal chain-of-thought is private."%(round_index+1))
        try: message=ollama_chat(messages)
        except Exception:
            out=general_answer(prompt); visible_status("complete","Answer ready.",1); return out
        calls=message.get("tool_calls",[])
        if not calls:
            answer=message.get("content","") or "The local model returned no text."
            visible_status("complete","Agent answer ready · "+answer[:160],1)
            return {"answer":answer,"actions":actions,"model":OLLAMA_MODEL}
        messages.append(message)
        for call in calls:
            fn=call.get("function",{}); name=fn.get("name","")
            visible_status("tool_call","Local agent selected tool: "+name)
            if name=="inspect_dataset": result=inspect_dataset()
            elif name=="train_predictor": result=train({"task":TASK,"csv":ACTIVE_CSV,"target":ACTIVE_TARGET,"size":1200,"epochs":50})
            elif name=="inspect_holdout": result=LAST_RESULT or {"status":"No training run yet; call train_predictor first."}
            elif name=="inspect_market_evaluation": result=inspect_market_evaluation()
            elif name=="explore_simulation": result=scan()
            elif name=="forecast_nasdaq": result=forecast_nasdaq({"csv":ACTIVE_CSV,"close_col":ACTIVE_TARGET,"instrument":"Nasdaq-100 (NDX/QQQ)"})
            elif name=="forecast_overnight_intraday": result=forecast_overnight_intraday()
            else: result={"error":"Tool is not allowlisted."}
            compact={k:v for k,v in result.items() if k not in ("trace","logs","holdout_trace","learning_trace")}
            actions.append({"tool":name,"observation":compact})
            visible_status("observation","Tool "+name+" returned "+json.dumps(compact,ensure_ascii=False)[:180])
            messages.append({"role":"tool","tool_name":name,"content":json.dumps(compact)})
    answer="Stopped after six tool rounds. Review the action log and narrow the goal if more work is needed."
    visible_status("complete","Agent finished the bounded task; answer and tool observations are in the dashboard.",1)
    return {"answer":answer,"actions":actions,"model":OLLAMA_MODEL}

# ---- Live chart data for the /live page: candlesticks, the model's calls scored against what followed, and a live price ----
CHART_CACHE={"at":0.0,"days":None,"data":None}

def chart_daily_candles():
    """Daily QQQ candles built from the saved hourly bars (empty until hourly data has been collected)."""
    if not intraday_path().exists(): return {}
    out={}
    for d,rows in intraday_groups(intraday_load()).items():
        rows=sorted(rows)
        if len(rows)>=5: out[d]=[rows[0][1],max(r[2] for r in rows),min(r[3] for r in rows),rows[-1][4]]
    return out

def chart_replay(dates,closes,brain,days):
    """The model's call on each recent day, made by the evaluation network that was trained BEFORE those days, then scored against what followed."""
    n=len(closes); names=brain.get("features",BRAIN_FEATURES)
    full=brain_feature_rows(dates,closes); feats=[select_features(r,names) if r is not None else None for r in full]
    ys=brain_labels(closes); ce=brain["eval"]; thr=brain["threshold"]
    hold=brain_split(dates,closes)[5]; first=max(hold[0] if hold else 0,n-days)
    ids=[i for i in range(first,n) if feats[i] is not None]
    probs=brain_predict_ids(brain_ensemble(ce["members"]),ce["mean"],ce["scale"],feats,ids); out=[]
    for i in ids:
        v="SELL" if probs[i]>=thr else "BUY"; y=ys[i]
        out.append({"d":dates[i],"v":v,"risk_state":"ELEVATED" if v=="SELL" else "NORMAL","kind":"downside_risk_classification","p":round(probs[i],4),"o":None if y is None else int((v=="SELL")==bool(y))})
    return out

def chart_scoreboard(brain):
    ho=brain.get("holdout") or {}; br=ho.get("base_rate"); fs=ho.get("flagged_share"); fd=ho.get("flagged_drop_rate"); cd=ho.get("calm_drop_rate")
    if None in (br,fs,fd,cd): return {"available":False}
    tp=fs*fd; tn=(1-fs)*(1-cd)
    return {"available":True,"days":ho.get("rows"),"auc":ho.get("auc"),"auc_ci":ho.get("auc_ci"),"call_accuracy":tp+tn,"risk_classification_accuracy":tp+tn,"always_buy_accuracy":1-br,"drops_caught":(tp/br) if br else None,"risk_warning_recall":(tp/br) if br else None,"alarm_precision":fd,"risk_warning_precision":fd,"drop_base_rate":br,"metric_semantics":"All BUY/SELL labels in this scoreboard refer to the neural downside-risk classifier (SELL=alarm, BUY=no alarm), not a price-direction forecast.","prediction_task":"21-trading-day drawdown-risk classification","directional_accuracy_available":False,"strategy":ho.get("strategy"),"buy_and_hold":ho.get("buy_and_hold"),"alarm_level":brain.get("threshold"),"trained_through":brain.get("trained_through")}

def chart_live_record():
    """Separate live scorecards for the neural drawdown classifier and directional fusion."""
    try: led=json.loads((LEARN_DIR/"ledger.json").read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): led=[]
    m=[e for e in led if "dropped_8pct_21d" in e]
    try: dledger=json.loads(directional_ledger_path().read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): dledger=[]
    dm=[e for e in dledger if "actual_up" in e]
    recent=dm[-126:]
    signals=[e for e in recent if e.get("signal") in ("BUY","SELL")]
    directional={"matured":len(dm),
                 "recent_brier":(sum(e["brier"] for e in recent)/len(recent)) if recent else None,
                 "recent_logloss":(sum(e["logloss"] for e in recent)/len(recent)) if recent else None,
                 "signal_accuracy":(sum(int(e.get("direction_correct",False)) for e in signals)/len(signals)) if signals else None,
                 "graded_signals":len(signals)}
    return {"calls":len(led),"month_graded":len(m),
            "month_right":sum(int((e["verdict"]=="SELL")==e["dropped_8pct_21d"]) for e in m),
            "month_always_buy_right":sum(int(not e["dropped_8pct_21d"]) for e in m),
            "directional":directional,
            "month_metric_semantics":"Neural downside-risk classification only: SELL=drawdown alarm, BUY=no alarm. Not directional BUY/SELL accuracy.",
            "day_metric_semantics":"Directional forecasts are graded against the next completed close; the risk model is not a next-day direction model."}


def refresh_prices_background():
    """Same job as refresh_prices(), but for callers that must respond fast (a live page waiting on this request) and can't afford to block on a slow/unreachable FRED server (up to ~75s of retries). Kicks the real refresh off on its own thread if one isn't already running, and returns immediately either way - the page just shows whatever data is already on disk from the last successful refresh, instead of hanging."""
    if PRICE_LOCK.acquire(blocking=False):
        PRICE_LOCK.release()
        threading.Thread(target=refresh_prices,args=(True,),daemon=True).start()
    # else: a refresh is already in flight (this request or the background loop) - don't pile on, just use what's on disk right now.

def fetch_minute_bars(force=False):
    """Fetch recent Alpaca IEX one-minute OHLCV bars; WebSocket bars take precedence."""
    if not (MARKET_API_KEY and MARKET_API_SECRET): return []
    now=time.time()
    if not force and now-MARKET_BARS_CACHE["at"]<12:
        return list(MARKET_BARS_CACHE["bars"])
    try:
        req=Request("https://data.alpaca.markets/v2/stocks/QQQ/bars?timeframe=1Min&adjustment=raw&feed=iex&sort=asc&limit=120",
                    headers={"APCA-API-KEY-ID":MARKET_API_KEY,"APCA-API-SECRET-KEY":MARKET_API_SECRET,"Accept":"application/json"})
        with urlopen(req,timeout=8) as response: raw=json.loads(response.read())
        bars=[]
        for b in raw.get("bars") or []:
            try:
                vals={"t":b["t"],"o":float(b["o"]),"h":float(b["h"]),"l":float(b["l"]),"c":float(b["c"]),"v":float(b.get("v",0))}
                if vals["o"]>0 and vals["h"]>=max(vals["o"],vals["c"]) and vals["l"]<=min(vals["o"],vals["c"]): bars.append(vals)
            except (KeyError,TypeError,ValueError): continue
        MARKET_BARS_CACHE.update({"at":now,"bars":bars[-240:]})
    except Exception:
        # Preserve last good bars during a temporary provider outage; never fabricate a candle.
        pass
    return list(MARKET_BARS_CACHE["bars"])

def chart_payload(days=180):
    days=min((30,90,180,365,500),key=lambda k:abs(k-int(days))); now=time.time()
    if not (CHART_CACHE["data"] and CHART_CACHE["days"]==days and now-CHART_CACHE["at"]<60):
        refresh_prices_background()   # never block this request on a live network fetch - see function docstring
        dates,closes=load_price_series(); brain=brain_load(); candles=chart_daily_candles(); win=dates[-days:]
        # Recompute the causal quantitative snapshot from the latest official closes.
        # It is cached with the chart payload for 60 seconds to avoid repeated HMM/GARCH fits.
        try:
            q_dates,q_closes=_load_csv_series()
            quant=quantitative_intelligence_snapshot(q_closes)
        except Exception as q_exc:
            quant={"version":2,"available":False,"reason":"Quantitative snapshot unavailable: "+str(q_exc)[:120]}
        data={"as_of":dates[-1],"days":days,"dates":win,"closes":[round(c,2) for c in closes[-days:]],"candles":[{"d":d,"o":candles[d][0],"h":candles[d][1],"l":candles[d][2],"c":candles[d][3]} for d in win if d in candles],"data_note":price_data_note(),"live_record":chart_live_record(),"quantitative_intelligence":quant}
        if brain is not None and brain.get("eval") and brain.get("data_basis")=="official_daily_closes_v1":
            model_dates,model_closes=_load_csv_series()
            data["replay"]=chart_replay(model_dates,model_closes,brain,days); data["scoreboard"]=chart_scoreboard(brain)
        else: data["replay"]=[]; data["scoreboard"]={"available":False}
        try:
            # /live displays the fused next-session direction. Asking for "next month"
            # here incorrectly triggered the deliberate unsupported-horizon HOLD guard.
            v=final_verdict("next session")
            data["verdict"]={k:v.get(k) for k in ("verdict","confidence","interim","risk_score","alarm_level","risk_state","trend_verdict","brain_verdict","agree","risk_trend_alignment","decision_status","decision_reason","risk_horizon_days","model_data_as_of","model_data_source","data_age_calendar_days","data_fresh","risk_model_fresh","fusion","brain_job","as_of","monte_carlo")}
            data["verdict"]["directional_job"]=dict(NASDAQ_JOB)
        except Exception as exc: data["verdict"]={"error":str(exc)[:120]}
        CHART_CACHE.update({"at":now,"days":days,"data":data})
    out=dict(CHART_CACHE["data"])
    try:
        out["live"]=fetch_market_snapshot()
        ws_bars=(out["live"].get("minute_bars") or []) if isinstance(out["live"],dict) else []
        rest_bars=fetch_minute_bars()
        merged={b.get("t"):b for b in rest_bars if b.get("t")}
        for b in ws_bars:
            if b.get("t"): merged[b["t"]]=b
        out["intraday_candles"]=sorted(merged.values(),key=lambda b:b.get("t",""))[-240:]
        out["intraday_status"]={"source":"Alpaca IEX 1-minute OHLCV","streaming":bool(ws_bars),
                                "bars":len(out["intraday_candles"]),"status":"LIVE MINUTE BARS" if ws_bars else ("REST MINUTE BARS" if rest_bars else "WAITING FOR ALPACA MINUTE BARS")}
    except Exception as exc:
        out["live"]={"connected":False,"error":str(exc)[:100]}
        out["intraday_candles"]=[]
        out["intraday_status"]={"source":"Alpaca IEX","streaming":False,"bars":0,"status":"MINUTE BARS UNAVAILABLE"}
    return out

CHAT_RATE={}
ADMIN_TOKEN=os.environ.get("MIRA_ADMIN_TOKEN","").strip()
def is_public_request(h):
    if ADMIN_TOKEN and hmac.compare_digest(h.headers.get("X-Admin-Token","").encode(),ADMIN_TOKEN.encode()): return False
    if any(h.headers.get(k) for k in ("Cf-Connecting-Ip","X-Forwarded-For","X-Real-Ip","Forwarded")): return True
    return h.client_address[0] not in ("127.0.0.1","::1")

def client_ip(h):
    cf=(h.headers.get("Cf-Connecting-Ip") or "").strip()
    if cf: return cf
    xff=[x.strip() for x in (h.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
    return xff[-1] if xff else h.client_address[0]

def rate_ok(h,bucket,limit,global_limit=None):
    """Per-visitor limit (plus an optional shared limit for everyone) per minute. Uses the proxy-set address, never the client-writable first X-Forwarded-For entry."""
    now=time.time(); keys=[(bucket,client_ip(h))]; lims=[limit]
    if global_limit: keys.append((bucket,"*")); lims.append(global_limit)
    for k,lim in zip(keys,lims):
        CHAT_RATE[k]=[t for t in CHAT_RATE.get(k,[]) if now-t<60]
        if len(CHAT_RATE[k])>=lim: return False
    for k in keys: CHAT_RATE[k].append(now)
    if len(CHAT_RATE)>4000: CHAT_RATE.clear()
    return True

def chat_rate_ok(h,limit=15): return rate_ok(h,"chat",limit,global_limit=120)

def _chat_rate_ok_old(h,limit=15):
    ip=h.headers.get("Cf-Connecting-Ip") or (h.headers.get("X-Forwarded-For") or h.client_address[0]).split(",")[0].strip()
    now=time.time(); hits=[t for t in CHAT_RATE.get(ip,[]) if now-t<60]
    if len(hits)>=limit: CHAT_RATE[ip]=hits; return False
    hits.append(now); CHAT_RATE[ip]=hits
    if len(CHAT_RATE)>2000: CHAT_RATE.clear()
    return True

# The /live page is patched on the fly so one server.py carries every fix (no separate edits to live.html needed).
LIVE_PAGE_FIXES=[
 (r"""const worse=sb.call_accuracy<sb.always_buy_accuracy;""",
  r"""const worse=sb.call_accuracy<sb.always_buy_accuracy;const ciFlip=sb.auc_ci&&sb.auc_ci[0]<=0.5;"""),
 (r"""+'<div class="stat"><b>'+(sb.auc!=null?sb.auc.toFixed(2):'—')+'</b><span>Risk-ranking score (0.50 = coin flip)</span></div>'""",
  r"""+'<div class="stat '+(ciFlip?'warn':'')+'"><b>'+(sb.auc!=null?sb.auc.toFixed(2):'—')+'</b><span>Risk-ranking score (0.50 = coin flip)'+(sb.auc_ci?' · 95% range '+sb.auc_ci[0].toFixed(2)+'–'+sb.auc_ci[1].toFixed(2):'')+(ciFlip?' — the range includes 0.50, so this is not clearly better than chance':'')+'</span></div>'"""),
 (r"""<div class="sub">Confidence: <b>""",
  r"""<div class="sub">Research signal, not advice · Confidence: <b>"""),
]

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(ROOT),**kwargs)
    def translate_path(self,path):
        p=urlsplit(path).path
        if p.startswith("/data/"):
            f=(DATA_DIR/p[len("/data/"):]).resolve()
            if DATA_DIR.resolve() in f.parents and f.exists(): return str(f)
        return super().translate_path(path)
    STATIC_OK_SUFFIX=(".html",".css",".js",".png",".jpg",".jpeg",".svg",".ico",".webmanifest",".txt")
    PUBLIC_DATA_FILES=("/data/nasdaq100_daily.csv","/data/models/nasdaq100_model.json","/data/models/nasdaq100_training_report.json")
    def static_allowed(self,path):
        """Only pages, the price CSV and the two model files are public. Source code, learning logs, ledgers and question logs are never served."""
        low=path.lower()
        if "%" in low or ".." in low or "\\" in low: return False
        if low.startswith("/api/") or low in ("/","/live"): return True
        if low.startswith("/data/"): return low in self.PUBLIC_DATA_FILES
        return low.endswith(self.STATIC_OK_SUFFIX)
    def list_directory(self,path):
        self.send_error(404); return None
    def do_GET(self):
        path=urlsplit(self.path).path
        if not self.static_allowed(path): self.send_error(404); return
        public=is_public_request(self)
        if public and path.startswith("/api/") and path not in ("/api/status","/api/market-data","/api/ollama") and not rate_ok(self,"get",30): self.send_error(429); return
        path=urlsplit(self.path).path
        if path=="/api/market-data":
            body=json.dumps(fetch_market_snapshot()).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/ollama":
            try:
                req=Request("http://127.0.0.1:11434/api/tags")
                with urlopen(req,timeout=2) as response: tags=json.loads(response.read())
                models=[m.get("name","") for m in tags.get("models",[])]
                ready=OLLAMA_MODEL in models
                body=json.dumps({"connected":True,"ready":ready,"required_model":OLLAMA_MODEL,"models":models,"message":("Ollama is ready." if ready else "Ollama is running, but "+OLLAMA_MODEL+" is not installed. Run: ollama pull "+OLLAMA_MODEL)}).encode()
            except Exception:
                body=json.dumps({"connected":False,"ready":False,"required_model":OLLAMA_MODEL,"models":[],"message":"Ollama is not running at 127.0.0.1:11434. Start the Ollama app, then check again."}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/paper-trade":
            body=json.dumps(paper_summary() if not public else {"enabled":False,"error":"Paper trading is private to the owner."}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/brain":
            body=json.dumps(brain_status()).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/mistakes":
            try: body=json.dumps({"mistakes":recent_mistakes(5),"target":(None if public else target_progress())}).encode()
            except Exception as exc: body=json.dumps({"mistakes":[],"target":None,"error":str(exc)[:150]}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/facts":
            try: body=json.dumps({"facts":mira_facts(private=not public)}).encode()
            except Exception as exc: body=json.dumps({"facts":"","error":str(exc)[:150]}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/chart":
            try:
                q=dict(p.split("=",1) for p in urlsplit(self.path).query.split("&") if "=" in p)
                body=json.dumps(chart_payload(int(q.get("days","180")))).encode()
            except Exception as exc: body=json.dumps({"error":str(exc)}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path in ("/live","/live.html"):
            try: html=(ROOT/"live.html").read_text(encoding="utf-8")
            except OSError: self.send_error(404); return
            for _old,_new in LIVE_PAGE_FIXES: html=html.replace(_old,_new)
            body=html.encode("utf-8")
            self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/intraday":
            body=json.dumps({k:v for k,v in INTRADAY_STATE.items()}|{"profile":intraday_profile()}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/prices":
            body=json.dumps({k:v for k,v in PRICE_STATE.items() if k!="extra"}|{"live_days":[(d,round(v,2),s) for d,v,s in PRICE_STATE["extra"]],"note":price_data_note(),"autopilot":(AUTOPILOT if not public else {"last_run":AUTOPILOT["last_run"]})}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/status":
            body=json.dumps(ENGINE_STATUS).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        super().do_GET()
    def do_POST(self):
        global MARKET_API_KEY, MARKET_API_SECRET, MARKET_CACHE
        try:
            length=int(self.headers.get("Content-Length","0"))
            if length>(20000 if is_public_request(self) else 8000000): raise PermissionError("Request too large.")
            payload=json.loads(self.rfile.read(length))
            path=urlsplit(self.path).path
            if is_public_request(self):
                # Visitors on the public internet (tunnel / cloud host) may only chat. Keys, training and retraining are owner-only (use localhost).
                if path!="/api/agent": raise PermissionError("This control is only available on the owner's own computer.")
                if not chat_rate_ok(self): raise PermissionError("Too many questions in a minute; please wait a little and ask again.")
            if path=="/api/market-data/config":
                key=str(payload.get("key","")).strip(); secret=str(payload.get("secret","")).strip()
                if payload.get("clear") is True:
                    MARKET_API_KEY=MARKET_API_SECRET=""; MARKET_CACHE={"at":0.0,"data":None}; clear_market_credentials()
                    result={"connected":False,"configured":False,"message":"Market-data credentials cleared from memory and the local encrypted store."}
                elif not key and not secret:
                    if not MARKET_API_KEY or not MARKET_API_SECRET: raise ValueError("Enter both Alpaca credentials first, or use Forget saved keys to remove stored credentials.")
                    snapshot=fetch_market_snapshot(force=True)
                    if not snapshot.get("connected"): raise ValueError(snapshot.get("error","Could not reconnect using the saved credentials."))
                    start_market_stream()
                    result={"connected":True,"message":"Reconnected using credentials saved in the local encrypted store.","snapshot":snapshot,"stream_status":MARKET_STREAM.get("status")}
                elif not key or not secret or len(key)>256 or len(secret)>256:
                    raise ValueError("Enter both Alpaca key ID and secret.")
                else:
                    old_key,old_secret=MARKET_API_KEY,MARKET_API_SECRET
                    MARKET_API_KEY,MARKET_API_SECRET=key,secret
                    MARKET_CACHE={"at":0.0,"data":None}
                    snapshot=fetch_market_snapshot(force=True)
                    if not snapshot.get("connected"):
                        MARKET_API_KEY,MARKET_API_SECRET=old_key,old_secret
                        MARKET_CACHE={"at":0.0,"data":None}
                        raise ValueError(snapshot.get("error","Could not connect to the free Alpaca IEX feed."))
                    try:
                        save_market_credentials(key,secret)
                        stored=True
                    except (OSError,sqlite3.Error,ValueError): stored=False
                    start_market_stream()
                    result={"connected":True,"message":("Connected. Credentials are saved encrypted on this Windows account." if stored else "Connected for this session, but secure local storage was unavailable; keys will be lost when the server stops."),"snapshot":snapshot,"stream_status":MARKET_STREAM.get("status")}
            elif path=="/api/train": result=train(payload)
            elif path=="/api/scan": result=scan()
            elif path=="/api/agent": result=run_agent(payload)
            elif path=="/api/nasdaq": result=forecast_nasdaq(payload)
            elif path=="/api/brain": result=start_brain_job(force=bool(payload.get("force",True)))
            elif path=="/api/paper-trade/toggle": paper_set_enabled(bool(payload.get("enabled"))); result=paper_summary()
            elif path=="/api/paper-trade/config": result=paper_set_config(payload.get("trade_pct"),payload.get("take_profit_pct"),payload.get("stop_loss_pct"))
            elif path=="/api/overnight": result=forecast_overnight_intraday()
            elif path=="/api/target": result=target_set(payload.get("pct"),payload.get("by_date",""),payload.get("note",""))
            elif path=="/api/paper-trade/sync": result=paper_sync_to_verdict()
            else: self.send_error(404); return
            body=json.dumps(result).encode(); code=200
        except PermissionError as exc:
            # Blocked owner-only action from a public visitor: tell that visitor, but never touch the shared live-status seen by everyone.
            body=json.dumps({"error":str(exc)}).encode(); code=403
        except Exception as exc:
            visible_status("error",str(exc),0.0)
            body=json.dumps({"error":str(exc)}).encode(); code=400
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)

if __name__=="__main__":
    import sys
    if "--train" in sys.argv:
        print("Training the MIRA neural risk brain on all of your price history. This takes a few minutes...")
        BRAIN_JOB["running"]=True; brain_job(force=True)
        print(BRAIN_JOB["message"])
        b=brain_load()
        if b: print("Holdout test (days the networks never saw): AUC %s | trained through %s"%(("%.3f"%b["holdout"]["auc"]) if b["holdout"].get("auc") is not None else "n/a",b["trained_through"]))
        print("Done. Start the server normally with: python server.py"); raise SystemExit
    try:
        if brain_load() is None: print("No trained neural risk brain yet: training it in the background now (a few minutes)."); start_brain_job(False)
    except Exception as exc: print("Could not start brain training:",exc)
    start_market_stream()
    print("Alpaca stream status:",MARKET_STREAM.get("status"))
    threading.Thread(target=price_updater_loop,daemon=True).start(); print("Live price updater started: official closes every 30 min (5 min while the market is open).")
    threading.Thread(target=paper_watch_loop,daemon=True).start(); print("Paper take-profit/stop-loss watcher started: checks every 5 seconds while the market is open.")
    def keepalive_loop():
        # Free cloud hosts put idle servers to sleep, which stops learning. Visiting our own public address every 10 minutes keeps it awake.
        url=os.environ.get("RENDER_EXTERNAL_URL","").rstrip("/")
        while url:
            time.sleep(600)
            try: urlopen(Request(url+"/api/status"),timeout=30).read()
            except Exception: pass
    threading.Thread(target=keepalive_loop,daemon=True).start()
    port=int(os.environ.get("PORT",8765))
    print(f"Python model server running on port {port}")
    QuietServer(("0.0.0.0",port),Handler).serve_forever()