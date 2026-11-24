from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import socket, sys, threading

class QuietServer(ThreadingHTTPServer):
    """Same as ThreadingHTTPServer, except it doesn't print a scary traceback when a browser tab just cancels a poll mid-response (normal when a page refreshes or a newer request replaces an older one) - a real error still prints normally."""
    def handle_error(self,request,client_address):
        exc=sys.exc_info()[1]
        if isinstance(exc,(ConnectionAbortedError,ConnectionResetError,BrokenPipeError,socket.timeout)): return
        super().handle_error(request,client_address)
from pathlib import Path
import json, math, random, csv, io, time, os, sqlite3, ctypes, hmac, re
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
MARKET_REFRESH_LOCK = threading.Lock()
MARKET_LEARNING_LAST = 0.0
MINUTE_CACHE={"at":0.0,"bars":[],"status":"not fetched","error":None}
MINUTE_REFRESH_LOCK=threading.Lock()


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

def fetch_market_snapshot(force=False):
    global MARKET_CACHE, MARKET_LEARNING_LAST
    if not MARKET_API_KEY or not MARKET_API_SECRET:
        return {"connected":False,"configured":False,"provider":"Alpaca Basic · free IEX","symbol":"QQQ","error":"Connect free Alpaca market-data keys to enable live quotes."}
    if not force and MARKET_CACHE["data"] and time.time()-MARKET_CACHE["at"] < 3:
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
    if display_price is not None and time.time()-MARKET_LEARNING_LAST>=60:
        MARKET_LEARNING_LAST=time.time()
        record_learning_event("alpaca_market","live_quote_snapshot","QQQ",{"price":display_price,"bid":bid,"ask":ask,"trade_at":stamp,"quote_at":quote_stamp,"feed":feed,"session":session,"freshness":freshness},observed_at=stamp or quote_stamp or now.isoformat(timespec="seconds"))
    return result


def fetch_minute_bars(force=False):
    """Fetch real completed Alpaca IEX 1-minute QQQ bars; never synthesize candles."""
    if not (MARKET_API_KEY and MARKET_API_SECRET):
        return {"bars":[],"status":"Alpaca keys not configured","error":"Connect Alpaca keys to enable 1-minute candles."}
    if not force and MINUTE_CACHE["bars"] and time.time()-MINUTE_CACHE["at"]<15:
        return {"bars":MINUTE_CACHE["bars"],"status":"cached Alpaca IEX 1-minute bars","error":None}
    end=datetime.now(timezone.utc); start=end-timedelta(days=2)
    params=("timeframe=1Min&adjustment=raw&feed=iex&sort=asc&limit=10000"
            "&start="+start.strftime("%Y-%m-%dT%H:%M:%SZ")+
            "&end="+end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    req=Request("https://data.alpaca.markets/v2/stocks/QQQ/bars?"+params,
        headers={"APCA-API-KEY-ID":MARKET_API_KEY,
                 "APCA-API-SECRET-KEY":MARKET_API_SECRET,"Accept":"application/json"})
    try:
        with urlopen(req,timeout=12) as response: raw=json.loads(response.read())
        bars=[]
        for b in raw.get("bars") or []:
            try:
                row={"t":b["t"],"o":float(b["o"]),"h":float(b["h"]),
                     "l":float(b["l"]),"c":float(b["c"]),"v":float(b.get("v",0))}
                if row["o"]>0 and row["h"]>=max(row["o"],row["c"]) and row["l"]<=min(row["o"],row["c"]):
                    bars.append(row)
            except (KeyError,TypeError,ValueError): continue
        bars=sorted({b["t"]:b for b in bars}.values(),key=lambda x:x["t"])[-1000:]
        MINUTE_CACHE.update({"at":time.time(),"bars":bars,
            "status":"%d real Alpaca IEX 1-minute bars"%len(bars),"error":None})
        return {"bars":bars,"status":MINUTE_CACHE["status"],"error":None}
    except Exception as exc:
        message="Alpaca minute-bar request failed: "+(str(exc)[:150] or exc.__class__.__name__)
        MINUTE_CACHE["error"]=message
        return {"bars":MINUTE_CACHE["bars"],"status":"minute-bar feed unavailable","error":message}

def refresh_minute_bars_background():
    if not (MARKET_API_KEY and MARKET_API_SECRET): return
    if not MINUTE_REFRESH_LOCK.acquire(blocking=False): return
    def _worker():
        try: fetch_minute_bars(force=True)
        finally: MINUTE_REFRESH_LOCK.release()
    threading.Thread(target=_worker,daemon=True,name="mira-alpaca-1m").start()


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
        record_learning_event("user_dataset","uploaded_csv_dataset",str(payload.get("instrument") or "CUSTOM"),{"rows":len(data),"numeric_features":features,"target":target,"label_classes":sorted(set(row["y"] for row in data)),"raw_customer_rows_stored":False})
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
    _supabase_restore_file(report_path,"models/nasdaq100_training_report.json")
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
    def features(i):
        volchange=0.0
        if volume_col and prices[i][2]>0 and prices[i-1][2]>0:
            volchange=max(-3,min(3,math.log(prices[i][2]/prices[i-1][2])))
        return [daily[i],close[i]/close[i-2]-1,close[i]/close[i-5]-1,close[i]/close[i-10]-1,close[i]/close[i-20]-1,std(daily[i-4:i+1]),std(daily[i-19:i+1]),volchange]
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
    if not raw_test: raise ValueError("Not enough dated examples to form a chronological holdout.")
    if len(raw_fit)<30 or len(raw_validation)<30:
        # Small uploaded histories cannot support a separate threshold set.
        raw_fit=examples[:max(1,cut-20)]; raw_validation=[]
    mean=[sum(o["x"][j] for o in raw_fit)/len(raw_fit) for j in range(8)]
    scale=[math.sqrt(sum((o["x"][j]-mean[j])**2 for o in raw_fit)/len(raw_fit)) or 1 for j in range(8)]
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
        m={"w1":[[random.uniform(-.15,.15) for _ in range(9)] for _ in range(8)],"w2":[random.uniform(-.15,.15) for _ in range(9)]}
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
    visible_status("preparing","Building chronological examples and normalizing eight price features.",0.08)
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
    full_mean=[sum(o["x"][j] for o in examples)/len(examples) for j in range(8)]
    full_scale=[math.sqrt(sum((o["x"][j]-full_mean[j])**2 for o in examples)/len(examples)) or 1 for j in range(8)]
    full_up,_,_=fit_ensemble(scaled(examples,"up",full_mean,full_scale),epochs=direction_best_epochs,label="direction final fit",base=0.52,span=0.12)
    full_risk,_,_=fit_ensemble(scaled(examples,"risk",full_mean,full_scale),epochs=stress_best_epochs,label="drawdown-risk final fit",base=0.64,span=0.12)
    full_vol,_,_=fit_ensemble(scaled(examples,"volatility",full_mean,full_scale),epochs=volatility_best_epochs,label="volatility-regime final fit",base=0.76,span=0.12)
    latest=[(v-full_mean[j])/full_scale[j] for j,v in enumerate(features(len(close)-1))]
    p_up=predict(full_up,latest);p_risk=predict(full_risk,latest);p_volatility=predict(full_vol,latest)
    if direction_balanced<=.52 or stress_balanced<=.52: signal="HOLD / NO EDGE DETECTED"
    elif p_up>=.55 and p_risk<.25: signal="BUY BIAS (RESEARCH)"
    elif p_up<=.45 or p_risk>=.35: signal="SELL / DEFENSIVE BIAS (RESEARCH)"
    else: signal="HOLD / UNCERTAIN"
    try:
        event_rows=list(csv.DictReader((DATA_DIR/"historical_stress_events.csv").open(encoding="utf-8")))
        episode_count=sum(1 for e in event_rows if e["asset"]=="Nasdaq-100" and "drawdown" in e["event_type"])
    except OSError: episode_count=0
    model_dir=DATA_DIR/"models";model_dir.mkdir(exist_ok=True)
    saved={"model_name":"MIRA — Market Insight & Risk Assistant","instrument":payload.get("instrument","Nasdaq-100"),"as_of":prices[-1][0].date().isoformat(),"features":["return_1d","return_2d","return_5d","return_10d","return_20d","volatility_5d","volatility_20d","volume_log_change"],"mean":full_mean,"scale":full_scale,"direction_model":full_up,"drawdown_risk_model":full_risk,"volatility_regime_model":full_vol,"stress_threshold":stress_threshold,"train_rows":len(examples),"architecture":"Three 8 → 8 → 1 neural-network ensembles (9 MLP members total)","ensemble_members":3,"validation_best_epochs":{"direction":direction_best_epochs,"stress":stress_best_epochs,"volatility_regime":volatility_best_epochs}}
    (model_dir/"nasdaq100_model.json").write_text(json.dumps(saved,indent=2),encoding="utf-8")
    _supabase_backup_file(model_dir/"nasdaq100_model.json","models/nasdaq100_model.json")
    MODEL=full_up
    NASDAQ_RESULT={"instrument":saved["instrument"],"as_of":saved["as_of"],"signal":signal,"next_session_up_probability":round(p_up,5),"next20_session_10pct_drawdown_risk":round(p_risk,5),"next20_session_high_volatility_probability":round(p_volatility,5),"holdout_direction_accuracy":up["accuracy"],"holdout_direction_balanced_accuracy":direction_balanced,"direction_majority_baseline":up_base,"holdout_direction_precision":up["precision"],"holdout_direction_recall":up["recall"],"holdout_direction_fpr":up["fpr"],"holdout_direction_fnr":up["fnr"],"direction_confusion":up["counts"],"holdout_stress_accuracy":risk["accuracy"],"holdout_stress_balanced_accuracy":stress_balanced,"stress_majority_baseline":risk_base,"stress_precision":risk["precision"],"stress_recall":risk["recall"],"stress_fpr":risk["fpr"],"stress_fnr":risk["fnr"],"stress_confusion":risk["counts"],"volatility_regime_holdout_accuracy":volatility["accuracy"],"volatility_regime_balanced_accuracy":(volatility["recall"]+1-volatility["fpr"])/2,"volatility_regime_baseline_accuracy":max(sum(o["y"] for o in test_vol),len(test_vol)-sum(o["y"] for o in test_vol))/len(test_vol),"stress_threshold":stress_threshold,"stress_threshold_validation_f2":threshold_selection["f2"],"stress_validation_precision":threshold_selection.get("validation_precision"),"stress_validation_recall":threshold_selection.get("validation_recall"),"validation_rows":len(raw_validation),"holdout_rows":len(raw_test),"training_rows":len(raw_fit),"purged_boundary_rows":20,"validation_best_epochs":{"direction":direction_best_epochs,"stress":stress_best_epochs,"volatility_regime":volatility_best_epochs},"historical_nasdaq100_20pct_drawdowns":episode_count,"target":"up/down: next close; 20-session 10% drawdown; elevated forward volatility relative to trailing volatility","learning_trace":logs,"holdout_trace":up["trace"][-30:],"saved_model":"data/models/nasdaq100_model.json","probability_warning":"Experimental ensemble outputs; not calibrated, not a trade instruction."}
    confusion=lambda counts:{"true_positive":counts["tp"],"false_positive":counts["fp"],"true_negative":counts["tn"],"false_negative":counts["fn"]}
    vol_base=max(sum(o["y"] for o in test_vol),len(test_vol)-sum(o["y"] for o in test_vol))/len(test_vol)
    report={"model_name":saved["model_name"],"instrument":saved["instrument"],"data_as_of":saved["as_of"],"trained_on":datetime.now().date().isoformat(),"signal":signal,"next_session_up_probability":round(p_up,5),"next20_session_10pct_drawdown_risk":round(p_risk,5),"next20_session_high_volatility_probability":round(p_volatility,5),"direction":{"holdout_accuracy":up["accuracy"],"majority_baseline_accuracy":up_base,"balanced_accuracy":direction_balanced,"precision":up["precision"],"recall":up["recall"],"false_positive_rate":up["fpr"],"false_negative_rate":up["fnr"],"confusion":confusion(up["counts"])},"stress":{"holdout_accuracy":risk["accuracy"],"majority_baseline_accuracy":risk_base,"balanced_accuracy":stress_balanced,"precision":risk["precision"],"recall":risk["recall"],"false_positive_rate":risk["fpr"],"false_negative_rate":risk["fnr"],"confusion":confusion(risk["counts"]),"decision_threshold":stress_threshold,"threshold_selection":"F2 on separate chronological validation block","validation_precision":threshold_selection.get("validation_precision"),"validation_recall":threshold_selection.get("validation_recall")},"volatility_regime":{"holdout_accuracy":volatility["accuracy"],"majority_baseline_accuracy":vol_base,"balanced_accuracy":(volatility["recall"]+1-volatility["fpr"])/2,"precision":volatility["precision"],"recall":volatility["recall"],"false_positive_rate":volatility["fpr"],"false_negative_rate":volatility["fnr"],"confusion":confusion(volatility["counts"]),"target":"next 20-session realized volatility exceeds 1.35 times the trailing 20-session realized volatility"},"train_rows":len(raw_fit),"validation_rows":len(raw_validation),"holdout_rows":len(raw_test),"purged_boundary_rows":20,"validation_best_epochs":{"direction":direction_best_epochs,"stress":stress_best_epochs,"volatility_regime":volatility_best_epochs},"ensemble_members_per_task":3,"features":["1/2/5/10/20-session returns","5/20-session volatility","volume change if provided"],"target_definitions":{"direction":"next recorded close higher than current close","stress":"any close in next 20 sessions at least 10% below current close"},"crash_event_counts":{"nasdaq100_daily_20pct_drawdowns":episode_count},"training_method":"Three independently seeded neural networks per task; ensemble predictions average member probabilities. Each member uses validation-loss early stopping and the final serving ensemble is refit using selected epoch counts.","limitation":"One chronological holdout after a 20-session purge; the stress decision threshold is selected on a separate validation block to favor recall. Probabilities are not calibrated. Research output only."}
    (model_dir/"nasdaq100_training_report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    _supabase_backup_file(model_dir/"nasdaq100_training_report.json","models/nasdaq100_training_report.json")
    visible_status("complete","Forecast complete · signal %s · direction %.1f%% · stress risk %.1f%%"%(signal,p_up*100,p_risk*100),1,{"label":"direction forecast","epoch":55,"epochs":55,"model":full_up,"sample":latest})
    return NASDAQ_RESULT

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

def _csv_path():
    path=DATA_DIR/"nasdaq100_daily.csv"
    _supabase_restore_file(path,"nasdaq100_daily.csv")
    return path

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
    """Official Nasdaq-100 observations only. QQQ is a separate ETF instrument."""
    return _load_csv_series()

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
    _supabase_backup_file(path,"nasdaq100_daily.csv")

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
        # Never splice QQQ ETF returns into the Nasdaq-100 index history.
        PRICE_STATE["alpaca"]="not used for NDX history (QQQ is a separate ETF)"
        PRICE_STATE["extra"]=[]
        PRICE_STATE["last_csv_date"]=last
        PRICE_STATE["updated"]=datetime.now().isoformat(timespec="seconds")
        if dates and closes:
            record_learning_event("fred_market","official_daily_close","NASDAQ100",{"date":dates[-1],"close":closes[-1],"observations":len(dates),"provider_status":PRICE_STATE.get("fred")},observed_at=dates[-1]+"T23:59:00+00:00")
        return PRICE_STATE

def price_data_note():
    dates,_=load_price_series()
    csv_last=PRICE_STATE.get("last_csv_date") or dates[-1]
    et=eastern_now()
    note="Nasdaq-100 research series: official FRED daily closes through %s; QQQ prices are not substituted into this series"%csv_last
    if PRICE_STATE.get("updated"):
        note+="; last official-data refresh %s (%s)"%(PRICE_STATE["updated"].replace("T"," "),PRICE_STATE["fred"])
    lag=(et.date()-date.fromisoformat(dates[-1])).days
    if lag>4:
        note+=". WARNING: newest official Nasdaq-100 observation is %d days old"%lag
    return note+"."

# ---- Hourly learning: save QQQ hourly bars from Alpaca, learn each hour's typical move, and test whether hourly data improves the networks ----
INTRADAY_STATE={"status":"not started","bars":0,"first":None,"last":None,"days":0,"ab":None,"collected_at":None}
INTRADAY_LOCK=threading.Lock()

def intraday_path():
    path=DATA_DIR/"intraday_qqq_1h.csv"
    _supabase_restore_file(path,"intraday_qqq_1h.csv")
    return path

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
        _supabase_backup_file(path,"intraday_qqq_1h.csv")
    allb=intraday_load(); INTRADAY_STATE.update({"days":len(intraday_features(allb)),"bars":len(allb),"first":allb[0][0] if allb else None,"last":allb[-1][0] if allb else None,"collected_at":datetime.now().isoformat(timespec="seconds"),"status":("collected %d new hourly bars"%len(added)) if added else "up to date"})
    if token: INTRADAY_STATE["status"]+=" (history download continues next cycle)"
    if added:
        record_learning_event("alpaca_market","hourly_bars_ingested","QQQ",{"new_bars":len(added),"first":added[0][0],"last":added[-1][0],"latest_close":added[-1][4]})
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
            dates,closes=load_price_series(); INTRADAY_STATE["ab"]=intraday_ab_test(dates,closes)
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
    v=v or final_verdict("tomorrow")
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
    """Runs without anyone asking: start brain training/retraining when due, and log + grade today's call once per new official close."""
    dates,closes=load_price_series(); brain=brain_load()
    if brain is None or (len(closes)-brain.get("checked_rows",0)>=21 and not BRAIN_JOB["running"]): start_brain_job(False)
    try: intraday_autopilot()
    except Exception as exc: INTRADAY_STATE["status"]="hourly learning problem: "+str(exc)[:80]
    official=PRICE_STATE.get("last_csv_date")
    if brain is not None and official and AUTOPILOT["logged_for"]!=official:
        v=final_verdict("tomorrow"); AUTOPILOT["logged_for"]=official
        AUTOPILOT["note"]="Logged the %s call (%s) for %s."%(v.get("brain_verdict") or v.get("verdict"),v.get("confidence"),v.get("as_of"))
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
        try:
            MACRO_CACHE["series"][name]=_fred_series(sid)
            ds,vs=MACRO_CACHE["series"][name]
            if ds: record_learning_event("fred_macro","macro_observation",name.upper(),{"series":name,"fred_id":sid,"date":ds[-1],"value":vs[-1]},observed_at=ds[-1]+"T23:59:00+00:00")
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

def brain_path():
    path=DATA_DIR/"models"/"mira_risk_brain.json"
    _supabase_restore_file(path,"models/mira_risk_brain.json")
    return path

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
    validation_auc=auc_score([p_val[i] for i in val_ids],[ys[i] for i in val_ids])
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
    return {"version":2,"created":datetime.now().isoformat(timespec="seconds"),"trained_through":dates[-1],"rows_at_training":len(closes),"checked_rows":len(closes),"hidden":hidden,"features":feature_names,"horizon_days":BRAIN_HORIZON,"drop":BRAIN_DROP,"threshold":best_thr,"mean":s_mean,"scale":s_scale,"members":serving,"validation_epochs":epochs,"eval":{"mean":mean,"scale":scale,"members":members,"holdout_start":dates[hold_ids[0]],"holdout_end":dates[hold_ids[-1]]},"validation":{"rows":len(val_ids),"auc":validation_auc,"base_rate":sum(ys[i] for i in val_ids)/len(val_ids)},"holdout":holdout,"history":[]}

def brain_risk_now(brain,dates,closes):
    x=select_features(brain_feature_rows(dates,closes)[-1],brain.get("features",BRAIN_FEATURES))
    return predict(brain_ensemble(brain["members"]),[(v-brain["mean"][j])/brain["scale"][j] for j,v in enumerate(x)])

def brain_save(brain):
    path=brain_path(); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(brain),encoding="utf-8")
    _supabase_backup_file(path,"models/mira_risk_brain.json")

def brain_job(force=False):
    """Train the first brain, or retrain on newer data and promote the new version only if it holds up on unseen days."""
    try:
        dates,closes=load_price_series(); champ=brain_load()
        if champ is None:
            BRAIN_JOB["message"]="Training the first neural risk brain..."
            try: refresh_macro()
            except Exception: pass
            usable_macro=all(MACRO_CACHE.get("series",{}).get(k) and MACRO_CACHE["series"][k][0] for k in ("yield_10y","credit_spread"))
            initial_features=ALL_FEATURES if usable_macro else BRAIN_FEATURES
            brain=train_brain(dates,closes,8,feature_names=initial_features)
            brain["history"]=[{"date":dates[-1],"event":"first brain trained","features":initial_features,"holdout_auc":brain["holdout"]["auc"]}]
            brain_save(brain); BRAIN_JOB["message"]="First brain trained with %s inputs."%len(initial_features)
            record_learning_event("model_governance","initial_model_trained","NASDAQ100",{"trained_through":dates[-1],"features":initial_features,"holdout":brain.get("holdout")})
        elif not force and len(closes)-champ.get("checked_rows",0)<21:
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
                cand=train_brain(dates,closes,hidden,feature_names)
                results.append((cand,kind,tol,cand.get("validation",{}).get("auc"),cand["holdout"].get("auc")))
            # Candidate choice uses the validation block only. The chronological holdout is reserved for the final promotion gate.
            eligible=[x for x in results if x[3] is not None]
            selected=max(eligible,key=lambda x:x[3]) if eligible else None
            winner=None; win_auc=None; selected_kind=None; selected_tol=None
            if selected is not None:
                cand,selected_kind,selected_tol,val_auc,hold_auc=selected
                if hold_auc is not None and champ_auc is not None and hold_auc>=champ_auc+selected_tol:
                    winner,win_auc=cand,hold_auc
            note=[{"candidate":kind,"validation_auc":val_auc,"holdout_auc":hold_auc,"selected_by_validation":bool(selected and cand is selected[0]),"champion_holdout_auc":champ_auc,"promotion_margin":tol,"qualified":bool(selected and cand is selected[0] and winner is not None)} for cand,kind,tol,val_auc,hold_auc in results]
            event={"date":dates[-1],"event":"self-improvement check","selection_metric":"chronological validation AUC","champion_holdout_auc_same_days":champ_auc,"candidates":note,"promoted":winner is not None}
            hist=(champ.get("history") or [])+[event]
            if winner is not None:
                winner["history"]=hist[-20:]; brain_save(winner); BRAIN_JOB["message"]="New brain promoted after validation selection and holdout confirmation (AUC %.3f vs %.3f)."%(win_auc,champ_auc or 0)
            else:
                champ["history"]=hist[-20:]; champ["checked_rows"]=len(closes); brain_save(champ); BRAIN_JOB["message"]="Kept the current brain; selected challenger failed the final holdout promotion gate."
            record_learning_event("model_governance","challenger_evaluation","NASDAQ100",event)
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
    return {"trained":b is not None,"trained_through":(b or {}).get("trained_through"),"alarm_level":(b or {}).get("threshold"),"holdout":(b or {}).get("holdout"),"history":(b or {}).get("history",[])[-5:],"job":dict(BRAIN_JOB)}

def monte_carlo_drop_risk(closes,horizon=None,drop=None,n_sims=3000,seed=12345):
    """A pure-statistics estimate, with no learning and no neural network involved: draws real historical horizon-day windows at random, each with EQUAL probability of being picked, and reports the share where a drop of that size actually happened. This is 'historical bootstrap' simulation - a standard, honest statistical method, independent of anything the networks learned, used here as a second check on the same question."""
    horizon=horizon or BRAIN_HORIZON; drop=drop or BRAIN_DROP
    daily=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    starts=list(range(0,len(daily)-horizon+1))
    if len(starts)<100: return None
    rnd=random.Random(seed); hits=0
    for _ in range(n_sims):
        s0=rnd.choice(starts); cum=1.0; worst=1.0
        for r in daily[s0:s0+horizon]:
            cum*=(1+r); worst=min(worst,cum)
        if worst-1<=-drop: hits+=1
    p=hits/n_sims; se=math.sqrt(p*(1-p)/n_sims)
    return {"probability":p,"simulations":n_sims,"horizon_days":horizon,"drop_threshold":drop,"confidence_95_low":max(0.0,p-1.96*se),"confidence_95_high":min(1.0,p+1.96*se),"windows_available":len(starts),"method":"historical bootstrap: %d random real past %d-day windows, each drawn with equal probability (1/%d); no learning, no neural network involved"%(n_sims,horizon,len(starts))}

def reconcile_signals(brain_verdict, trend_verdict, horizon_days):
    """Conservative decision layer: disagreement is HOLD, never a forced BUY/SELL.

    The neural model estimates a fixed-horizon drawdown risk; the trend rule is
    an independent directional filter. Neither is treated as a calibrated
    probability of price direction. Long horizons beyond 63 trading days are
    not timed by this system and therefore return HOLD.
    """
    if brain_verdict not in ("BUY", "SELL") or trend_verdict not in ("BUY", "SELL"):
        return {"verdict":"HOLD", "agree":False, "status":"INSUFFICIENT_SIGNAL",
                "reason":"One or more signal engines did not produce a valid directional signal."}
    if brain_verdict != trend_verdict:
        return {"verdict":"HOLD", "agree":False, "status":"CONFLICT",
                "reason":"The neural risk model and trend engine disagree. No directional call is issued."}
    if horizon_days > 63:
        return {"verdict":"HOLD", "agree":True, "status":"HORIZON_UNSUPPORTED",
                "reason":"The requested horizon exceeds 63 trading days; this system has not established a reliable timing signal at that horizon."}
    return {"verdict":brain_verdict, "agree":True, "status":"AGREEMENT",
            "reason":"The neural risk model and trend engine agree."}

def final_verdict(prompt=""):
    h,label,defaulted=parse_horizon(prompt)
    try: refresh_prices(False)
    except Exception: pass
    dates,closes=load_price_series()
    brain=brain_load()
    if brain is not None and not BRAIN_JOB["running"] and len(closes)-brain.get("checked_rows",0)>=21: start_brain_job(False)   # self-improvement runs in the background
    rule,learn_note,tested=self_improve(dates,closes); n,band=rule
    pos=trend_positions(closes,n,band); trend_verdict="BUY" if pos[-1]==1 else "SELL"
    dist=closes[-1]/(sum(closes[-n:])/n)-1; d50=closes[-1]/(sum(closes[-50:])/50)-1
    start=eval_start(dates); everyday=horizon_stats(closes,[1.0]*len(closes),start,h,None)
    out={"as_of":dates[-1],"data_note":price_data_note(),"hourly_note":intraday_note(h),"horizon_days":h,"horizon_label":label,"horizon_defaulted":defaulted,"horizon_all":everyday,"trend_verdict":trend_verdict,"trend_rule":{"average_days":n,"band_pct":band*100},"distance_from_average_pct":round(dist*100,2),"distance_from_50day_pct":round(d50*100,2),"self_check":learn_note,"brain":None,"monte_carlo":monte_carlo_drop_risk(closes,h if h>=5 else BRAIN_HORIZON,BRAIN_DROP),"risk_horizon_days":BRAIN_HORIZON,"risk_horizon_note":"The neural risk score uses its trained fixed horizon; it is not automatically changed when the requested outlook horizon changes."}
    if brain is None:
        started=start_brain_job(False)
        out.update({"verdict":"HOLD","confidence":"Low","interim":True,"decision_status":"BRAIN_UNAVAILABLE","decision_reason":"The neural risk model is not trained yet. The trend rule is shown as provisional context only; no final BUY/SELL call is issued.","provisional_trend_verdict":trend_verdict,"brain_job":started["message"],"agree":False})
        return out
    p=brain_risk_now(brain,dates,closes); thr=brain["threshold"]; brain_verdict="SELL" if p>=thr else "BUY"
    margin=((p-thr)/thr) if brain_verdict=="SELL" else ((thr-p)/thr)
    level=2 if margin>=0.5 else (1 if margin>=0.2 else 0)
    decision=reconcile_signals(brain_verdict,trend_verdict,h)
    if not decision["agree"]: level=0
    cap=0 if h<=5 else (1 if h<=21 else (2 if h<=63 else 0))
    confidence=("Low","Medium","High")[min(level,cap)]
    # Keep the risk-model ledger separate from the reconciled directional call:
    # it grades the neural model's drawdown warning, not the BUY/SELL strategy.
    live=update_ledger(dates,closes,brain_verdict,confidence,p,thr)
    out.update({"verdict":decision["verdict"],"brain_verdict":brain_verdict,"confidence":confidence,"interim":False,"agree":decision["agree"],"decision_status":decision["status"],"decision_reason":decision["reason"],"risk_score":p,"alarm_level":thr,"brain":{"trained_through":brain["trained_through"],"hidden":brain["hidden"],"holdout":brain["holdout"],"holdout_start":brain["eval"]["holdout_start"],"holdout_end":brain["eval"]["holdout_end"],"history":brain.get("history",[])[-3:]},"live_record":live,"job":dict(BRAIN_JOB)})
    return out

def build_final_answer(v):
    pct=lambda x:"%.1f%%"%(x*100); p0=lambda x:"n/a" if x is None else "%.0f%%"%(x*100); sg=lambda x:("%+.1f%%" if abs(x)<0.1 else "%+.0f%%")%(x*100)
    h=v["horizon_days"]; a=v["horizon_all"]; n=v["trend_rule"]["average_days"]
    parts=["Time frame: %s.%s"%(v["horizon_label"]," You did not name one, so I used about a month; ask about tomorrow or next year for a different view." if v["horizon_defaulted"] else "")]
    if v["interim"]:
        parts.append("Decision: HOLD / NO FINAL DIRECTIONAL CALL. The neural risk model is not trained yet (%s)."%v["brain_job"])
        parts.append("The trend rule currently says %s (the index is %.1f%% %s its %d-day average). This is provisional context only, not a BUY/SELL recommendation."%(v["provisional_trend_verdict"],abs(v["distance_from_average_pct"]),"above" if v["distance_from_average_pct"]>=0 else "below",n))
        return " ".join(parts)+"\n\nSUMMARY | HOLD | Brain unavailable | Provisional trend: %s | Data as of %s"%(v["provisional_trend_verdict"],v["as_of"])
    if v["verdict"]=="BUY":
        parts.append("Model call: BUY. The neural risk model and trend engine agree.")
    elif v["verdict"]=="SELL":
        parts.append("Model call: SELL. The neural risk model and trend engine agree on elevated downside risk / defensive trend.")
    else:
        parts.append("Model call: HOLD / NO DIRECTIONAL CALL. "+v.get("decision_reason","The decision layer withheld a directional call."))
    parts.append("Confidence: %s."%v["confidence"])
    state="above" if v["risk_score"]>=v["alarm_level"] else "below"
    parts.append("Neural risk model: estimated probability of a drop of 8%% or more over its trained %d-trading-day horizon is %s; its alarm threshold is %s, and the score is %s the threshold. Its independent risk classification is %s."%(v.get("risk_horizon_days",BRAIN_HORIZON),p0(v["risk_score"]),p0(v["alarm_level"]),state,v["brain_verdict"]))
    parts.append("Trend engine: %s (the index is %.1f%% %s its %d-day average and %.1f%% %s its 50-day average)."%(v["trend_verdict"],abs(v["distance_from_average_pct"]),"above" if v["distance_from_average_pct"]>=0 else "below",n,abs(v["distance_from_50day_pct"]),"above" if v["distance_from_50day_pct"]>=0 else "below"))
    if not v["agree"]: parts.append("Reconciliation: CONFLICT. The engines disagree, so MIRA returns HOLD and sets confidence to Low instead of letting either engine override the other.")
    elif v.get("decision_status")=="HORIZON_UNSUPPORTED": parts.append("Reconciliation: the engines agree, but the requested horizon is beyond 63 trading days. MIRA returns HOLD because its timing signal is not validated that far ahead.")
    mc=v.get("monte_carlo")
    if mc: parts.append("Base-rate check (history, not a prediction): across %d random real %d-day stretches, a drop like this happened %s of the time (95%% confidence range %s to %s)."%(mc["simulations"],mc["horizon_days"],p0(mc["probability"]),p0(mc["confidence_95_low"]),p0(mc["confidence_95_high"])))
    b=v["brain"]; ho=b["holdout"]; s=ho["strategy"]; bh=ho["buy_and_hold"]
    if ho.get("auc") is not None:
        txt="How good the brain is: on days it never saw during training (%s to %s), its risk score separated stormy from calm periods with a score of %.2f%s (0.50 would be a coin flip)."%(b["holdout_start"],b["holdout_end"],ho["auc"],(" (95%% range %.2f to %.2f)"%tuple(ho["auc_ci"])) if ho.get("auc_ci") else "")
        if ho.get("flagged_drop_rate") is not None and ho.get("calm_drop_rate") is not None:
            txt+=" When it raised the alarm, a drop of 8%%+ followed %s of the time, versus %s when it did not."%(p0(ho["flagged_drop_rate"]),p0(ho["calm_drop_rate"]))
        if s and bh: txt+=" Following its calls in that test earned %s a year versus %s for buy-and-hold, with a worst drop of %.0f%% versus %.0f%%."%(pct(s["cagr"]),pct(bh["cagr"]),s["max_drawdown"]*100,bh["max_drawdown"]*100)
        txt+=" Caveat: the brain is re-checked on these same unseen days each time it retrains, so these numbers lean optimistic; the live record below is the clean test."
        parts.append(txt)
    if a:
        parts.append("For this time frame, the index has historically been higher %s of the time and typically moved %s, but in the worst 5%% of cases it fell %s or more."%(p0(a["p_up"]),sg(a["median"]),("%.1f%%" if abs(a["p5"])<0.1 else "%.0f%%")%(abs(a["p5"])*100)))
    if h<=1: parts.append("Tomorrow is close to a coin flip for any method, so a day trader should not lean on this call; the brain looks about a month ahead.")
    elif h<=5: parts.append("A week ahead is still mostly noise, so treat this as a mild lean only.")
    elif h>63: parts.append("No timing signal has been established for this horizon; MIRA therefore reports HOLD rather than extrapolating a short-horizon model.")
    r=v["live_record"]["graded"]
    if r["n"]>=1: parts.append("Live neural risk record: %d call%s graded, the risk call was right %s of the time (always saying BUY would be right %s). This grades the neural drawdown warning, not the reconciled HOLD/BUY/SELL decision."%(r["n"],"" if r["n"]==1 else "s",pct(r["hit_rate"]),pct(r["always_buy_rate"])))
    else: parts.append("Live neural risk record: %d call%s logged; each is graded after 1 day and after a month against what the market really did."%(v["live_record"]["calls_logged"],"" if v["live_record"]["calls_logged"]==1 else "s"))
    hist=b.get("history") or []
    if hist:
        last=hist[-1]
        if last.get("event")=="self-improvement check": parts.append("Self-improvement: last check on %s, %s."%(last["date"],"a retrained brain was promoted" if last.get("promoted") else "the current brain was kept because challengers did not qualify"))
        else: parts.append("Self-improvement: the brain was first trained on data through %s; it retrains on new data about every 21 trading days and only replaces itself if it holds up on days it has not seen."%b["trained_through"])
    if v["job"].get("running"): parts.append("(A background retraining job is running now.)")
    if v.get("hourly_note"): parts.append(v["hourly_note"])
    if v.get("data_note"): parts.append(v["data_note"])
    parts.append("This is a research tool, not financial advice.")
    line="SUMMARY | Time frame: %s | Decision: %s | Confidence: %s | Risk score %s vs alarm %s | Neural risk: %s | Trend: %s | Brain test score %s | Data as of %s"%(v["horizon_label"],v["verdict"],v["confidence"],p0(v["risk_score"]),p0(v["alarm_level"]),v["brain_verdict"],v["trend_verdict"],("%.2f"%ho["auc"]) if ho.get("auc") is not None else "n/a",v["as_of"])
    return " ".join(parts)+"\n\n"+line

def answer_decision(prompt):
    visible_status("tool_call","MIRA chat: neural risk brain is scoring today's market and grading past calls.")
    v=final_verdict(prompt)
    log_question(prompt,v["verdict"])
    answer=build_final_answer(v)
    visible_status("observation","FINAL %s (%s confidence) as of %s"%(v["verdict"],v["confidence"],v["as_of"]))
    visible_status("complete","Answer ready: FINAL "+v["verdict"],1)
    return {"answer":answer,"summary":v,"actions":[{"tool":"neural_risk_brain","observation":{k:v.get(k) for k in ("verdict","confidence","horizon_label","as_of","risk_score","alarm_level","trend_verdict","interim")}}],"model":"MIRA neural risk brain (self-improving)"}

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
            parts.append("On %s days the model had never seen, its BUY/SELL calls were right %s of the time. Simply saying BUY every day would have been right %s, so it does not beat that yet. Its risk ranking scores %.2f (0.50 is a coin flip). It warned before %s of the real 8%%+ drops, and %s of its alarms were followed by a drop (a drop happens %s of the time anyway)."%(sb["days"],pc(sb["call_accuracy"]),pc(sb["always_buy_accuracy"]),sb["auc"] or 0,pc(sb["drops_caught"]),pc(sb["alarm_precision"]),pc(sb["drop_base_rate"])))
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
        tools.append("monte_carlo"); dates,closes=load_price_series(); mc=monte_carlo_drop_risk(closes)
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
        if v.get("interim"): parts.append("The brain is still training; the trend rule says %s for now."%v["trend_verdict"])
        else: parts.append("The networks put the chance of a drop of 8%% or more within about a month at %s. Their alarm level is %s (SELL at or above it), so the call is %s. Drops of that size happened %s of the time historically."%(pc(v["risk_score"]),pc(v["alarm_level"]),v["verdict"],pc(sb.get("drop_base_rate"))))
    elif has("how","work","what are you","what is","explain","neural","network","model","why","who"):
        parts.append("I'm MIRA, a market research assistant. A small ensemble of neural networks, written in plain Python, reads 13 price-based signals from about 40 years (1986 onward) of Nasdaq-100 and index history and estimates the chance of a drop of 8% or more in the next month. At or above its alarm level the call is SELL, otherwise BUY. A simple trend rule gives a second opinion. Each call is logged and graded later, and the networks retrain themselves on new data and only replace themselves if they prove better on unseen days.")
        parts.append("I can't predict tomorrow's move reliably - nobody can - and I'm not financial advice.")
    else:
        try:
            v=final_verdict("next month")
            if not v.get("interim"): parts.append("Here is the model's current call: %s for the next month (%s confidence), drop risk %s against an alarm level of %s."%(v["verdict"],v["confidence"],pc(v["risk_score"]),pc(v["alarm_level"])))
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
            lines.append("Unseen-data test (%s days): BUY/SELL calls right %.1f%%; always-BUY would be right %.1f%%; risk-ranking AUC %.2f; caught %.0f%% of real 8%%+ drops; %.0f%% of alarms followed by a drop (base rate %.0f%%)."%(sb["days"],100*sb["call_accuracy"],100*sb["always_buy_accuracy"],sb["auc"] or 0,100*(sb["drops_caught"] or 0),100*sb["alarm_precision"],100*sb["drop_base_rate"]))
            if sb.get("strategy"): lines.append("Following its calls: %.1f%%/yr, worst fall %.0f%%; buy-and-hold: %.1f%%/yr, worst fall %.0f%%."%(100*sb["strategy"]["cagr"],100*sb["strategy"]["max_drawdown"],100*sb["buy_and_hold"]["cagr"],100*sb["buy_and_hold"]["max_drawdown"]))
        lines.append("Brain trained through %s."%brain.get("trained_through"))
        lines.append("Inputs used: %s."%("13 price-based features only" if brain.get("features",BRAIN_FEATURES)==BRAIN_FEATURES else "13 price-based features plus 3 macro features (10-year Treasury yield change, high-yield credit spread, recession flag)"))
    try:
        v=final_verdict("next month")
        if not v.get("interim"): lines.append("Current call (next month): %s, confidence %s, drop risk %.1f%% vs alarm %.1f%%, trend rule %s, data as of %s."%(v["verdict"],v["confidence"],100*v["risk_score"],100*v["alarm_level"],v["trend_verdict"],v["as_of"]))
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
        out.append({"d":dates[i],"v":v,"p":round(probs[i],4),"o":None if y is None else int((v=="SELL")==bool(y))})
    return out

def chart_scoreboard(brain):
    ho=brain.get("holdout") or {}; br=ho.get("base_rate"); fs=ho.get("flagged_share"); fd=ho.get("flagged_drop_rate"); cd=ho.get("calm_drop_rate")
    if None in (br,fs,fd,cd): return {"available":False}
    tp=fs*fd; tn=(1-fs)*(1-cd)
    return {"available":True,"days":ho.get("rows"),"auc":ho.get("auc"),"auc_ci":ho.get("auc_ci"),"call_accuracy":tp+tn,"always_buy_accuracy":1-br,"drops_caught":(tp/br) if br else None,"alarm_precision":fd,"drop_base_rate":br,"strategy":ho.get("strategy"),"buy_and_hold":ho.get("buy_and_hold"),"alarm_level":brain.get("threshold"),"trained_through":brain.get("trained_through")}

def chart_live_record():
    try: led=json.loads((LEARN_DIR/"ledger.json").read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): led=[]
    m=[e for e in led if "dropped_8pct_21d" in e]; d=[e for e in led if "next_day_up" in e]
    return {"calls":len(led),"month_graded":len(m),"month_right":sum(int((e["verdict"]=="SELL")==e["dropped_8pct_21d"]) for e in m),"month_always_buy_right":sum(int(not e["dropped_8pct_21d"]) for e in m),"day_graded":len(d),"day_right":sum(int((e["verdict"]=="BUY")==e["next_day_up"]) for e in d),"day_always_buy_right":sum(int(e["next_day_up"]) for e in d)}

def refresh_prices_background():
    """Same job as refresh_prices(), but for callers that must respond fast (a live page waiting on this request) and can't afford to block on a slow/unreachable FRED server (up to ~75s of retries). Kicks the real refresh off on its own thread if one isn't already running, and returns immediately either way - the page just shows whatever data is already on disk from the last successful refresh, instead of hanging."""
    if PRICE_LOCK.acquire(blocking=False):
        PRICE_LOCK.release()
        threading.Thread(target=refresh_prices,args=(True,),daemon=True).start()
    # else: a refresh is already in flight (this request or the background loop) - don't pile on, just use what's on disk right now.

def _fit_garch11(returns):
    """Grid-MLE for a constrained Gaussian GARCH(1,1); returns None if fit is unstable."""
    n=len(returns); mu=sum(returns)/n; centered=[x-mu for x in returns]
    var=sum(x*x for x in centered)/max(1,n-1)
    best=None
    for alpha_i in range(2,21,2):
        alpha=alpha_i/100
        for beta_i in range(50,96,3):
            beta=beta_i/100
            if alpha+beta>=.995: continue
            omega=max(1e-12,(1-alpha-beta)*var); h=max(var,1e-12); ll=0.0
            for e in centered:
                h=max(1e-12,omega+alpha*e*e+beta*h)
                ll+=-.5*(math.log(2*math.pi)+math.log(h)+(e*e/h))
            if best is None or ll>best[0]: best=(ll,omega,alpha,beta,h)
    if best is None: return None
    ll,omega,alpha,beta,h=best
    return {"next_volatility_pct":math.sqrt(h)*100,"persistence":alpha+beta,"alpha":alpha,"beta":beta,"omega":omega,"log_likelihood":ll,"fit":"Constrained Gaussian maximum-likelihood grid fit; in-sample diagnostic, not a forecast guarantee"}


def _fit_two_state_vol_hmm(returns, iterations=35):
    """Two-state zero-mean Gaussian volatility HMM with Baum-Welch EM and filtered state probability."""
    n=len(returns)
    if n<80: return {"high_volatility_probability_next":None,"fit_rows":0,"status":"Not fitted: at least 80 returns required."}
    v=sum(x*x for x in returns)/n
    if v<=1e-12: return {"high_volatility_probability_next":None,"fit_rows":n,"status":"Not fitted: near-zero return variance."}
    trans=[[.97,.03],[.08,.92]]; variances=[max(v*.45,1e-10),max(v*2.2,1e-10)]; prior=[.8,.2]
    def emission(x, vv): return max(1e-300,math.exp(max(-700,-.5*(math.log(2*math.pi*vv)+x*x/vv))))
    for _ in range(iterations):
        emit=[[emission(x,variances[j]) for j in range(2)] for x in returns]
        f=[[0.0,0.0] for _ in range(n)]; scales=[0.0]*n
        for j in range(2): f[0][j]=prior[j]*emit[0][j]
        scales[0]=sum(f[0]) or 1e-300; f[0]=[x/scales[0] for x in f[0]]
        for t in range(1,n):
            for j in range(2): f[t][j]=emit[t][j]*sum(f[t-1][i]*trans[i][j] for i in range(2))
            scales[t]=sum(f[t]) or 1e-300; f[t]=[x/scales[t] for x in f[t]]
        b=[[1.0,1.0] for _ in range(n)]
        for t in range(n-2,-1,-1):
            for i in range(2): b[t][i]=sum(trans[i][j]*emit[t+1][j]*b[t+1][j] for j in range(2))/max(scales[t+1],1e-300)
        gamma=[]
        for t in range(n):
            z=[f[t][j]*b[t][j] for j in range(2)]; sm=sum(z) or 1e-300; gamma.append([x/sm for x in z])
        xi=[[0.0,0.0],[0.0,0.0]]
        for t in range(n-1):
            z=[[f[t][i]*trans[i][j]*emit[t+1][j]*b[t+1][j] for j in range(2)] for i in range(2)]
            sm=sum(sum(row) for row in z) or 1e-300
            for i in range(2):
                for j in range(2): xi[i][j]+=z[i][j]/sm
        prior=gamma[0][:]
        for i in range(2):
            den=sum(xi[i])
            if den>1e-9: trans[i]=[max(.001,xi[i][j]/den) for j in range(2)]
            den=sum(gamma[t][i] for t in range(n))
            if den>1e-9: variances[i]=max(1e-10,sum(gamma[t][i]*returns[t]**2 for t in range(n))/den)
        # Keep state labels stable: state 1 is always the higher-volatility state.
        if variances[0]>variances[1]:
            variances.reverse(); prior.reverse(); trans=[[trans[1][1],trans[1][0]],[trans[0][1],trans[0][0]]]
    # Filter one-step probability from the final filtered state and transition matrix.
    prob_high=sum(f[-1][i]*trans[i][1] for i in range(2))
    return {"high_volatility_probability_next":max(0.0,min(1.0,prob_high)),"fit_rows":n,"state_variances_pct2":[variances[0]*10000,variances[1]*10000],"transition_matrix":trans,"status":"Fitted two-state Gaussian volatility HMM by Baum-Welch EM; in-sample regime estimate."}


def _chart_technical_geometry(dates, closes, candles=None):
    """Descriptive price geometry and smoothing; projections are conditional reference levels, not forecasts."""
    n=len(closes)
    if n<20:
        return {"available":False,"reason":"At least 20 valid closes are required for technical geometry."}
    clean=[float(x) for x in closes if x is not None and math.isfinite(float(x)) and float(x)>0]
    if len(clean)!=n or len(clean)<20:
        return {"available":False,"reason":"Price series contains missing or invalid closes."}
    def ema(values, span):
        alpha=2/(span+1); out=[]; value=values[0]
        for x in values:
            value=alpha*x+(1-alpha)*value; out.append(value)
        return out
    def regression(values, window):
        z=values[-min(window,len(values)):]; m=len(z)
        if m<10:return {"observations":m,"slope_pct_per_20_sessions":None,"r_squared":None,"direction":"insufficient data"}
        y=[math.log(x) for x in z]; xm=(m-1)/2; ym=sum(y)/m
        den=sum((i-xm)**2 for i in range(m))
        slope=sum((i-xm)*(y[i]-ym) for i in range(m))/den if den else 0.0
        intercept=ym-slope*xm; ss_tot=sum((v-ym)**2 for v in y); ss_res=sum((y[i]-(intercept+slope*i))**2 for i in range(m))
        r2=max(0.0,min(1.0,1-ss_res/ss_tot)) if ss_tot>1e-15 else 0.0
        return {"observations":m,"slope_pct_per_20_sessions":(math.exp(slope*20)-1)*100,"r_squared":r2,"direction":"upward" if slope>1e-8 else ("downward" if slope< -1e-8 else "flat")}
    # Prefer real OHLC extrema; otherwise disclose that the geometry is close-only.
    candle_map={str(c.get("d")):c for c in (candles or []) if isinstance(c,dict) and c.get("d")}
    highs=[]; lows=[]; trs=[]; ha=[]
    prev_close=None; ha_open=None; ha_close_prev=None
    for i,(d,c) in enumerate(zip(dates,clean)):
        bar=candle_map.get(str(d))
        if bar:
            try:
                o,h,l,cl=[float(bar[k]) for k in ("o","h","l","c")]
                if not (o>0 and l>0 and h>=max(o,cl) and l<=min(o,cl)): raise ValueError()
                highs.append(h); lows.append(l); tr=max(h-l,abs(h-prev_close) if prev_close else h-l,abs(l-prev_close) if prev_close else h-l)
                hc=(o+h+l+cl)/4; ho=(o+cl)/2 if ha_open is None else (ha_open+ha_close_prev)/2
                ha.append({"open":ho,"close":hc,"high":max(h,ho,hc),"low":min(l,ho,hc)})
                ha_open,ha_close_prev=ho,hc
            except (KeyError,TypeError,ValueError):
                highs.append(c);lows.append(c);tr=abs(c-prev_close) if prev_close else 0
        else:
            highs.append(c);lows.append(c);tr=abs(c-prev_close) if prev_close else 0
        trs.append(tr);prev_close=c
    def window_geometry(w):
        k=min(w,n); hh=max(highs[-k:]); ll=min(lows[-k:]); span=max(0.0,hh-ll); last=clean[-1]
        ratios=(0.236,0.382,0.5,0.618,0.786)
        return {"window_sessions":k,"swing_high":hh,"swing_low":ll,"range_pct_of_last":span/last*100,
          "fib_retracements_from_high":{str(r):hh-span*r for r in ratios},
          "fib_extensions_from_low":{str(r):ll+span*r for r in (1.0,1.272,1.618)},
          "measured_move_up_if_breakout":hh+span,"measured_move_down_if_breakdown":ll-span,
          "interpretation":"Conditional geometric levels from the observed range; not support/resistance guarantees or price targets."}
    windows={str(w):window_geometry(w) for w in (20,60,120) if n>=w}
    ema20=ema(clean,20)[-1]; ema50=ema(clean,50)[-1] if n>=50 else None
    sma20=sum(clean[-20:])/20; sma50=sum(clean[-50:])/50 if n>=50 else None
    atr14=sum(trs[-14:])/min(14,len(trs))
    rets=[math.log(clean[i]/clean[i-1]) for i in range(1,n)]
    ewma_var=None
    if rets:
        ewma_var=sum(x*x for x in rets[:min(20,len(rets))])/min(20,len(rets))
        for r in rets[min(20,len(rets)):]: ewma_var=.94*ewma_var+.06*r*r
    ha_summary=None
    if len(ha)>=3:
        tail=ha[-3:];ha_summary={"last_open":tail[-1]["open"],"last_close":tail[-1]["close"],"last_three_bullish":all(x["close"]>=x["open"] for x in tail),"method":"Heikin-Ashi transform computed from provider OHLC bars; visual smoothing, not independent market observations."}
    # Confirmed local pivots (three bars on either side) provide reproducible swing geometry.
    pivots=[]
    for i in range(max(3,n-180),n-3):
        left=max(0,i-3); right=min(n,i+4); hs=highs[left:right]; ls=lows[left:right]
        is_high=highs[i]==max(hs) and highs[i]>max(hs[:i-left]+hs[i-left+1:])
        is_low=lows[i]==min(ls) and lows[i]<min(ls[:i-left]+ls[i-left+1:])
        if is_high and is_low:
            is_high=(clean[i]-lows[i])>=(highs[i]-clean[i]); is_low=not is_high
        if not is_high and not is_low: continue
        point={"index":i,"date":str(dates[i]),"kind":"H" if is_high else "L","price":highs[i] if is_high else lows[i]}
        if pivots and pivots[-1]["kind"]==point["kind"]:
            more_extreme=point["price"]>pivots[-1]["price"] if is_high else point["price"]<pivots[-1]["price"]
            if more_extreme:pivots[-1]=point
        else: pivots.append(point)
    recent_pivots=pivots[-8:]
    harmonic=[]
    if len(recent_pivots)>=5:
        pat=recent_pivots[-5:]; kinds="".join(p["kind"] for p in pat)
        if kinds in ("LHLHL","HLHLH"):
            X,A,B,C,D=[p["price"] for p in pat]
            xa,ab,bc,cd,ad=abs(A-X),abs(B-A),abs(C-B),abs(D-C),abs(D-A)
            if min(xa,ab,bc,cd)>0:
                r_ab,r_bc,r_cd,r_ad=ab/xa,bc/ab,cd/bc,ad/xa
                common=.382<=r_bc<=.886
                templates=(
                    ("Gartley",abs(r_ab-.618)<=.08,1.272<=r_cd<=1.618,abs(r_ad-.786)<=.09),
                    ("Bat",.382<=r_ab<=.50,1.618<=r_cd<=2.618,abs(r_ad-.886)<=.09),
                    ("Butterfly",abs(r_ab-.786)<=.10,1.618<=r_cd<=2.618,1.27<=r_ad<=1.618),
                )
                for name,ab_ok,cd_ok,ad_ok in templates:
                    if ab_ok and common and cd_ok and ad_ok:
                        harmonic.append({"pattern":name,"direction":"bullish" if pat[-1]["kind"]=="L" else "bearish","pivots":[{k:v for k,v in p.items() if k!="index"} for p in pat],"ratios":{"AB_XA":r_ab,"BC_AB":r_bc,"CD_BC":r_cd,"AD_XA":r_ad},"status":"ratio match only; wait for independent price confirmation"})
    fib_swing=None
    if len(recent_pivots)>=2:
        p0,p1=recent_pivots[-2:]; span=abs(p1["price"]-p0["price"])
        if span>0:
            ratios=(.236,.382,.5,.618,.786)
            levels={str(r):(p1["price"]-span*r if p1["kind"]=="H" else p1["price"]+span*r) for r in ratios}
            extensions={str(r):(p1["price"]+span*(r-1) if p1["kind"]=="H" else p1["price"]-span*(r-1)) for r in (1.272,1.618)}
            fib_swing={"direction":"up" if p1["kind"]=="H" else "down","start":p0["price"],"end":p1["price"],"retracements":levels,"extensions":extensions,"basis":"last confirmed local swing; levels are reference zones, not predictions"}
    # Classic floor pivots use the prior completed provider candle, never a partial current bar.
    valid_bars=[]
    for d in dates:
        bar=candle_map.get(str(d))
        if bar:
            try:
                o,h,l,cl=[float(bar[k]) for k in ("o","h","l","c")]
                if o>0 and l>0 and h>=max(o,cl) and l<=min(o,cl): valid_bars.append((h,l,cl))
            except (KeyError,TypeError,ValueError): pass
    floor_pivots=None
    if len(valid_bars)>=2:
        ph,pl,pc=valid_bars[-2]; pp=(ph+pl+pc)/3
        floor_pivots={"based_on":"prior completed OHLC candle","P":pp,"R1":2*pp-pl,"S1":2*pp-ph,"R2":pp+(ph-pl),"S2":pp-(ph-pl)}
    def midpoint(high_values,low_values,period):
        if len(high_values)<period:return None
        return (max(high_values[-period:])+min(low_values[-period:]))/2
    tenkan=midpoint(highs,lows,9); kijun=midpoint(highs,lows,26); span_b=midpoint(highs,lows,52)
    span_a=(tenkan+kijun)/2 if tenkan is not None and kijun is not None else None
    cloud=None
    if span_a is not None and span_b is not None:
        cloud={"span_a":span_a,"span_b":span_b,"top":max(span_a,span_b),"bottom":min(span_a,span_b),"price_vs_cloud":"above" if clean[-1]>max(span_a,span_b) else ("below" if clean[-1]<min(span_a,span_b) else "inside")}
    # McGinley Dynamic adapts its effective speed to the price-to-average ratio.
    mcginley=clean[0]; mcginley_n=14
    for price in clean[1:]:
        ratio=max(.01,price/mcginley)
        mcginley += (price-mcginley)/(0.6*mcginley_n*(ratio**4))
    # Point-and-figure summary from closes, with an ATR-sized box and a three-box reversal.
    box=max(atr14,clean[-1]*.001); pnf=[]; direction=0; anchor=clean[0]
    for price in clean[1:]:
        if direction==0:
            if price>=anchor+box: direction=1; pnf.append({"direction":"up","low":anchor,"high":price})
            elif price<=anchor-box: direction=-1; pnf.append({"direction":"down","high":anchor,"low":price})
        elif direction>0:
            col=pnf[-1]
            if price>=col["high"]+box: col["high"]=price
            elif price<=col["high"]-3*box: direction=-1; pnf.append({"direction":"down","high":col["high"],"low":price})
        else:
            col=pnf[-1]
            if price<=col["low"]-box: col["low"]=price
            elif price>=col["low"]+3*box: direction=1; pnf.append({"direction":"up","low":col["low"],"high":price})
    pnf_summary={"available":bool(pnf),"box_size":box,"reversal_boxes":3,"columns":len(pnf),"last_columns":pnf[-12:],"source":"close series; ATR-sized boxes; time is removed from the x-axis"}
    reg20=regression(clean,20); reg60=regression(clean,60)
    votes=[reg20.get("direction")=="upward",reg60.get("direction")=="upward",clean[-1]>ema20,clean[-1]>mcginley]
    if cloud is not None:votes.append(cloud["price_vs_cloud"]=="above")
    if ha_summary is not None:votes.append(ha_summary["last_close"]>=ha_summary["last_open"])
    up_votes=sum(bool(v) for v in votes); score=up_votes/len(votes) if votes else None
    trend_state="bullish confluence" if score is not None and score>=2/3 else ("bearish confluence" if score is not None and score<=1/3 else "mixed")
    return {"available":True,"last_close":clean[-1],"ohlc_observations":len(candle_map),"geometry_source":"provider OHLC" if len(candle_map)>=min(20,n) else "close-only proxy; intraday extrema unavailable",
      "ranges":windows,"confirmed_pivots":[{k:v for k,v in p.items() if k!="index"} for p in recent_pivots],"harmonic_patterns":harmonic,"fibonacci_last_swing":fib_swing,"floor_pivots":floor_pivots,
      "ichimoku":{"tenkan_9":tenkan,"kijun_26":kijun,"senkou_a_now":span_a,"senkou_b_now":span_b,"cloud":cloud,"method":"Rolling high/low midpoints; spans are current values (not forward-shifted on this chart)."},
      "smoothed_transforms":{"ema20":ema20,"ema50":ema50,"sma20":sma20,"sma50":sma50,"mcginley_dynamic_14":mcginley,"price_vs_ema20_pct":(clean[-1]/ema20-1)*100,"price_vs_sma20_pct":(clean[-1]/sma20-1)*100,"price_vs_mcginley_pct":(clean[-1]/mcginley-1)*100,"heikin_ashi":ha_summary},
      "volatility":{"atr14_absolute":atr14,"atr14_pct":atr14/clean[-1]*100,"ewma_20d_annualized_pct":math.sqrt(max(0,ewma_var or 0)*252)*100 if ewma_var is not None else None,"ewma_lambda":0.94},
      "regression":{"20":reg20,"60":reg60,"120":regression(clean,120)},"trend_confluence":{"classification":trend_state,"bullish_components":up_votes,"components":len(votes),"score_pct":score*100 if score is not None else None,"interpretation":"Directional component agreement, not a calibrated probability."},
      "point_and_figure":pnf_summary,
      "ratio_projection":{"ratios":[0.236,0.382,0.5,0.618,0.786,1.272,1.618],"basis":"Recent rolling swing high/low; retracement and extension levels are descriptive reference geometry."}}


def _arima_return_diagnostics(returns):
    """One-step ARIMA(1,1,0) log-price approximation, fit in return space; uncalibrated in-sample diagnostic."""
    sample=[float(x) for x in returns[-252:] if math.isfinite(float(x))]
    if len(sample)<30:return {"available":False,"reason":"At least 30 daily returns are required for an ARIMA(1,1,0) diagnostic."}
    mu=sum(sample)/len(sample); prev=sample[:-1]; nxt=sample[1:]
    pm=sum(prev)/len(prev); nm=sum(nxt)/len(nxt); den=sum((x-pm)**2 for x in prev)
    phi=sum((x-pm)*(y-nm) for x,y in zip(prev,nxt))/den if den>1e-15 else 0.0
    phi=max(-.98,min(.98,phi)); intercept=mu*(1-phi)
    residuals=[y-(intercept+phi*x) for x,y in zip(prev,nxt)]
    sigma=math.sqrt(sum(x*x for x in residuals)/max(1,len(residuals)-2)); sigma=max(sigma,1e-8)
    forecast=intercept+phi*sample[-1]; z=forecast/sigma
    probability=.5*(1+math.erf(z/math.sqrt(2)))
    return {"available":True,"model":"ARIMA(1,1,0) on log price, estimated as AR(1) on daily log returns","phi":phi,"next_return_pct":forecast*100,"residual_volatility_pct":sigma*100,"next_session_positive_probability":probability,"trend_probability_up":probability,"forecast_interval_95_pct":[(forecast-1.96*sigma)*100,(forecast+1.96*sigma)*100],"calibrated":False,"interpretation":"Gaussian one-step model probability; requires rolling out-of-sample calibration before use as a trading probability."}


def chart_quantitative_intelligence(dates, closes, candles=None):
    """Causal statistics plus price geometry; no diagnostic is silently converted into a trade signal."""
    technical=_chart_technical_geometry(dates,closes,candles)
    returns=[]
    for a,b in zip(closes[:-1],closes[1:]):
        if a>0 and b>0:
            r=math.log(b/a)
            if math.isfinite(r): returns.append(r)
    arima=_arima_return_diagnostics(returns)
    if arima.get("available") and closes:
        last=float(closes[-1]); lo,hi=arima["forecast_interval_95_pct"]
        arima["next_session_price_range_95"]=[last*math.exp(lo/100),last*math.exp(hi/100)]
    if len(returns)<80:
        return {"available":False,"reason":"At least 80 valid daily log returns are required for fitted Bayesian/regime diagnostics; price geometry may still be available.","as_of_date":dates[-1] if dates else None,"as_of_returns":len(returns),"bayesian_drift":{},"kalman":{},"garch":{},"hmm":{},"arima":arima,"derivative_features":{},"optimization":{"solver":"Not run: insufficient observations","curvature":None},"price_action_geometry":technical}
    n=len(returns); mu=sum(returns)/n
    # Conjugate Normal-Normal posterior for the historical mean; not a next-day direction probability.
    obs_var=sum((x-mu)**2 for x in returns)/(n-1); obs_var=max(obs_var,1e-12)
    prior_mean=0.0; prior_var=.005**2
    post_var=1/(1/prior_var+n/obs_var); post_mean=post_var*(prior_mean/prior_var+n*mu/obs_var)
    z=post_mean/math.sqrt(post_var); p_positive=.5*(1+math.erf(z/math.sqrt(2)))
    # Scalar local-level Kalman filter with disclosed fixed process-noise ratio.
    q=max(obs_var*.01,1e-12); state=prior_mean; state_var=prior_var
    for obs in returns:
        state_var+=q; gain=state_var/(state_var+obs_var); state+=gain*(obs-state); state_var=(1-gain)*state_var
    garch=_fit_garch11(returns); hmm=_fit_two_state_vol_hmm(returns)
    first=returns[-1]-returns[-2]; second=returns[-1]-2*returns[-2]+returns[-3]
    return {"available":True,"as_of_date":dates[-1],"as_of_returns":n,
      "bayesian_drift":{"probability_positive":p_positive,"posterior_mean_pct":post_mean*100,"posterior_sd_pct":math.sqrt(post_var)*100,"prior":"Normal(0, 0.5% per session)","interpretation":"Posterior sign probability for mean historical daily log return; not probability of next-session price increase."},
      "kalman":{"trend_pct_per_session":state*100,"state_variance":state_var,"process_variance":q,"observation_variance":obs_var,"method":"Scalar local-level filter; process variance fixed at 1% of sample observation variance."},
      "garch":garch or {"next_volatility_pct":None,"fit":"Unavailable: constrained likelihood fit failed"},
      "hmm":hmm,"arima":arima,
      "derivative_features":{"first_difference_daily_log_return":first,"second_difference_change_in_daily_log_return":second},
      "optimization":{"solver":"No directional optimizer: no validated portfolio objective or constraints were supplied","curvature":None},
      "price_action_geometry":technical}

def chart_fast_verdict(dates, closes, brain):
    """Separate downside-risk alarm from trend direction; never relabel risk as BUY/SELL."""
    h,label,defaulted=parse_horizon("next month")
    rule=DEFAULT_RULE
    try:
        champion=json.loads((LEARN_DIR/"champion.json").read_text(encoding="utf-8"))
        n0,b0=int(champion.get("n",DEFAULT_RULE[0])),float(champion.get("band",DEFAULT_RULE[1]))
        if n0>=2 and 0<=b0<=0.10: rule=(n0,b0)
    except (OSError,ValueError,TypeError,json.JSONDecodeError): pass
    n,band=rule; positions=trend_positions(closes,n,band)
    trend="BUY" if positions and positions[-1]==1 else ("SELL" if positions and positions[-1]==0 else None)
    base={"as_of":dates[-1],"horizon_days":h,"horizon_label":label,
        "horizon_defaulted":defaulted,"trend_verdict":trend,
        "trend_rule":{"average_days":n,"band_pct":band*100},
        "brain_job":dict(BRAIN_JOB),"risk_horizon_days":BRAIN_HORIZON,
        "fusion":{"available":False,"reason":"No validated directional probability ensemble is present in this backend."}}
    if brain is None:
        base.update({"verdict":"HOLD","confidence":"Low","interim":True,
            "decision_status":"BRAIN_UNAVAILABLE",
            "decision_reason":"The downside-risk model is unavailable; direction will not be inferred from a missing risk score.",
            "risk_score":None,"alarm_level":None,"risk_alarm_active":None,"agree":False})
        return base
    try:
        risk=float(brain_risk_now(brain,dates,closes)); threshold=float(brain["threshold"])
        alarm=risk>=threshold
        if h>63:
            verdict="HOLD"; status="HORIZON_UNSUPPORTED"
            reason="The requested horizon exceeds 63 sessions; no validated timing rule is available."
        elif alarm:
            verdict="HOLD"; status="DOWNSIDE_RISK_ALARM"
            reason="The 21-session downside-risk model triggered its 8% drawdown alarm. This is a risk warning, not a SELL prediction."
        elif trend is None:
            verdict="HOLD"; status="INSUFFICIENT_TREND_HISTORY"
            reason="The trend rule does not yet have enough history for a directional signal."
        else:
            verdict=trend; status="TREND_ONLY_NO_RISK_ALARM"
            reason="No downside-risk alarm is active. Direction comes only from the Nasdaq-100 trend rule; no calibrated probability of a rise is available."
        base.update({"verdict":verdict,"confidence":"Low","interim":False,
            "risk_score":risk,"alarm_level":threshold,"risk_alarm_active":alarm,
            "brain_verdict":"RISK_ALARM" if alarm else "NO_RISK_ALARM",
            "agree":None,"decision_status":status,"decision_reason":reason})
    except Exception as exc:
        base.update({"verdict":"HOLD","confidence":"Low","interim":True,
            "decision_status":"INFERENCE_ERROR",
            "decision_reason":"Live risk inference unavailable: "+str(exc)[:100],
            "risk_score":None,"alarm_level":None,"risk_alarm_active":None,"agree":False})
    return base

def refresh_market_snapshot_background():
    """Refresh Alpaca quote without making the chart HTTP request wait on the provider."""
    if not MARKET_REFRESH_LOCK.acquire(blocking=False):
        return
    def _worker():
        try:
            fetch_market_snapshot(force=True)
        finally:
            MARKET_REFRESH_LOCK.release()
    threading.Thread(target=_worker, daemon=True, name="mira-alpaca-snapshot").start()

def chart_payload(days=180):
    days=min((30,90,180,365,500),key=lambda k:abs(k-int(days)))
    now=time.time()
    if not (CHART_CACHE["data"] and CHART_CACHE["days"]==days and now-CHART_CACHE["at"]<30):
        refresh_prices_background()
        ndx_dates,ndx_closes=load_price_series()
        brain=brain_load()
        qqq_daily=chart_daily_candles()
        qqq_dates=sorted(qqq_daily)[-days:]
        qqq_candles=[{"d":d,"o":round(qqq_daily[d][0],4),"h":round(qqq_daily[d][1],4),
            "l":round(qqq_daily[d][2],4),"c":round(qqq_daily[d][3],4)} for d in qqq_dates]
        if brain is not None and brain.get("eval"):
            research_replay=chart_replay(ndx_dates,ndx_closes,brain,days)
            scoreboard=chart_scoreboard(brain)
        else:
            research_replay=[]; scoreboard={"available":False}
        try: verdict=chart_fast_verdict(ndx_dates,ndx_closes,brain)
        except Exception as exc: verdict={"error":str(exc)[:120]}
        if brain is not None and not verdict.get("error"):
            try: _record_ndx_prediction(ndx_dates,ndx_closes,verdict)
            except Exception: pass
        data={"as_of":ndx_dates[-1],"days":days,
            "dates":qqq_dates,"closes":[c["c"] for c in qqq_candles],"candles":qqq_candles,
            "chart_instrument":"QQQ","chart_source":"Alpaca IEX hourly bars aggregated into daily OHLC",
            "research_instrument":"NASDAQ100","research_dates":ndx_dates[-days:],
            "research_closes":[round(c,2) for c in ndx_closes[-days:]],
            "research_replay":research_replay,"replay":[],
            "scoreboard":scoreboard,"verdict":verdict,
            "quantitative_intelligence":chart_quantitative_intelligence(ndx_dates,ndx_closes),
            "data_note":price_data_note(),"live_record":chart_live_record()}
        CHART_CACHE.update({"at":now,"days":days,"data":data})
    out=dict(CHART_CACHE["data"])
    cached=MARKET_CACHE.get("data")
    out["live"]=cached if cached is not None else {
        "connected":False,"configured":bool(MARKET_API_KEY and MARKET_API_SECRET),
        "provider":"Alpaca Basic","symbol":"QQQ",
        "error":"Waiting for the first background market-data snapshot."}
    if cached is None or time.time()-MARKET_CACHE.get("at",0.0)>3:
        refresh_market_snapshot_background()
    minute=MINUTE_CACHE.get("bars") or []
    if time.time()-MINUTE_CACHE.get("at",0.0)>15:
        refresh_minute_bars_background()
    out["intraday_candles"]=minute
    out["intraday_status"]={"status":MINUTE_CACHE.get("status"),"bars":len(minute),
        "source":"Alpaca IEX 1-minute QQQ bars","error":MINUTE_CACHE.get("error"),
        "market_session":market_session().replace("_", " "),
        "credentials_configured":bool(MARKET_API_KEY and MARKET_API_SECRET)}
    return out


# Multi-asset market data. The existing neural model is trained only on NASDAQ100;
# other symbols receive market data and statistical diagnostics, never borrowed predictions.
ASSET_UNIVERSE_CACHE={"at":0.0,"equities":[],"error":None}
ASSET_CHART_CACHE={}
ASSET_INDEX_SERIES={"NASDAQ100":"NASDAQ100","SP500":"SP500","DJIA":"DJIA"}

def asset_universe_payload():
    now=time.time()
    indices=[
        {"symbol":"NASDAQ100","name":"Nasdaq-100","type":"index","source":"FRED","live":False},
        {"symbol":"SP500","name":"S&P 500","type":"index","source":"FRED","live":False},
        {"symbol":"DJIA","name":"Dow Jones Industrial Average","type":"index","source":"FRED","live":False},
    ]
    if now-ASSET_UNIVERSE_CACHE["at"]>3600:
        if not (MARKET_API_KEY and MARKET_API_SECRET):
            ASSET_UNIVERSE_CACHE.update({"at":now,"equities":[],"error":"Alpaca credentials are not configured."})
        else:
            try:
                req=Request("https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity",
                    headers={"APCA-API-KEY-ID":MARKET_API_KEY,"APCA-API-SECRET-KEY":MARKET_API_SECRET,"Accept":"application/json"})
                with urlopen(req,timeout=18) as response: raw=json.loads(response.read())
                assets=[]
                for a in raw if isinstance(raw,list) else []:
                    sym=str(a.get("symbol","")).upper()
                    if not re.fullmatch(r"[A-Z0-9.-]{1,12}",sym): continue
                    if not a.get("tradable",False): continue
                    assets.append({"symbol":sym,"name":str(a.get("name") or sym)[:100],
                        "type":"equity","exchange":a.get("exchange"),"source":"Alpaca IEX",
                        "live":True,"asset_class":a.get("class","us_equity")})
                ASSET_UNIVERSE_CACHE.update({"at":now,"equities":sorted(assets,key=lambda x:x["symbol"]),"error":None})
            except Exception as exc:
                ASSET_UNIVERSE_CACHE.update({"at":now,"equities":[],"error":"Alpaca asset directory unavailable: "+str(exc)[:120]})
    return {"indices":indices,"equities":ASSET_UNIVERSE_CACHE["equities"],
        "equity_count":len(ASSET_UNIVERSE_CACHE["equities"]),"error":ASSET_UNIVERSE_CACHE["error"],
        "note":"Alpaca's active US equity/ETF directory; availability and market-data entitlements depend on your subscription."}

def _fred_series_all(series_id, days):
    start=(date.today()-timedelta(days=max(500,days*2))).isoformat()
    if FRED_API_KEY:
        rows=_fred_api(series_id,start)
    else:
        url="https://fred.stlouisfed.org/graph/fredgraph.csv?id="+series_id+"&cosd="+start
        req=Request(url,headers={"User-Agent":"MIRA-research/1.0"})
        with urlopen(req,timeout=20) as response: raw=response.read().decode("utf-8-sig")
        rows=[]
        for row in csv.DictReader(io.StringIO(raw)):
            try: rows.append((row.get("observation_date") or row.get("DATE"),float(row.get(series_id) or row.get("VALUE"))))
            except (ValueError,TypeError): continue
    rows=[(d,float(v)) for d,v in rows if v and float(v)>0]
    return rows[-days:]

def asset_chart_payload(symbol, asset_type="equity", days=180):
    symbol=str(symbol or "").upper().strip()
    if not re.fullmatch(r"[A-Z0-9.-]{1,12}",symbol): raise ValueError("Invalid ticker symbol.")
    days=min((30,90,180,365,500,750,1000),key=lambda k:abs(k-int(days)))
    cache_key=(symbol,asset_type,days)
    cached=ASSET_CHART_CACHE.get(cache_key)
    if cached and time.time()-cached[0]<45: return dict(cached[1])
    if asset_type=="index":
        if symbol not in ASSET_INDEX_SERIES: raise ValueError("Unsupported index. Choose one of the listed FRED indices.")
        rows=_fred_series_all(ASSET_INDEX_SERIES[symbol],days)
        source="FRED daily observations"
        # FRED index observations are closes only; do not manufacture OHLC candles.
        dates=[d for d,v in rows]; closes=[float(v) for d,v in rows]
        candles=[]
        intraday=[]
        live={"connected":False,"symbol":symbol,"freshness":"CLOSED","error":"FRED index observations are daily, not a real-time exchange feed."}
    else:
        if not (MARKET_API_KEY and MARKET_API_SECRET): raise RuntimeError("Configure ALPACA_API_KEY and ALPACA_API_SECRET to load individual securities.")
        # The broker's asset-directory endpoint can be empty or unavailable even when its
        # market-data endpoint can serve a valid symbol. Query the requested ticker directly;
        # the provider response is the authority on whether this symbol/feed is available.
        start=(datetime.now(timezone.utc)-timedelta(days=max(730,days*2))).strftime("%Y-%m-%dT%H:%M:%SZ")
        base="https://data.alpaca.markets/v2/stocks/"+symbol+"/bars?"
        headers={"APCA-API-KEY-ID":MARKET_API_KEY,"APCA-API-SECRET-KEY":MARKET_API_SECRET,"Accept":"application/json"}
        req=Request(base+"timeframe=1Day&adjustment=all&feed=iex&sort=asc&limit=1000&start="+start,headers=headers)
        with urlopen(req,timeout=20) as response: raw=json.loads(response.read())
        bars=raw.get("bars") or []
        candles=[]
        for b in bars:
            try:
                c={"d":b["t"][:10],"o":float(b["o"]),"h":float(b["h"]),"l":float(b["l"]),"c":float(b["c"]),"v":float(b.get("v",0))}
                if c["o"]>0 and c["h"]>=max(c["o"],c["c"]) and c["l"]<=min(c["o"],c["c"]): candles.append(c)
            except (KeyError,TypeError,ValueError): continue
        candles=candles[-days:]
        dates=[c["d"] for c in candles]; closes=[c["c"] for c in candles]
        intraday=[]
        try:
            end=datetime.now(timezone.utc); st=end-timedelta(days=2)
            murl=base+"timeframe=1Min&adjustment=raw&feed=iex&sort=asc&limit=1000&start="+st.strftime("%Y-%m-%dT%H:%M:%SZ")+"&end="+end.strftime("%Y-%m-%dT%H:%M:%SZ")
            with urlopen(Request(murl,headers=headers),timeout=12) as response: mraw=json.loads(response.read())
            for b in mraw.get("bars") or []:
                try: intraday.append({"t":b["t"],"o":float(b["o"]),"h":float(b["h"]),"l":float(b["l"]),"c":float(b["c"]),"v":float(b.get("v",0))})
                except (KeyError,TypeError,ValueError): continue
        except Exception: intraday=[]
        source="Alpaca IEX daily bars"
        latest=candles[-1]["c"] if candles else None
        last_minute=intraday[-1]["c"] if intraday else None
        live={"connected":bool(last_minute is not None),"symbol":symbol,"last":last_minute,
            "freshness":"RECENT" if last_minute is not None else "CLOSED",
            "provider":"Alpaca IEX 1-minute bars" if last_minute is not None else "Alpaca IEX daily bars",
            "error":None if last_minute is not None else "No recent one-minute bar is available."}
    if len(closes)<30: raise RuntimeError("Insufficient historical observations for meaningful diagnostics.")
    # The NDX neural/risk model is only valid for the NDX training series.
    is_ndx=(asset_type=="index" and symbol=="NASDAQ100")
    brain=brain_load() if is_ndx else None
    if is_ndx:
        verdict=chart_fast_verdict(dates,closes,brain)
        if brain is not None: _record_ndx_prediction(dates,closes,verdict)
        model_scope="Nasdaq-100 neural downside-risk model; symbol-specific models are not substituted."
    else:
        am=_asset_model(symbol)
        latest_run=_latest_asset_run(symbol) if am is None else None
        probability=_asset_model_probability(am,dates,closes) if am else None
        if am and probability is not None and am.get("status")=="champion":
            try: _record_asset_prediction(symbol,dates,closes,am,probability)
            except Exception: pass
            threshold=float(am.get("threshold",.5)); alarm=probability>=threshold
            verdict={"verdict":"HOLD" if alarm else "NO CALL","confidence":"Calibrated risk estimate" if am.get("holdout",{}).get("brier") is not None else "Unverified","interim":False,"decision_status":"DOWNSIDE_RISK_ALARM" if alarm else "RISK_BELOW_THRESHOLD_NO_DIRECTIONAL_CALL","risk_score":probability,"alarm_level":threshold,"risk_alarm_active":alarm,"decision_reason":"Symbol-specific 21-session downside-risk estimate. This estimates an 8% drawdown event, not price direction or expected return.","model_version":am.get("version"),"model_scope":"Symbol-specific logistic risk model; validation and holdout details available in governance."}
            model_scope="Symbol-specific 21-session downside-risk model; not a directional forecast."
        else:
            reason=(latest_run.get("promotion_reason") if latest_run else "No symbol-specific model has completed chronological training and untouched holdout evaluation.")
            verdict={"verdict":"NO VALIDATED CALL","confidence":"N/A","interim":True,"decision_status":"CHALLENGER_NOT_PROMOTED" if latest_run else "SYMBOL_MODEL_NOT_TRAINED","decision_reason":reason,"risk_score":None,"alarm_level":None,"risk_alarm_active":None,"fusion":{"available":False,"reason":"No promoted symbol-specific model is available."}}
            model_scope="Challenger model not promoted; previous champion retained and no unvalidated probability is served." if latest_run else "No validated neural forecast for this instrument. Train a symbol-specific risk model before displaying a risk probability."
    q=chart_quantitative_intelligence(dates,closes,candles)
    payload={"symbol":symbol,"instrument_type":asset_type,"chart_instrument":symbol,
        "chart_source":source,"dates":dates,"closes":[round(x,4) for x in closes],
        "candles":candles,"intraday_candles":intraday,"intraday_status":{"status":"available" if intraday else "not available for this feed","bars":len(intraday)},
        "as_of":dates[-1],"live":live,"verdict":verdict,
        "quantitative_intelligence":q,"model_scope":model_scope,"symbol_model":_asset_model(symbol) if not is_ndx else None,
        "feature_drift":_asset_model_drift(am,dates,closes) if (not is_ndx and am) else {"available":False,"status":"no promoted symbol model"},
        "data_note":source+"; no synthetic candles. Data availability depends on provider entitlements."}
    ASSET_CHART_CACHE[cache_key]=(time.time(),payload)
    return payload


# -----------------------------------------------------------------------------
# MIRA Intelligence Workspace v1
# Evidence, portfolio risk, symbol-specific models and model governance.
# Every data item carries source/as-of metadata; unavailable providers stay absent.
# -----------------------------------------------------------------------------
import hashlib
import statistics

INTEL_DB = DATA_DIR / "mira_intelligence.sqlite3"
SEC_UA = " ".join(os.environ.get("MIRA_SEC_USER_AGENT", "").replace("\r", " ").replace("\n", " ").split())
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

# Supabase is the durable cloud backup for MIRA's intelligence workspace database.
# The secret key is read only by this Python process and is never sent to the browser.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SECRET_KEY = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY") or "").strip()
SUPABASE_BUCKET = "mira-private-state"
_SUPABASE_SYNC_LOCK = threading.RLock()
_SUPABASE_RESTORE_DONE = False
_SUPABASE_FILE_RESTORED = set()
_SUPABASE_SYNC_STATUS = {"configured": bool(SUPABASE_URL and SUPABASE_SECRET_KEY), "restored": False, "last_backup": None, "error": None}
_SUPABASE_SYNCING = threading.local()

def _supabase_storage_request(method, object_path, data=None):
    if not (SUPABASE_URL and SUPABASE_SECRET_KEY):
        raise RuntimeError("Supabase persistence is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the hosting environment.")
    from urllib.parse import quote
    url=SUPABASE_URL+"/storage/v1/object/"+SUPABASE_BUCKET+"/"+quote(object_path, safe="/")
    headers={"apikey":SUPABASE_SECRET_KEY,"Authorization":"Bearer "+SUPABASE_SECRET_KEY,"Content-Type":"application/octet-stream"}
    if method.upper()=="POST": headers["x-upsert"]="true"
    req=Request(url,data=data,headers=headers,method=method.upper())
    try:
        with urlopen(req,timeout=25) as response: return response.read()
    except HTTPError as exc:
        detail=exc.read(500).decode("utf-8","replace")
        if exc.code==404 and method.upper()=="GET": return None
        raise RuntimeError("Supabase Storage returned HTTP "+str(exc.code)+": "+detail[:250]) from exc

def _supabase_restore_intel_db():
    global _SUPABASE_RESTORE_DONE
    with _SUPABASE_SYNC_LOCK:
        if _SUPABASE_RESTORE_DONE: return
        _SUPABASE_RESTORE_DONE=True
        if not (SUPABASE_URL and SUPABASE_SECRET_KEY): return
        try:
            blob=_supabase_storage_request("GET","mira_intelligence.sqlite3")
            if blob:
                INTEL_DB.parent.mkdir(parents=True,exist_ok=True)
                tmp=INTEL_DB.with_suffix(".restore.tmp")
                tmp.write_bytes(blob)
                # Validate the SQLite image before replacing the local cache.
                check=sqlite3.connect(str(tmp)); check.execute("PRAGMA quick_check").fetchone(); check.close()
                tmp.replace(INTEL_DB)
                _SUPABASE_SYNC_STATUS["restored"]=True
            elif INTEL_DB.exists() and INTEL_DB.stat().st_size>0:
                _SUPABASE_SYNC_STATUS["restored"]=True
        except Exception as exc:
            _SUPABASE_SYNC_STATUS["error"]="Restore failed: "+str(exc)[:220]

def _supabase_backup_intel_db():
    if not (SUPABASE_URL and SUPABASE_SECRET_KEY) or getattr(_SUPABASE_SYNCING,"active",False): return
    if not INTEL_DB.exists() or INTEL_DB.stat().st_size==0: return
    with _SUPABASE_SYNC_LOCK:
        _SUPABASE_SYNCING.active=True
        temp=INTEL_DB.with_suffix(".backup.tmp")
        try:
            src=sqlite3.connect(str(INTEL_DB),timeout=15)
            dst=sqlite3.connect(str(temp),timeout=15)
            try: src.backup(dst)
            finally: dst.close(); src.close()
            _supabase_storage_request("POST","mira_intelligence.sqlite3",temp.read_bytes())
            _SUPABASE_SYNC_STATUS.update({"last_backup":datetime.now(timezone.utc).isoformat(timespec="seconds"),"error":None})
        except Exception as exc:
            _SUPABASE_SYNC_STATUS["error"]="Backup failed: "+str(exc)[:220]
        finally:
            temp.unlink(missing_ok=True)
            _SUPABASE_SYNCING.active=False

def _supabase_restore_file(local_path, object_name):
    """Restore the latest durable MIRA data/model file once per process, preferring the cloud copy."""
    local_path=Path(local_path)
    if object_name in _SUPABASE_FILE_RESTORED or not (SUPABASE_URL and SUPABASE_SECRET_KEY): return
    _SUPABASE_FILE_RESTORED.add(object_name)
    try:
        blob=_supabase_storage_request("GET",object_name)
        if blob is not None:
            local_path.parent.mkdir(parents=True,exist_ok=True)
            tmp=local_path.with_suffix(local_path.suffix+".restore.tmp")
            tmp.write_bytes(blob); tmp.replace(local_path)
    except Exception as exc:
        _SUPABASE_FILE_RESTORED.discard(object_name)
        _SUPABASE_SYNC_STATUS["error"]="File restore failed: "+str(exc)[:200]

def _supabase_backup_file(local_path, object_name):
    local_path=Path(local_path)
    if not local_path.is_file() or not (SUPABASE_URL and SUPABASE_SECRET_KEY): return
    try:
        _supabase_storage_request("POST",object_name,local_path.read_bytes())
        _SUPABASE_SYNC_STATUS.update({"last_backup":datetime.now(timezone.utc).isoformat(timespec="seconds"),"error":None})
    except Exception as exc:
        _SUPABASE_SYNC_STATUS["error"]="File backup failed: "+str(exc)[:200]

class _MiraSQLiteConnection(sqlite3.Connection):
    def commit(self):
        super().commit()
        # Synchronous backup means a successful local commit is immediately mirrored remotely.
        _supabase_backup_intel_db()

def intel_db():
    INTEL_DB.parent.mkdir(parents=True, exist_ok=True)
    _supabase_restore_intel_db()
    db=sqlite3.connect(INTEL_DB, timeout=15, factory=_MiraSQLiteConnection)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS theses (
      id TEXT PRIMARY KEY, workspace TEXT NOT NULL, symbol TEXT NOT NULL,
      title TEXT NOT NULL, thesis TEXT NOT NULL, assumptions TEXT NOT NULL,
      invalidation TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS theses_workspace ON theses(workspace, updated_at DESC);
    CREATE TABLE IF NOT EXISTS thesis_evidence (
      workspace TEXT NOT NULL, thesis_id TEXT NOT NULL, symbol TEXT NOT NULL,
      fetched_at TEXT NOT NULL, payload_json TEXT NOT NULL,
      PRIMARY KEY(workspace,thesis_id)
    );
    CREATE INDEX IF NOT EXISTS thesis_evidence_symbol ON thesis_evidence(workspace,symbol,fetched_at DESC);
    CREATE TABLE IF NOT EXISTS portfolios (
      workspace TEXT NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL,
      updated_at TEXT NOT NULL, PRIMARY KEY(workspace,symbol)
    );
    CREATE TABLE IF NOT EXISTS asset_models (
      symbol TEXT PRIMARY KEY, instrument_type TEXT NOT NULL, model_json TEXT NOT NULL,
      created_at TEXT NOT NULL, data_as_of TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS asset_model_runs (
      run_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, status TEXT NOT NULL,
      model_json TEXT NOT NULL, created_at TEXT NOT NULL, data_as_of TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS asset_model_runs_symbol ON asset_model_runs(symbol,created_at DESC);
    CREATE TABLE IF NOT EXISTS prediction_ledger (
      id TEXT PRIMARY KEY, symbol TEXT NOT NULL, as_of TEXT NOT NULL,
      model_version TEXT NOT NULL, probability REAL NOT NULL, threshold REAL NOT NULL,
      horizon INTEGER NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL,
      outcome INTEGER, graded_at TEXT
    );
    CREATE INDEX IF NOT EXISTS prediction_due ON prediction_ledger(symbol,as_of,horizon,outcome);
    CREATE TABLE IF NOT EXISTS learning_events (
      event_id TEXT PRIMARY KEY, source TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT '',
      event_type TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS learning_events_recent ON learning_events(observed_at DESC);
    CREATE INDEX IF NOT EXISTS learning_events_source ON learning_events(source,observed_at DESC);
    """)
    return db


def record_learning_events(events):
    """Persist a batch of source-attributed observations with one SQLite/Supabase commit."""
    try:
        rows=[]; digests=[]
        for source,event_type,symbol,payload,observed_at in events:
            stamp=observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
            body=json.dumps(payload or {},sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str)
            if len(body)>60000: body=body[:60000]
            digest=hashlib.sha256((str(source)+"|"+str(event_type)+"|"+str(symbol)+"|"+body).encode("utf-8")).hexdigest()
            rows.append((digest,str(source)[:40],str(symbol or "")[:20],str(event_type)[:60],stamp,body));digests.append(digest)
        if not rows:return []
        db=intel_db()
        try:
            db.executemany("INSERT OR IGNORE INTO learning_events(event_id,source,symbol,event_type,observed_at,payload_json) VALUES(?,?,?,?,?,?)",rows)
            db.execute("DELETE FROM learning_events WHERE event_id IN (SELECT event_id FROM learning_events ORDER BY observed_at DESC LIMIT -1 OFFSET 12000)")
            db.commit()
        finally: db.close()
        return digests
    except Exception as exc:
        try: visible_status("learning_memory","Could not persist incoming learning events: "+str(exc)[:120])
        except Exception: pass
        return []


def record_learning_event(source, event_type, symbol="", payload=None, observed_at=None):
    """Persist a deduplicated observation. Event memory is not automatically a supervised label."""
    result=record_learning_events([(source,event_type,symbol,payload,observed_at)])
    return result[0] if result else None


LEARNING_RUNTIME={"last_supervisor_cycle":None,"cycles":0,"last_error":None}

def learning_status_payload():
    db=intel_db()
    try:
        total=db.execute("SELECT COUNT(*) FROM learning_events").fetchone()[0]
        groups=[dict(r) for r in db.execute("SELECT source,COUNT(*) AS events,MAX(observed_at) AS last_seen FROM learning_events GROUP BY source ORDER BY last_seen DESC").fetchall()]
        recent=[dict(r) for r in db.execute("SELECT source,symbol,event_type,observed_at FROM learning_events ORDER BY observed_at DESC LIMIT 8").fetchall()]
        graded=db.execute("SELECT COUNT(*) FROM prediction_ledger WHERE outcome IS NOT NULL").fetchone()[0]
        pending=db.execute("SELECT COUNT(*) FROM prediction_ledger WHERE outcome IS NULL").fetchone()[0]
    finally: db.close()
    brain=brain_load() or {}
    return {"enabled":True,"event_memory":"supabase-backed SQLite snapshot" if _SUPABASE_SYNC_STATUS.get("configured") else "local SQLite (cloud filesystem may be ephemeral)","events_total":total,"sources":groups,"recent":recent,"predictions_graded":graded,"predictions_pending":pending,"model_trained_through":brain.get("trained_through"),"model_features":brain.get("features",[]),"training_running":bool(BRAIN_JOB.get("running")),"training_message":BRAIN_JOB.get("message"),"supervisor":dict(LEARNING_RUNTIME),"learning_policy":"New observations are retained and transformed into features; supervised model updates wait for matured labels and pass chronological validation. News/filings are evidence memory until a point-in-time labelled historical corpus is available.","continuous_compute_note":"Runs while the Render process is awake. Render Free may sleep/restart; this endpoint cannot guarantee 24/7 execution."}


def _workspace(handler):
    raw=(handler.headers.get("X-MIRA-Workspace") or "").strip()
    if not re.fullmatch(r"[A-Fa-f0-9-]{32,36}",raw):
        raise ValueError("A browser workspace ID is required.")
    return hashlib.sha256(raw.encode()).hexdigest()


def _json_response(handler, payload, status=200):
    body=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type","application/json; charset=utf-8")
    handler.send_header("Cache-Control","no-store")
    handler.send_header("Content-Length",str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _safe_float(value, name, minimum=None):
    try: x=float(value)
    except (TypeError,ValueError): raise ValueError(name+" must be numeric.")
    if not math.isfinite(x) or (minimum is not None and x<minimum): raise ValueError(name+" is outside its valid range.")
    return x


def _sec_get(url):
    if not SEC_UA or "@" not in SEC_UA:
        raise RuntimeError("SEC research is disabled until MIRA_SEC_USER_AGENT is configured with an identifying contact email.")
    req=Request(url,headers={"User-Agent":SEC_UA,"Accept":"application/json"})
    with urlopen(req,timeout=18) as response: return json.loads(response.read())


SEC_TICKERS_CACHE={"at":0.0,"rows":None}
RESEARCH_BUNDLE_CACHE={}
def _sec_cik_for_symbol(symbol):
    now=time.time()
    if SEC_TICKERS_CACHE["rows"] is None or now-SEC_TICKERS_CACHE["at"]>86400:
        raw=_sec_get("https://www.sec.gov/files/company_tickers.json")
        rows={}
        for v in raw.values():
            t=str(v.get("ticker","")).upper()
            if t: rows[t]={"cik":int(v["cik_str"]),"name":v.get("title",t)}
        SEC_TICKERS_CACHE.update({"at":now,"rows":rows})
    return SEC_TICKERS_CACHE["rows"].get(symbol)


def _sec_company_research(symbol):
    info=_sec_cik_for_symbol(symbol)
    if not info: return {"available":False,"reason":"Ticker was not found in the SEC company ticker directory.","source":"SEC EDGAR"}
    cik=str(info["cik"]).zfill(10)
    facts=_sec_get("https://data.sec.gov/api/xbrl/companyfacts/CIK"+cik+".json")
    submissions=_sec_get("https://data.sec.gov/submissions/CIK"+cik+".json")
    us=facts.get("facts",{}).get("us-gaap",{})
    def latest_fact(candidates):
        for tag in candidates:
            node=us.get(tag)
            if not node: continue
            units=node.get("units",{})
            arr=units.get("USD") or units.get("shares") or units.get("USD/shares") or []
            valid=[x for x in arr if x.get("form") in ("10-K","10-Q") and x.get("fy") and x.get("end")]
            if valid:
                valid.sort(key=lambda x:(x.get("end",""),x.get("filed","")))
                x=valid[-1]
                return {"value":x.get("val"),"unit":(next(iter(units),None)),"period_end":x.get("end"),"filed":x.get("filed"),"form":x.get("form"),"tag":tag}
        return None
    metrics={
      "revenue":latest_fact(["RevenueFromContractWithCustomerExcludingAssessedTax","SalesRevenueNet","Revenues"]),
      "net_income":latest_fact(["NetIncomeLoss"]),
      "total_assets":latest_fact(["Assets"]),
      "total_liabilities":latest_fact(["Liabilities"]),
      "operating_cash_flow":latest_fact(["NetCashProvidedByUsedInOperatingActivities"]),
      "cash_and_equivalents":latest_fact(["CashAndCashEquivalentsAtCarryingValue"])
    }
    rec=submissions.get("filings",{}).get("recent",{})
    forms=rec.get("form",[]); dates=rec.get("filingDate",[]); accession=rec.get("accessionNumber",[]); docs=rec.get("primaryDocument",[]); report=rec.get("primaryDocDescription",[])
    filings=[]
    for i,form in enumerate(forms[:100]):
        if form not in ("10-K","10-Q","8-K","20-F","6-K"): continue
        acc=accession[i].replace("-","") if i<len(accession) else ""
        doc=docs[i] if i<len(docs) else ""
        if not acc or not doc: continue
        filings.append({"form":form,"filed":dates[i] if i<len(dates) else None,"description":report[i] if i<len(report) else "SEC filing","url":f"https://www.sec.gov/Archives/edgar/data/{info['cik']}/{acc}/{doc}","source":"SEC EDGAR"})
        if len(filings)>=15: break
    return {"available":True,"symbol":symbol,"company":info["name"],"cik":info["cik"],"metrics":metrics,"filings":filings,"source":"SEC EDGAR Company Facts + Submissions","retrieved_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"note":"Latest reported facts can have different fiscal periods. Each value retains its own filing date and period end."}


def _news_for_symbol(symbol):
    if not FINNHUB_KEY:
        return {"available":False,"items":[],"reason":"News provider not configured. Set FINNHUB_API_KEY; no headlines are fabricated.","source":None}
    end=date.today(); start=end-timedelta(days=14)
    url=f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={start.isoformat()}&to={end.isoformat()}&token={FINNHUB_KEY}"
    req=Request(url,headers={"User-Agent":"MIRA-Research/1.0"})
    with urlopen(req,timeout=12) as response: raw=json.loads(response.read())
    items=[]
    for x in raw[:30]:
        items.append({"headline":str(x.get("headline",""))[:300],"summary":str(x.get("summary",""))[:800],"url":x.get("url"),"source":x.get("source"),"published_at":datetime.fromtimestamp(int(x.get("datetime",0)),timezone.utc).isoformat(timespec="seconds") if x.get("datetime") else None})
    return {"available":True,"items":items,"source":"Finnhub company news","retrieved_at":datetime.now(timezone.utc).isoformat(timespec="seconds")}


def research_bundle(symbol):
    symbol=str(symbol or "").upper().strip()
    if not re.fullmatch(r"[A-Z0-9.-]{1,12}",symbol): raise ValueError("Invalid ticker symbol.")
    cached=RESEARCH_BUNDLE_CACHE.get(symbol)
    if cached and time.time()-cached[0]<900:return cached[1]
    result={"symbol":symbol,"retrieved_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"fundamentals":{"available":False},"filings":{"available":False},"news":{"available":False},"macro":{"available":False}}
    try:
        sec=_sec_company_research(symbol); result["fundamentals"]={k:v for k,v in sec.items() if k not in ("filings",)}; result["filings"]={"available":sec.get("available",False),"items":sec.get("filings",[]),"source":sec.get("source"),"reason":sec.get("reason")}
    except Exception as exc: result["fundamentals"]={"available":False,"reason":str(exc)[:220]}; result["filings"]={"available":False,"items":[],"reason":str(exc)[:220]}
    try: result["news"]=_news_for_symbol(symbol)
    except Exception as exc: result["news"]={"available":False,"items":[],"reason":"News provider request failed: "+str(exc)[:160]}
    try:
        # Use the already-cached FRED observations; never make a company-research page wait on three slow macro downloads.
        cached=MACRO_CACHE.get("series",{}); observations={}
        for name,pair in cached.items():
            if pair and pair[0]: observations[name]={"date":pair[0][-1],"value":pair[1][-1]}
        result["macro"]={"available":bool(observations),"observations":observations,"source":"FRED macro series","as_of":max((x["date"] for x in observations.values()),default=None),"reason":None if observations else "Macro series have not yet been loaded into this server process."}
    except Exception as exc: result["macro"]={"available":False,"reason":str(exc)[:160]}
    events=[]
    for item in result.get("news",{}).get("items",[]):
        events.append(("finnhub_news","news_article",symbol,item,item.get("published_at")))
    for item in result.get("filings",{}).get("items",[]):
        events.append(("sec_edgar","sec_filing",symbol,item,item.get("filed") or item.get("filing_date")))
    if result.get("fundamentals",{}).get("available"):
        snapshot=dict(result["fundamentals"]);snapshot.pop("retrieved_at",None)
        events.append(("sec_edgar","fundamental_snapshot",symbol,snapshot,None))
    record_learning_events(events)
    RESEARCH_BUNDLE_CACHE[symbol]=(time.time(),result)
    return result


def _returns_from_prices(prices):
    return [math.log(prices[i]/prices[i-1]) for i in range(1,len(prices)) if prices[i]>0 and prices[i-1]>0]


def _portfolio_payload(workspace):
    db=intel_db()
    try: holdings=[dict(r) for r in db.execute("SELECT symbol,quantity,updated_at FROM portfolios WHERE workspace=? ORDER BY symbol",(workspace,)).fetchall()]
    finally: db.close()
    if not holdings: return {"available":False,"holdings":[],"reason":"Add at least one holding with a positive quantity."}
    series={}; names={}; failed=[]
    for h in holdings:
        sym=h["symbol"]
        try:
            if sym in ASSET_INDEX_SERIES:
                failed.append({"symbol":sym,"reason":"FRED indices are research benchmarks, not tradable portfolio holdings. Use an ETF proxy instead."}); continue
            d=asset_chart_payload(sym,"equity",500); dates=d["dates"]; vals=d["closes"]
            series[sym]=dict(zip(dates,vals)); names[sym]=vals[-1]*h["quantity"] if vals else 0
        except Exception as exc: failed.append({"symbol":sym,"reason":str(exc)[:140]})
    usable=[s for s in series if len(series[s])>=31]
    if not usable: return {"available":False,"holdings":holdings,"failed":failed,"reason":"No holding has sufficient aligned price history."}
    common=sorted(set.intersection(*(set(series[s]) for s in usable)))
    if len(common)<31: return {"available":False,"holdings":holdings,"failed":failed,"reason":"Insufficient common trading dates across holdings."}
    values={s:[series[s][d] for d in common] for s in usable}; weights={s:names[s] for s in usable}; total=sum(weights.values())
    if total<=0: return {"available":False,"holdings":holdings,"reason":"Portfolio market value is not positive."}
    w={s:weights[s]/total for s in usable}; rets={s:_returns_from_prices(values[s]) for s in usable}; n=len(common)-1
    port=[sum(w[s]*rets[s][i] for s in usable) for i in range(n)]
    mu=sum(port)/n; vol=math.sqrt(sum((x-mu)**2 for x in port)/(n-1))*math.sqrt(252)
    sorted_loss=sorted(port); qidx=max(0,int(.05*n)-1); var=max(0,-sorted_loss[qidx]); tail=[x for x in port if x<=sorted_loss[qidx]]; cvar=max(0,-sum(tail)/max(1,len(tail)))
    peak=values[usable[0]][0]; curve=1.0; peak_curve=1.0; mdd=0.0
    for r in port:
        curve*=math.exp(r); peak_curve=max(peak_curve,curve); mdd=min(mdd,curve/peak_curve-1)
    corr={}
    for a in usable:
        corr[a]={}
        for b in usable:
            ra,rb=rets[a],rets[b]; ma=sum(ra)/len(ra); mb=sum(rb)/len(rb)
            den=math.sqrt(sum((x-ma)**2 for x in ra)*sum((y-mb)**2 for y in rb))
            corr[a][b]=(sum((x-ma)*(y-mb) for x,y in zip(ra,rb))/den) if den else None
    concentration={s:round(w[s],6) for s in usable}
    # Broad-market beta proxy against the FRED S&P 500 series; not a commercial factor model.
    factor_exposures={}; factor_dates=[]
    try:
        sp_rows=_fred_series_all("SP500",500); sp=dict(sp_rows); factor_dates=sorted(set(common)&set(sp))
        if len(factor_dates)>=31:
            market=_returns_from_prices([sp[d] for d in factor_dates]); mv=sum(market)/len(market); mvar=sum((x-mv)**2 for x in market)/max(1,len(market)-1)
            for sym in usable:
                own=_returns_from_prices([series[sym][d] for d in factor_dates]); ov=sum(own)/len(own)
                cov=sum((x-ov)*(y-mv) for x,y in zip(own,market))/max(1,len(own)-1)
                factor_exposures[sym]={"sp500_beta_proxy":cov/mvar if mvar>0 else None,"observations":len(own),"benchmark":"FRED S&P 500 daily close","note":"Single-factor historical beta proxy; not Barra or a multi-factor risk model."}
    except Exception as exc: factor_exposures={"unavailable":{"reason":str(exc)[:120]}}
    # Euler-style volatility contributions under the historical sample covariance matrix.
    cov_matrix={}; contribution={}
    for a0 in usable:
        cov_matrix[a0]={}
        for b0 in usable:
            ra,rb=rets[a0],rets[b0]; ma=sum(ra)/len(ra); mb=sum(rb)/len(rb)
            cov_matrix[a0][b0]=sum((x-ma)*(y-mb) for x,y in zip(ra,rb))/max(1,len(ra)-1)
    port_daily_var=sum(w[a0]*w[b0]*cov_matrix[a0][b0] for a0 in usable for b0 in usable)
    for a0 in usable:
        marginal=sum(w[b0]*cov_matrix[a0][b0] for b0 in usable)
        contribution[a0]=(w[a0]*marginal/port_daily_var) if port_daily_var>0 else None
    stress={"parallel_market_shock_minus_5pct":-0.05,"parallel_market_shock_minus_10pct":-0.10,"historical_volatility_doubled_one_day":-vol/math.sqrt(252)}
    return {"available":True,"as_of":common[-1],"observations":n,"holdings":[{"symbol":s,"quantity":next(x["quantity"] for x in holdings if x["symbol"]==s),"market_value_estimate":weights[s],"weight":w[s],"volatility_contribution_share":contribution[s]} for s in usable],"failed":failed,"annualized_volatility":vol,"historical_daily_var_95":var,"historical_daily_cvar_95":cvar,"max_drawdown":mdd,"correlation":corr,"concentration":concentration,"factor_exposures":factor_exposures,"stress_scenarios":stress,"method":"Equal timestamp intersection; close-to-close log returns; current weights held constant; historical 95% VaR/CVaR; Euler volatility contributions; S&P 500 single-factor beta proxy where available. No fees, taxes, slippage or intraday risk.","source":"FRED + Alpaca IEX daily bars"}


def _workspace_theses(workspace):
    db=intel_db()
    try:
        rows=[dict(r) for r in db.execute("SELECT * FROM theses WHERE workspace=? ORDER BY updated_at DESC",(workspace,)).fetchall()]
        evidence={r["thesis_id"]:r["fetched_at"] for r in db.execute("SELECT thesis_id,fetched_at FROM thesis_evidence WHERE workspace=?",(workspace,)).fetchall()}
    finally: db.close()
    return {"items":[{**r,"assumptions":json.loads(r["assumptions"]),"invalidation":json.loads(r["invalidation"]),"last_evidence_check":evidence.get(r["id"])} for r in rows]}


def _thesis_evidence(workspace, symbol):
    db=intel_db()
    try:
        rows=db.execute("SELECT t.id,t.symbol,t.title,e.fetched_at,e.payload_json FROM theses t LEFT JOIN thesis_evidence e ON e.thesis_id=t.id AND e.workspace=t.workspace WHERE t.workspace=? AND t.symbol=? ORDER BY t.updated_at DESC",(workspace,symbol)).fetchall()
    finally: db.close()
    return {"symbol":symbol,"items":[{"id":r["id"],"symbol":r["symbol"],"title":r["title"],"fetched_at":r["fetched_at"],"evidence":json.loads(r["payload_json"]) if r["payload_json"] else None} for r in rows]}


def thesis_monitor_loop():
    """Refresh saved thesis evidence periodically; only configured providers can contribute evidence."""
    while True:
        time.sleep(60)
        try:
            db=intel_db()
            try: targets=[dict(r) for r in db.execute("SELECT workspace,id,symbol FROM theses ORDER BY updated_at DESC LIMIT 100").fetchall()]
            finally: db.close()
            seen=set()
            for row in targets:
                key=(row["workspace"],row["symbol"])
                if key in seen: continue
                seen.add(key)
                db=intel_db()
                try:
                    previous=db.execute("SELECT MAX(fetched_at) FROM thesis_evidence WHERE workspace=? AND symbol=?",key).fetchone()[0]
                    prior_row=db.execute("SELECT payload_json FROM thesis_evidence WHERE workspace=? AND symbol=? ORDER BY fetched_at DESC LIMIT 1",key).fetchone()
                finally: db.close()
                if previous:
                    try: age=(datetime.now(timezone.utc)-datetime.fromisoformat(previous)).total_seconds()
                    except ValueError: age=999999
                    if age<21600: continue
                prior=json.loads(prior_row[0]) if prior_row else {}
                payload=research_bundle(row["symbol"]); fetched=datetime.now(timezone.utc).isoformat(timespec="seconds")
                prior_urls={x.get("url") for x in ((prior.get("filings") or {}).get("items",[])+(prior.get("news") or {}).get("items",[])) if x.get("url")}
                current=(payload.get("filings") or {}).get("items",[])+(payload.get("news") or {}).get("items",[])
                payload["monitor_changes"]={"previous_scan":previous,"new_items":[x for x in current if x.get("url") and x.get("url") not in prior_urls],"checked_at":fetched}
                for item in payload["monitor_changes"]["new_items"]:
                    record_learning_event("thesis_monitor","new_thesis_evidence",row["symbol"],item,observed_at=item.get("published_at") or fetched)
                db=intel_db()
                try:
                    for thesis in [x for x in targets if x["workspace"]==row["workspace"] and x["symbol"]==row["symbol"]]:
                        db.execute("INSERT INTO thesis_evidence(workspace,thesis_id,symbol,fetched_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(workspace,thesis_id) DO UPDATE SET symbol=excluded.symbol,fetched_at=excluded.fetched_at,payload_json=excluded.payload_json",(row["workspace"],thesis["id"],row["symbol"],fetched,json.dumps(payload)))
                    db.commit()
                finally: db.close()
                time.sleep(.2)
        except Exception as exc:
            visible_status("thesis_monitor","Evidence refresh encountered a provider or storage error: "+str(exc)[:140])
        time.sleep(21600)


def macro_updater_loop():
    while True:
        try: refresh_macro(force=True)
        except Exception: pass
        time.sleep(21600)


def _save_thesis(workspace,payload):
    symbol=str(payload.get("symbol","")).upper().strip(); title=str(payload.get("title","")).strip(); thesis=str(payload.get("thesis","")).strip()
    if not re.fullmatch(r"[A-Z0-9.-]{1,12}",symbol): raise ValueError("A valid ticker symbol is required.")
    if not title or len(title)>180 or not thesis or len(thesis)>4000: raise ValueError("Provide a title (max 180 characters) and thesis (max 4,000 characters).")
    assumptions=payload.get("assumptions",[]); invalidation=payload.get("invalidation",[])
    if not isinstance(assumptions,list) or not isinstance(invalidation,list) or len(assumptions)>20 or len(invalidation)>20: raise ValueError("Assumptions and invalidation conditions must be lists of at most 20 items.")
    assumptions=[str(x).strip()[:500] for x in assumptions if str(x).strip()]; invalidation=[str(x).strip()[:500] for x in invalidation if str(x).strip()]
    ident=str(payload.get("id") or __import__("uuid").uuid4()); now=datetime.now(timezone.utc).isoformat(timespec="seconds")
    db=intel_db()
    try:
        db.execute("INSERT INTO theses(id,workspace,symbol,title,thesis,assumptions,invalidation,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET symbol=excluded.symbol,title=excluded.title,thesis=excluded.thesis,assumptions=excluded.assumptions,invalidation=excluded.invalidation,updated_at=excluded.updated_at WHERE theses.workspace=excluded.workspace",(ident,workspace,symbol,title,thesis,json.dumps(assumptions),json.dumps(invalidation),now,now)); db.commit()
    finally: db.close()
    return {"saved":True,"id":ident,"updated_at":now}


def _train_asset_model(symbol, asset_type, dates, closes):
    """Train symbol-specific downside-risk candidates; select by validation Brier, score once on holdout."""
    if len(closes)<260: raise ValueError("At least 260 daily closes are required before training a symbol-specific model.")
    rows=[]; labels=[]
    for i in range(BRAIN_WARMUP,len(closes)-21):
        hist=closes[:i+1]; ret=_returns_from_prices(hist)
        if len(ret)<60: continue
        def sd(a):
            z=ret[-a:]; m=sum(z)/len(z); return math.sqrt(sum((v-m)**2 for v in z)/max(1,len(z)-1))
        feat=[ret[-1],sum(ret[-5:]),sum(ret[-20:]),sum(ret[-60:]),sd(5),sd(20),hist[-1]/max(hist[-20:])-1,hist[-1]/(sum(hist[-20:])/20)-1]
        future=closes[i+1:i+22]; y=int(min(future)/closes[i]-1<=-.08)
        rows.append((dates[i],feat)); labels.append(y)
    n=len(rows); a=int(n*.60); b=int(n*.80); gap=BRAIN_GAP
    train_ids=list(range(0,max(0,a-gap))); valid_ids=list(range(a,max(a+50,b-gap))); test_ids=list(range(b,n))
    if min(len(train_ids),len(valid_ids),len(test_ids))<30: raise ValueError("Not enough purged chronological observations for train/validation/test splits.")
    dim=len(rows[0][1]); means=[sum(rows[i][1][j] for i in train_ids)/len(train_ids) for j in range(dim)]
    scales=[math.sqrt(sum((rows[i][1][j]-means[j])**2 for i in train_ids)/len(train_ids)) or 1 for j in range(dim)]
    X=[[max(-8,min(8,(r[j]-means[j])/scales[j])) for j in range(dim)] for _,r in rows]; y=labels
    def sig(z): return 1/(1+math.exp(-max(-30,min(30,z))))
    def score_brier(probs, ids): return sum((pp-y[i])**2 for pp,i in zip(probs,ids))/len(ids)
    # Candidate A: L2-regularized logistic regression.
    w=[0.0]*(dim+1); lr=.035; lam=.025
    for ep in range(650):
        grad=[0.0]*(dim+1)
        for i in train_ids:
            pp=sig(w[0]+sum(w[j+1]*X[i][j] for j in range(dim))); e=pp-y[i]
            grad[0]+=e
            for j in range(dim): grad[j+1]+=e*X[i][j]
        for j in range(dim+1): w[j]-=lr*(grad[j]/len(train_ids)+(lam*w[j] if j else 0))
        if ep%100==99: lr*=.92
    logit=lambda i:w[0]+sum(w[j+1]*X[i][j] for j in range(dim))
    # Candidate B: shallow gradient-boosted decision stumps, fitted only on training rows.
    prevalence=min(.999,max(.001,sum(y[i] for i in train_ids)/len(train_ids)))
    base=math.log(prevalence/(1-prevalence)); raw={i:base for i in train_ids+valid_ids+test_ids}; stumps=[]
    for _ in range(60):
        probs={i:sig(raw[i]) for i in train_ids}; residual={i:y[i]-probs[i] for i in train_ids}; best=None
        for j in range(dim):
            vals=sorted(set(X[i][j] for i in train_ids))
            if len(vals)<3: continue
            cuts=[(vals[int((len(vals)-1)*q)] + vals[min(len(vals)-1,int((len(vals)-1)*q)+1)])/2 for q in (.1,.2,.3,.4,.5,.6,.7,.8,.9)]
            for cut in cuts:
                left=[i for i in train_ids if X[i][j]<=cut]; right=[i for i in train_ids if X[i][j]>cut]
                if len(left)<12 or len(right)<12: continue
                sl=sum(residual[i] for i in left); sr=sum(residual[i] for i in right)
                gain=sl*sl/(len(left)+5)+sr*sr/(len(right)+5)
                if best is None or gain>best[0]: best=(gain,j,cut,.08*sl/(len(left)+5),.08*sr/(len(right)+5))
        if best is None: break
        _,j,cut,lv,rv=best; stumps.append({"feature":j,"threshold":cut,"left":lv,"right":rv})
        for i in raw: raw[i]+=lv if X[i][j]<=cut else rv
    def boost_logit(i): return base+sum(st["left"] if X[i][st["feature"]]<=st["threshold"] else st["right"] for st in stumps)
    # Platt calibration is fitted on the validation block only for each candidate.
    def calibrate(logits):
        aa=0.0; bb=1.0
        for _ in range(300):
            ga=gb=0.0
            for zz,yy in zip(logits,[y[i] for i in valid_ids]):
                pp=sig(aa+bb*zz); ga+=pp-yy; gb+=(pp-yy)*zz
            aa-=.025*ga/len(logits); bb-=.025*gb/len(logits)
        return {"intercept":aa,"slope":bb}
    cal_log=calibrate([logit(i) for i in valid_ids]); cal_boost=calibrate([boost_logit(i) for i in valid_ids])
    val_log=[sig(cal_log["intercept"]+cal_log["slope"]*logit(i)) for i in valid_ids]
    val_boost=[sig(cal_boost["intercept"]+cal_boost["slope"]*boost_logit(i)) for i in valid_ids]
    candidates={"regularized_logistic":{"brier":score_brier(val_log,valid_ids)},"gradient_boosted_stumps":{"brier":score_brier(val_boost,valid_ids)}}
    neural_model=None; neural_val=None; neural_hold=None
    if len(closes)>=650:
        try:
            neural_model=train_brain(dates,closes,8,feature_names=BRAIN_FEATURES)
            neural_model["trained_label_through"]=dates[-BRAIN_HORIZON-1]
            nf,ny,nidx,ntrain,nvalid,nhold=brain_split(dates,closes)
            nfeatures=[select_features(row,BRAIN_FEATURES) if row is not None else None for row in nf]
            ev=neural_model["eval"]
            nprob=brain_predict_ids(brain_ensemble(ev["members"]),ev["mean"],ev["scale"],nfeatures,nvalid)
            hprob=brain_predict_ids(brain_ensemble(ev["members"]),ev["mean"],ev["scale"],nfeatures,nhold)
            date_to_index={d:i for i,d in enumerate(dates)}
            neural_valid_indices=[date_to_index[rows[i][0]] for i in valid_ids]
            neural_test_indices=[date_to_index[rows[i][0]] for i in test_ids]
            neural_val=[nprob[i] for i in neural_valid_indices if i in nprob]
            neural_hold=[hprob[i] for i in neural_test_indices] if all(i in hprob for i in neural_test_indices) else None
            if len(neural_val)==len(valid_ids) and neural_hold is not None and len(neural_hold)==len(test_ids):
                candidates["neural_ensemble"]={"brier":score_brier(neural_val,valid_ids)}
        except Exception as exc:
            candidates["neural_ensemble"]={"available":False,"reason":"Neural candidate training failed: "+str(exc)[:140]}
            neural_model=None; neural_val=None; neural_hold=None
    selected=min(((v["brier"],k) for k,v in candidates.items() if v.get("brier") is not None),default=(float("inf"),"regularized_logistic"))[1]
    if selected=="regularized_logistic":
        model_core={"model_type":selected,"weights":w,"calibration":cal_log}; valps=val_log; holdps=[sig(cal_log["intercept"]+cal_log["slope"]*logit(i)) for i in test_ids]
    elif selected=="gradient_boosted_stumps":
        model_core={"model_type":selected,"base_logit":base,"stumps":stumps,"calibration":cal_boost}; valps=val_boost; holdps=[sig(cal_boost["intercept"]+cal_boost["slope"]*boost_logit(i)) for i in test_ids]
    else:
        model_core={"model_type":selected,"neural_model":neural_model}; valps=neural_val; holdps=neural_hold
    ysval=[y[i] for i in valid_ids]
    # Threshold selected on validation balanced accuracy, never on holdout.
    best=(.5,-1)
    for k in range(20,81,2):
        th=k/100; tp=sum(1 for pp,yy in zip(valps,ysval) if pp>=th and yy); tn=sum(1 for pp,yy in zip(valps,ysval) if pp<th and not yy)
        pos=sum(ysval); neg=len(ysval)-pos; score=(tp/pos+tn/neg)/2 if pos and neg else -1
        if score>best[1]: best=(th,score)
    ps=holdps; ys=[y[i] for i in test_ids]
    brier=sum((pp-yy)**2 for pp,yy in zip(ps,ys))/len(ys)
    ll=-sum(yy*math.log(max(1e-8,pp))+(1-yy)*math.log(max(1e-8,1-pp)) for pp,yy in zip(ps,ys))/len(ys)
    auc=auc_score(ps,ys); auc_ci=auc_block_ci(ps,ys,block=21,n_boot=120,seed=31); holdout_rate=sum(ys)/len(ys); baseline_brier=holdout_rate*(1-holdout_rate)
    predicted=[int(pp>=best[0]) for pp in ps]; tp=sum(1 for pp,yy in zip(predicted,ys) if pp and yy); fp=sum(1 for pp,yy in zip(predicted,ys) if pp and not yy); fn=sum(1 for pp,yy in zip(predicted,ys) if not pp and yy); tn=len(ys)-tp-fp-fn
    precision=tp/(tp+fp) if tp+fp else None; recall=tp/(tp+fn) if tp+fn else None
    promoted=(len(test_ids)>=100 and brier<baseline_brier and auc is not None and auc>.5 and auc_ci is not None and auc_ci[0]>.5)
    model={"version":2,"symbol":symbol,"instrument_type":asset_type,"status":"champion" if promoted else "challenger_not_promoted","label":"forward 21-session maximum drawdown >= 8%","horizon":21,"features":["return_1d","momentum_5d","momentum_20d","momentum_60d","volatility_5d","volatility_20d","drawdown_20d","price_vs_sma20"],"means":means,"scales":scales,**model_core,"candidate_validation_brier":candidates,"selected_by":"lower validation Brier score","threshold":best[0],"trained_through":rows[train_ids[-1]][0],"validation_period":[rows[valid_ids[0]][0],rows[valid_ids[-1]][0]],"holdout_period":[rows[test_ids[0]][0],rows[test_ids[-1]][0]],"holdout":{"rows":len(test_ids),"positive_rate":holdout_rate,"brier":brier,"baseline_brier":baseline_brier,"log_loss":ll,"auc":auc,"auc_block_bootstrap_95ci":auc_ci,"precision":precision,"recall":recall,"false_positives":fp,"false_negatives":fn,"beats_constant_base_rate":brier<baseline_brier},"promotion_reason":"Passed untouched holdout: Brier beats constant-rate baseline and the 95% block-bootstrap AUC lower bound exceeds 0.50." if promoted else "Not promoted: the model must beat the constant-rate Brier baseline and have a 95% block-bootstrap AUC lower bound above 0.50 on at least 100 holdout rows. No risk probability will be served as a validated call.","purge_sessions":gap,"created_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"data_as_of":dates[-1],"limitations":"Single-symbol daily price-only downside classifier. Neural candidate is compared only when sufficient observations exist for non-overlapping purged splits; otherwise logistic and boosted-stump candidates are compared. No fundamentals/news/factor data in training. Holdout estimates may be noisy and are not evidence of profitable trading."}
    db=intel_db(); run_id=__import__("uuid").uuid4().hex
    try:
        db.execute("INSERT INTO asset_model_runs(run_id,symbol,status,model_json,created_at,data_as_of) VALUES(?,?,?,?,?,?)",(run_id,symbol,model["status"],json.dumps(model),model["created_at"],dates[-1]))
        # Failed challengers never overwrite an existing champion.
        if promoted:
            db.execute("INSERT INTO asset_models(symbol,instrument_type,model_json,created_at,data_as_of) VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET instrument_type=excluded.instrument_type,model_json=excluded.model_json,created_at=excluded.created_at,data_as_of=excluded.data_as_of",(symbol,asset_type,json.dumps(model),model["created_at"],dates[-1]))
        db.commit()
    finally: db.close()
    return model

def _asset_model(symbol):
    db=intel_db()
    try: row=db.execute("SELECT model_json FROM asset_models WHERE symbol=?",(symbol,)).fetchone()
    finally: db.close()
    return json.loads(row[0]) if row else None


def _latest_asset_run(symbol):
    db=intel_db()
    try: row=db.execute("SELECT model_json FROM asset_model_runs WHERE symbol=? ORDER BY created_at DESC LIMIT 1",(symbol,)).fetchone()
    finally: db.close()
    return json.loads(row[0]) if row else None


def _asset_model_probability(model, dates, closes):
    if len(closes)<61: return None
    if model.get("model_type")=="neural_ensemble":
        return brain_risk_now(model["neural_model"],dates,closes)
    ret=_returns_from_prices(closes); hist=closes
    def sd(a):
        z=ret[-a:]; m=sum(z)/len(z); return math.sqrt(sum((v-m)**2 for v in z)/max(1,len(z)-1))
    feat=[ret[-1],sum(ret[-5:]),sum(ret[-20:]),sum(ret[-60:]),sd(5),sd(20),hist[-1]/max(hist[-20:])-1,hist[-1]/(sum(hist[-20:])/20)-1]
    x=[max(-8,min(8,(feat[j]-model["means"][j])/model["scales"][j])) for j in range(len(feat))]
    if model.get("model_type")=="gradient_boosted_stumps":
        z=model["base_logit"]+sum(st["left"] if x[st["feature"]]<=st["threshold"] else st["right"] for st in model.get("stumps",[]))
    else: z=model["weights"][0]+sum(model["weights"][j+1]*x[j] for j in range(len(x)))
    z=model["calibration"]["intercept"]+model["calibration"]["slope"]*z
    return 1/(1+math.exp(-max(-30,min(30,z))))

def _asset_model_drift(model, dates, closes):
    if len(closes)<61: return {"available":False,"status":"insufficient history"}
    if model.get("model_type")=="neural_ensemble":
        row=brain_feature_rows(dates,closes)[-1]; feat=select_features(row,model["neural_model"].get("features",BRAIN_FEATURES)); means=model["neural_model"]["mean"]; scales=model["neural_model"]["scale"]
    else:
        ret=_returns_from_prices(closes)
        def sd(a):
            z=ret[-a:]; m=sum(z)/len(z); return math.sqrt(sum((v-m)**2 for v in z)/max(1,len(z)-1))
        feat=[ret[-1],sum(ret[-5:]),sum(ret[-20:]),sum(ret[-60:]),sd(5),sd(20),closes[-1]/max(closes[-20:])-1,closes[-1]/(sum(closes[-20:])/20)-1]; means=model["means"]; scales=model["scales"]
    z=[(feat[i]-means[i])/(scales[i] or 1) for i in range(len(feat))]
    names=model.get("features",[])
    flagged=[names[i] if i<len(names) else "feature_"+str(i) for i,v in enumerate(z) if abs(v)>=3]
    return {"available":True,"max_abs_standardized_deviation":max(abs(v) for v in z),"features_beyond_3sd":flagged,"status":"review_required" if flagged else "within_3sd_screen","method":"Point-in-time standardized feature screen; not a formal population-stability test."}


def _record_ndx_prediction(dates, closes, verdict):
    if not dates or not closes or verdict.get("risk_score") is None: return
    db=intel_db(); asof=dates[-1]; prob=float(verdict["risk_score"]); threshold=float(verdict.get("alarm_level") or .2)
    ident="NDX:"+asof+":"+str(BRAIN_JOB.get("message",""))[:20]
    try:
        db.execute("INSERT OR IGNORE INTO prediction_ledger(id,symbol,as_of,model_version,probability,threshold,horizon,label,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(ident,"NASDAQ100",""+asof,"ndx-brain-v2",prob,threshold,BRAIN_HORIZON,"8pct_drawdown",datetime.now(timezone.utc).isoformat(timespec="seconds")))
        # Grade only when the complete future horizon is present in the official close series.
        rows=db.execute("SELECT id,as_of,horizon FROM prediction_ledger WHERE symbol='NASDAQ100' AND outcome IS NULL").fetchall(); pos={d:i for i,d in enumerate(dates)}
        for row in rows:
            i=pos.get(row["as_of"])
            if i is not None and i+int(row["horizon"])<len(closes):
                outcome=int(min(closes[i+1:i+int(row["horizon"])+1])/closes[i]-1<=-BRAIN_DROP)
                db.execute("UPDATE prediction_ledger SET outcome=?,graded_at=? WHERE id=?",(outcome,datetime.now(timezone.utc).isoformat(timespec="seconds"),row["id"]))
        db.commit()
        record_learning_event("prediction_ledger","risk_forecast","NASDAQ100",{"as_of":asof,"probability":prob,"threshold":threshold,"horizon":BRAIN_HORIZON,"label":"8pct_drawdown"},observed_at=asof+"T23:59:00+00:00")
        graded_now=db.execute("SELECT COUNT(*) FROM prediction_ledger WHERE symbol='NASDAQ100' AND graded_at>=?",(datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0]
        if graded_now: record_learning_event("prediction_ledger","matured_outcomes","NASDAQ100",{"graded_today":graded_now})
    finally: db.close()


def _record_asset_prediction(symbol, dates, closes, model, probability):
    if not dates or probability is None: return
    ident=symbol+":"+dates[-1]+":"+str(model.get("created_at",""))
    db=intel_db()
    try:
        db.execute("INSERT OR IGNORE INTO prediction_ledger(id,symbol,as_of,model_version,probability,threshold,horizon,label,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(ident,symbol,dates[-1],"asset-model-v"+str(model.get("version",1)),float(probability),float(model.get("threshold",.5)),21,"8pct_drawdown",datetime.now(timezone.utc).isoformat(timespec="seconds")))
        rows=db.execute("SELECT id,as_of,horizon FROM prediction_ledger WHERE symbol=? AND outcome IS NULL",(symbol,)).fetchall(); pos={d:i for i,d in enumerate(dates)}
        for row in rows:
            i=pos.get(row["as_of"])
            if i is not None and i+int(row["horizon"])<len(closes):
                outcome=int(min(closes[i+1:i+int(row["horizon"])+1])/closes[i]-1<=-.08)
                db.execute("UPDATE prediction_ledger SET outcome=?,graded_at=? WHERE id=?",(outcome,datetime.now(timezone.utc).isoformat(timespec="seconds"),row["id"]))
        db.commit()
        record_learning_event("prediction_ledger","asset_risk_forecast",symbol,{"as_of":dates[-1],"probability":float(probability),"threshold":float(model.get("threshold",.5)),"horizon":21,"label":"8pct_drawdown"},observed_at=dates[-1]+"T23:59:00+00:00")
    finally: db.close()


def _governance_payload():
    db=intel_db()
    try:
        rows=[dict(r) for r in db.execute("SELECT * FROM prediction_ledger ORDER BY as_of DESC LIMIT 1000").fetchall()]
        models=[dict(r) for r in db.execute("SELECT symbol,instrument_type,created_at,data_as_of,model_json FROM asset_models ORDER BY symbol").fetchall()]
        runs=[dict(r) for r in db.execute("SELECT symbol,status,created_at,data_as_of,model_json FROM asset_model_runs ORDER BY created_at DESC LIMIT 100").fetchall()]
    finally: db.close()
    graded=[r for r in rows if r["outcome"] is not None]
    brier=sum((r["probability"]-r["outcome"])**2 for r in graded)/len(graded) if graded else None
    logloss=-sum(r["outcome"]*math.log(max(1e-8,r["probability"]))+(1-r["outcome"])*math.log(max(1e-8,1-r["probability"])) for r in graded)/len(graded) if graded else None
    bins=[]
    for lo in (0,.2,.4,.6,.8):
        subset=[r for r in graded if lo<=r["probability"]<lo+.2]
        bins.append({"range":[lo,lo+.2],"count":len(subset),"mean_probability":sum(r["probability"] for r in subset)/len(subset) if subset else None,"observed_rate":sum(r["outcome"] for r in subset)/len(subset) if subset else None})
    return {"predictions_recorded":len(rows),"predictions_graded":len(graded),"brier":brier,"log_loss":logloss,"calibration_bins":bins,"asset_models":[{"symbol":m["symbol"],"instrument_type":m["instrument_type"],"status":"champion","created_at":m["created_at"],"data_as_of":m["data_as_of"],"holdout":json.loads(m["model_json"]).get("holdout"),"holdout_period":json.loads(m["model_json"]).get("holdout_period")} for m in models],"challenger_runs":[{"symbol":m["symbol"],"status":m["status"],"created_at":m["created_at"],"data_as_of":m["data_as_of"],"holdout":json.loads(m["model_json"]).get("holdout"),"promotion_reason":json.loads(m["model_json"]).get("promotion_reason")} for m in runs],"champion_policy":"A challenger is stored only after a chronological, purged holdout report is created. This release does not auto-promote challengers based on in-sample scores.","note":"Metrics are unavailable until sufficient predictions mature. Historical model holdout metrics are reported separately from prospective live-ledger metrics."}


def _portfolio_save(workspace, holdings):
    if not isinstance(holdings,list) or len(holdings)>50: raise ValueError("Holdings must be a list of at most 50 symbols.")
    clean={}
    for h in holdings:
        sym=str(h.get("symbol","")).upper().strip()
        if not re.fullmatch(r"[A-Z0-9.-]{1,12}",sym): raise ValueError("Invalid portfolio ticker: "+sym)
        qty=_safe_float(h.get("quantity"),"Quantity",0)
        if qty<=0: raise ValueError("Holding quantity must be greater than zero.")
        clean[sym]=qty
    db=intel_db(); now=datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        db.execute("DELETE FROM portfolios WHERE workspace=?",(workspace,))
        db.executemany("INSERT INTO portfolios(workspace,symbol,quantity,updated_at) VALUES(?,?,?,?)",[(workspace,s,q,now) for s,q in clean.items()]); db.commit()
    finally: db.close()
    return {"saved":True,"holdings":len(clean),"updated_at":now}


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
        if path=="/api/health":
            body=json.dumps({"ok":True,"server_time":datetime.now(timezone.utc).isoformat(timespec="seconds"),"service":"MIRA API","persistence":{"provider":"supabase_storage_backup","configured":_SUPABASE_SYNC_STATUS["configured"],"restored":_SUPABASE_SYNC_STATUS["restored"],"last_backup":_SUPABASE_SYNC_STATUS["last_backup"],"error":_SUPABASE_SYNC_STATUS["error"]}}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/universe":
            try: body=json.dumps(asset_universe_payload()).encode()
            except Exception as exc: body=json.dumps({"indices":[],"equities":[],"error":str(exc)[:160]}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/asset-chart":
            try:
                q=dict(p.split("=",1) for p in urlsplit(self.path).query.split("&") if "=" in p)
                sym=q.get("symbol",""); typ=q.get("type","equity"); days=int(q.get("days","180"))
                body=json.dumps(asset_chart_payload(sym,typ,days)).encode()
                status=200
            except Exception as exc:
                body=json.dumps({"error":str(exc)[:200]}).encode(); status=400
            self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/research":
            try:
                q=dict(p.split("=",1) for p in urlsplit(self.path).query.split("&") if "=" in p)
                _json_response(self,research_bundle(q.get("symbol","")))
            except Exception as exc: _json_response(self,{"error":str(exc)[:240]},400)
            return
        if path=="/api/theses":
            try: _json_response(self,_workspace_theses(_workspace(self)))
            except Exception as exc: _json_response(self,{"error":str(exc)[:180]},400)
            return
        if path=="/api/thesis-evidence":
            try:
                q=dict(p.split("=",1) for p in urlsplit(self.path).query.split("&") if "=" in p)
                _json_response(self,_thesis_evidence(_workspace(self),str(q.get("symbol","")).upper()))
            except Exception as exc: _json_response(self,{"error":str(exc)[:180]},400)
            return
        if path=="/api/portfolio":
            try: _json_response(self,_portfolio_payload(_workspace(self)))
            except Exception as exc: _json_response(self,{"error":str(exc)[:180]},400)
            return
        if path=="/api/governance":
            try: _json_response(self,_governance_payload())
            except Exception as exc: _json_response(self,{"error":str(exc)[:180]},500)
            return
        if path=="/api/learning":
            try: _json_response(self,learning_status_payload())
            except Exception as exc: _json_response(self,{"error":str(exc)[:180]},500)
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
            body=json.dumps({**ENGINE_STATUS,"learning":{"enabled":True,"training_running":bool(BRAIN_JOB.get("running")),"training_message":BRAIN_JOB.get("message"),"trained_through":(brain_load() or {}).get("trained_through"),"last_official_close":PRICE_STATE.get("last_csv_date")},"persistence":{"provider":"supabase_storage_backup","configured":_SUPABASE_SYNC_STATUS["configured"],"restored":_SUPABASE_SYNC_STATUS["restored"],"last_backup":_SUPABASE_SYNC_STATUS["last_backup"],"error":_SUPABASE_SYNC_STATUS["error"]}}).encode()
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
                # Workspace records are isolated by a random browser workspace ID; credentials and model training remain owner-only.
                if path not in ("/api/agent","/api/theses","/api/portfolio","/api/model/train"):
                    raise PermissionError("This control is only available on the owner's own computer.")
                if path=="/api/agent" and not chat_rate_ok(self): raise PermissionError("Too many questions in a minute; please wait a little and ask again.")
                if path in ("/api/theses","/api/portfolio") and not rate_ok(self,"workspace-write",20): raise PermissionError("Workspace write limit reached. Please wait a minute.")
                if path=="/api/model/train" and not rate_ok(self,"model-train",1): raise PermissionError("Model training is limited to one run per visitor per minute.")
            if path=="/api/theses":
                ws=_workspace(self)
                if payload.get("delete_id"):
                    db=intel_db()
                    try:
                        db.execute("DELETE FROM thesis_evidence WHERE thesis_id=? AND workspace=?",(str(payload["delete_id"]),ws))
                        db.execute("DELETE FROM theses WHERE id=? AND workspace=?",(str(payload["delete_id"]),ws)); db.commit()
                    finally: db.close()
                    result={"deleted":True}
                else: result=_save_thesis(ws,payload)
            elif path=="/api/portfolio": result=_portfolio_save(_workspace(self),payload.get("holdings",[]))
            elif path=="/api/model/train":
                symbol=str(payload.get("symbol","")).upper().strip()
                typ=str(payload.get("type","equity")).lower().strip()
                aliases={"NASDAQ-100":"NASDAQ100","NDX":"NASDAQ100","^NDX":"NASDAQ100","^NDX100":"NASDAQ100","SPX":"SP500","^GSPC":"SP500","^SPX":"SP500","DJI":"DJIA","^DJI":"DJIA"}
                symbol=aliases.get(symbol,symbol)
                if not re.fullmatch(r"[A-Z0-9.-]{1,12}",symbol): raise ValueError("Enter a valid ticker or one of the supported indices: NASDAQ100, SP500, DJIA.")
                if typ not in ("index","equity"):
                    typ="index" if symbol in ASSET_INDEX_SERIES else "equity"
                # The UI's current explorer filter can be stale when a user types a stock ticker.
                # Canonical supported index symbols win; any other valid symbol is treated as an equity request.
                if symbol in ASSET_INDEX_SERIES: typ="index"
                elif typ=="index": typ="equity"
                if typ=="index":
                    if symbol not in ASSET_INDEX_SERIES: raise ValueError("Unsupported index. Choose NASDAQ100, SP500 or DJIA, or select Stocks & ETFs for a ticker.")
                    if symbol=="NASDAQ100":
                        job=start_brain_job(force=True)
                        result={"trained":False,"started":bool(job.get("started")),"workflow":"nasdaq100_neural_brain","message":job.get("message"),"job":job}
                    else:
                        rows=_fred_series_all(ASSET_INDEX_SERIES[symbol],1000)
                        if len(rows)<260: raise ValueError("FRED returned fewer than 260 daily observations for "+symbol+"; model training was not started.")
                        dates=[x[0] for x in rows]; closes=[x[1] for x in rows]
                        result={"trained":True,"model":_train_asset_model(symbol,typ,dates,closes)}
                else:
                    d=asset_chart_payload(symbol,"equity",1000); dates=d["dates"]; closes=d["closes"]
                    result={"trained":True,"model":_train_asset_model(symbol,typ,dates,closes)}
            elif path=="/api/market-data/config":
                key=str(payload.get("key","")).strip(); secret=str(payload.get("secret","")).strip()
                if payload.get("clear") is True:
                    MARKET_API_KEY=MARKET_API_SECRET=""; MARKET_CACHE={"at":0.0,"data":None}; clear_market_credentials()
                    result={"connected":False,"configured":False,"message":"Market-data credentials cleared from memory and the local encrypted store."}
                elif not key and not secret:
                    if not MARKET_API_KEY or not MARKET_API_SECRET: raise ValueError("Enter both Alpaca credentials first, or use Forget saved keys to remove stored credentials.")
                    snapshot=fetch_market_snapshot(force=True)
                    if not snapshot.get("connected"): raise ValueError(snapshot.get("error","Could not reconnect using the saved credentials."))
                    result={"connected":True,"message":"Reconnected using credentials saved in the local encrypted store.","snapshot":snapshot}
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
                    result={"connected":True,"message":("Connected. Credentials are saved encrypted on this Windows account." if stored else "Connected for this session, but secure local storage was unavailable; keys will be lost when the server stops."),"snapshot":snapshot}
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
    threading.Thread(target=price_updater_loop,daemon=True).start(); print("Live price updater started: official closes every 30 min (5 min while the market is open).")
    threading.Thread(target=paper_watch_loop,daemon=True).start(); print("Paper take-profit/stop-loss watcher started: checks every 5 seconds while the market is open.")
    threading.Thread(target=macro_updater_loop,daemon=True,name="mira-macro-refresh").start()
    threading.Thread(target=thesis_monitor_loop,daemon=True,name="mira-thesis-monitor").start()
    def learning_supervisor_loop():
        # A low-cost supervisor coordinates existing collectors; provider limits and market calendars still determine actual data cadence.
        while True:
            try:
                if not BRAIN_JOB.get("running"):
                    dates,closes=load_price_series(); brain=brain_load()
                    if brain is None or len(closes)-int(brain.get("checked_rows",0))>=BRAIN_GAP:
                        start_brain_job(False)
                # The price updater owns actual market refresh cadence; this loop records only operational status.
                LEARNING_RUNTIME["last_supervisor_cycle"]=datetime.now(timezone.utc).isoformat(timespec="seconds")
                LEARNING_RUNTIME["cycles"]+=1
            except Exception as exc:
                LEARNING_RUNTIME["last_error"]=str(exc)[:160]
            time.sleep(60)
    threading.Thread(target=learning_supervisor_loop,daemon=True,name="mira-learning-supervisor").start()
    port=int(os.environ.get("PORT",8765))
    print(f"Python model server running on port {port}")
    QuietServer(("0.0.0.0",port),Handler).serve_forever()
