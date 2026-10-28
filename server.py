from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json, math, random, csv, io, time, os, sqlite3, ctypes
from contextlib import closing
from datetime import date, datetime, time as dt_time, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
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
    # FIXED: Bypasses Windows library calls on Render's Linux servers
    if os.name != "nt": 
        return data  # Safely returns the plain string/bytes on the cloud
        
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
    global MARKET_CACHE
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
 {"type":"function","function":{"name":"explore_simulation","description":"Run 100 new generated cases in the simulated bank or market environment. Never accesses external systems.","parameters":{"type":"object","properties":{},"required":[]}}}
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
    MODEL=full_up
    NASDAQ_RESULT={"instrument":saved["instrument"],"as_of":saved["as_of"],"signal":signal,"next_session_up_probability":round(p_up,5),"next20_session_10pct_drawdown_risk":round(p_risk,5),"next20_session_high_volatility_probability":round(p_volatility,5),"holdout_direction_accuracy":up["accuracy"],"holdout_direction_balanced_accuracy":direction_balanced,"direction_majority_baseline":up_base,"holdout_direction_precision":up["precision"],"holdout_direction_recall":up["recall"],"holdout_direction_fpr":up["fpr"],"holdout_direction_fnr":up["fnr"],"direction_confusion":up["counts"],"holdout_stress_accuracy":risk["accuracy"],"holdout_stress_balanced_accuracy":stress_balanced,"stress_majority_baseline":risk_base,"stress_precision":risk["precision"],"stress_recall":risk["recall"],"stress_fpr":risk["fpr"],"stress_fnr":risk["fnr"],"stress_confusion":risk["counts"],"volatility_regime_holdout_accuracy":volatility["accuracy"],"volatility_regime_balanced_accuracy":(volatility["recall"]+1-volatility["fpr"])/2,"volatility_regime_baseline_accuracy":max(sum(o["y"] for o in test_vol),len(test_vol)-sum(o["y"] for o in test_vol))/len(test_vol),"stress_threshold":stress_threshold,"stress_threshold_validation_f2":threshold_selection["f2"],"stress_validation_precision":threshold_selection.get("validation_precision"),"stress_validation_recall":threshold_selection.get("validation_recall"),"validation_rows":len(raw_validation),"holdout_rows":len(raw_test),"training_rows":len(raw_fit),"purged_boundary_rows":20,"validation_best_epochs":{"direction":direction_best_epochs,"stress":stress_best_epochs,"volatility_regime":volatility_best_epochs},"historical_nasdaq100_20pct_drawdowns":episode_count,"target":"up/down: next close; 20-session 10% drawdown; elevated forward volatility relative to trailing volatility","learning_trace":logs,"holdout_trace":up["trace"][-30:],"saved_model":"data/models/nasdaq100_model.json","probability_warning":"Experimental ensemble outputs; not calibrated, not a trade instruction."}
    confusion=lambda counts:{"true_positive":counts["tp"],"false_positive":counts["fp"],"true_negative":counts["tn"],"false_negative":counts["fn"]}
    vol_base=max(sum(o["y"] for o in test_vol),len(test_vol)-sum(o["y"] for o in test_vol))/len(test_vol)
    report={"model_name":saved["model_name"],"instrument":saved["instrument"],"data_as_of":saved["as_of"],"trained_on":datetime.now().date().isoformat(),"signal":signal,"next_session_up_probability":round(p_up,5),"next20_session_10pct_drawdown_risk":round(p_risk,5),"next20_session_high_volatility_probability":round(p_volatility,5),"direction":{"holdout_accuracy":up["accuracy"],"majority_baseline_accuracy":up_base,"balanced_accuracy":direction_balanced,"precision":up["precision"],"recall":up["recall"],"false_positive_rate":up["fpr"],"false_negative_rate":up["fnr"],"confusion":confusion(up["counts"])},"stress":{"holdout_accuracy":risk["accuracy"],"majority_baseline_accuracy":risk_base,"balanced_accuracy":stress_balanced,"precision":risk["precision"],"recall":risk["recall"],"false_positive_rate":risk["fpr"],"false_negative_rate":risk["fnr"],"confusion":confusion(risk["counts"]),"decision_threshold":stress_threshold,"threshold_selection":"F2 on separate chronological validation block","validation_precision":threshold_selection.get("validation_precision"),"validation_recall":threshold_selection.get("validation_recall")},"volatility_regime":{"holdout_accuracy":volatility["accuracy"],"majority_baseline_accuracy":vol_base,"balanced_accuracy":(volatility["recall"]+1-volatility["fpr"])/2,"precision":volatility["precision"],"recall":volatility["recall"],"false_positive_rate":volatility["fpr"],"false_negative_rate":volatility["fnr"],"confusion":confusion(volatility["counts"]),"target":"next 20-session realized volatility exceeds 1.35 times the trailing 20-session realized volatility"},"train_rows":len(raw_fit),"validation_rows":len(raw_validation),"holdout_rows":len(raw_test),"purged_boundary_rows":20,"validation_best_epochs":{"direction":direction_best_epochs,"stress":stress_best_epochs,"volatility_regime":volatility_best_epochs},"ensemble_members_per_task":3,"features":["1/2/5/10/20-session returns","5/20-session volatility","volume change if provided"],"target_definitions":{"direction":"next recorded close higher than current close","stress":"any close in next 20 sessions at least 10% below current close"},"crash_event_counts":{"nasdaq100_daily_20pct_drawdowns":episode_count},"training_method":"Three independently seeded neural networks per task; ensemble predictions average member probabilities. Each member uses validation-loss early stopping and the final serving ensemble is refit using selected epoch counts.","limitation":"One chronological holdout after a 20-session purge; the stress decision threshold is selected on a separate validation block to favor recall. Probabilities are not calibrated. Research output only."}
    (model_dir/"nasdaq100_training_report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
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

def _fred_rows(last_date):
    start=(date.fromisoformat(last_date)-timedelta(days=10)).isoformat()
    req=Request("https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQ100&cosd="+start,headers={"User-Agent":"MIRA-research/1.0"})
    with urlopen(req,timeout=10) as response: text=response.read().decode("utf-8-sig")
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
        if not force and time.time()-PRICE_STATE["checked_at"]<600: return PRICE_STATE
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

def price_data_note():
    dates,_=load_price_series(); csv_last=PRICE_STATE.get("last_csv_date") or dates[-1]; et=eastern_now()
    ex=PRICE_STATE.get("extra") or []; note="Data: official daily closes through %s"%csv_last
    if ex:
        live=ex[-1][0]; is_today=(live==et.date().isoformat() and market_session(et)!="closed" and et.time().replace(tzinfo=None)<dt_time(16))
        note+=", plus %d day%s estimated from live QQQ prices through %s%s"%(len(ex),"" if len(ex)==1 else "s",live," (that trading day is not finished, so this estimate can still change)" if is_today else "")
    elif PRICE_STATE["alpaca"]=="not connected": note+=" (connect free Alpaca keys on the Live market data panel to include today's live move)"
    if PRICE_STATE.get("updated"): note+="; auto-update last ran %s (official closes: %s)"%(PRICE_STATE["updated"].replace("T"," "),PRICE_STATE["fred"])
    lag=(et.date()-date.fromisoformat(dates[-1])).days
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
    extra=intraday_features(); base=brain_feature_rows(closes); ys=brain_labels(closes)
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
    AUTOPILOT["last_run"]=datetime.now().isoformat(timespec="seconds")

def price_updater_loop():
    while True:
        try: refresh_prices(force=True); autopilot_once()
        except Exception as exc: AUTOPILOT["note"]="Autopilot error: "+str(exc)[:100]
        time.sleep(300 if market_session()=="regular session" else 1800)

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
BRAIN_JOB={"running":False,"message":"","error":None,"started":None}
BRAIN_LOCK=threading.Lock()

def brain_path(): return DATA_DIR/"models"/"mira_risk_brain.json"

def brain_load():
    try: return json.loads(brain_path().read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return None

def brain_feature_rows(closes):
    n=len(closes); daily=[0.0]+[closes[i]/closes[i-1]-1 for i in range(1,n)]
    cs=[0.0]
    for v in closes: cs.append(cs[-1]+v)
    def sd(i,k):
        seg=daily[i-k+1:i+1]; m=sum(seg)/k; return math.sqrt(sum((x-m)**2 for x in seg)/k)
    rows=[None]*n
    for i in range(BRAIN_WARMUP,n):
        v5,v20,v60=sd(i,5),sd(i,20),sd(i,60)
        rows[i]=[daily[i],closes[i]/closes[i-2]-1,closes[i]/closes[i-5]-1,closes[i]/closes[i-10]-1,closes[i]/closes[i-20]-1,v5,v20,closes[i]/closes[i-60]-1,v60,closes[i]/((cs[i+1]-cs[i-49])/50)-1,closes[i]/((cs[i+1]-cs[i-249])/250)-1,closes[i]/max(closes[i-251:i+1])-1,(v5/v60) if v60>0 else 1.0]
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

def brain_split(closes):
    feats=brain_feature_rows(closes); ys=brain_labels(closes)
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
    return {"rows":len(ids),"auc":auc_score(ps,yv),"base_rate":sum(yv)/len(yv),"flagged_drop_rate":(sum(flagged)/len(flagged)) if flagged else None,"calm_drop_rate":(sum(calm)/len(calm)) if calm else None,"flagged_share":len(flagged)/len(ids),"strategy":strat,"buy_and_hold":hold,"invested_share":sum(1 for i in ids if pos[i]==1.0)/len(ids)}

_LABEL_CACHE={}
def brain_labels_cache(closes):
    key=(len(closes),closes[-1])
    if key not in _LABEL_CACHE: _LABEL_CACHE.clear(); _LABEL_CACHE[key]=brain_labels(closes)
    return _LABEL_CACHE[key]

def train_brain(dates,closes,hidden=8):
    visible_status("preparing","Neural risk brain: building 13 price features and 21-day risk labels.",0.02)
    feats,ys,idx,fit_ids,val_ids,hold_ids=brain_split(closes)
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
    return {"version":2,"created":datetime.now().isoformat(timespec="seconds"),"trained_through":dates[-1],"rows_at_training":len(closes),"checked_rows":len(closes),"hidden":hidden,"features":BRAIN_FEATURES,"horizon_days":BRAIN_HORIZON,"drop":BRAIN_DROP,"threshold":best_thr,"mean":s_mean,"scale":s_scale,"members":serving,"validation_epochs":epochs,"eval":{"mean":mean,"scale":scale,"members":members,"holdout_start":dates[hold_ids[0]],"holdout_end":dates[hold_ids[-1]]},"holdout":holdout,"history":[]}

def brain_risk_now(brain,closes):
    x=brain_feature_rows(closes)[-1]
    return predict(brain_ensemble(brain["members"]),[(v-brain["mean"][j])/brain["scale"][j] for j,v in enumerate(x)])

def brain_save(brain):
    path=brain_path(); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(brain),encoding="utf-8")

def brain_job(force=False):
    """Train the first brain, or retrain on newer data and promote the new version only if it holds up on unseen days."""
    try:
        dates,closes=load_price_series(); champ=brain_load()
        if champ is None:
            BRAIN_JOB["message"]="Training the first neural risk brain..."
            brain=train_brain(dates,closes,8); brain["history"]=[{"date":dates[-1],"event":"first brain trained","holdout_auc":brain["holdout"]["auc"]}]
            brain_save(brain); BRAIN_JOB["message"]="First brain trained."
        elif not force and len(closes)-champ.get("checked_rows",0)<21:
            BRAIN_JOB["message"]="Brain is up to date."
        else:
            BRAIN_JOB["message"]="Self-improvement: training challengers on newer data..."
            order=list(BRAIN_HIDDEN_CHOICES); alt=order[(order.index(champ["hidden"])+1)%len(order)] if champ["hidden"] in order else 12
            feats,ys,idx,_,_,hold_ids=brain_split(closes)
            ce=champ["eval"]; p_champ=brain_predict_ids(brain_ensemble(ce["members"]),ce["mean"],ce["scale"],feats,hold_ids)
            yh=[ys[i] for i in hold_ids]; champ_auc=auc_score([p_champ[i] for i in hold_ids],yh)
            results=[]
            for hidden,kind,tol in ((champ["hidden"],"refresh on newer data",-0.015),(alt,"alternative size (%d hidden units)"%alt,0.01)):
                cand=train_brain(dates,closes,hidden); results.append((cand,kind,tol,cand["holdout"]["auc"]))
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
    return {"trained":b is not None,"trained_through":(b or {}).get("trained_through"),"alarm_level":(b or {}).get("threshold"),"holdout":(b or {}).get("holdout"),"history":(b or {}).get("history",[])[-5:],"job":dict(BRAIN_JOB)}

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
    out={"as_of":dates[-1],"data_note":price_data_note(),"hourly_note":intraday_note(h),"horizon_days":h,"horizon_label":label,"horizon_defaulted":defaulted,"horizon_all":everyday,"trend_verdict":trend_verdict,"trend_rule":{"average_days":n,"band_pct":band*100},"distance_from_average_pct":round(dist*100,2),"distance_from_50day_pct":round(d50*100,2),"self_check":learn_note,"brain":None}
    if brain is None:
        started=start_brain_job(False)
        out.update({"verdict":trend_verdict,"confidence":"Low","interim":True,"brain_job":started["message"]}); return out
    p=brain_risk_now(brain,closes); thr=brain["threshold"]; brain_verdict="SELL" if p>=thr else "BUY"
    margin=((p-thr)/thr) if brain_verdict=="SELL" else ((thr-p)/thr)
    level=2 if margin>=0.5 else (1 if margin>=0.2 else 0)
    agree=(brain_verdict==trend_verdict)
    if not agree: level=max(0,level-1)
    cap=0 if h<=5 else (1 if h<=21 else (2 if h<=63 else 0))
    verdict=brain_verdict
    if h>63: verdict="BUY"       # beyond ~3 months no timing signal has worked well; the brain's warning is shown instead
    live=update_ledger(dates,closes,brain_verdict,("Low","Medium","High")[level],p,thr)
    out.update({"verdict":verdict,"brain_verdict":brain_verdict,"confidence":("Low","Medium","High")[min(level,cap)],"interim":False,"agree":agree,"risk_score":p,"alarm_level":thr,"brain":{"trained_through":brain["trained_through"],"hidden":brain["hidden"],"holdout":brain["holdout"],"holdout_start":brain["eval"]["holdout_start"],"holdout_end":brain["eval"]["holdout_end"],"history":brain.get("history",[])[-3:]},"live_record":live,"job":dict(BRAIN_JOB)})
    return out

def build_final_answer(v):
    pct=lambda x:"%.1f%%"%(x*100); p0=lambda x:"n/a" if x is None else "%.0f%%"%(x*100); sg=lambda x:("%+.1f%%" if abs(x)<0.1 else "%+.0f%%")%(x*100)
    h=v["horizon_days"]; a=v["horizon_all"]; n=v["trend_rule"]["average_days"]
    parts=["Time frame: %s.%s"%(v["horizon_label"]," You did not name one, so I used about a month; ask about tomorrow or next year for a different view." if v["horizon_defaulted"] else "")]
    if v["interim"]:
        parts.append("My neural risk brain is not trained yet. I have started training it now (%s). Check the live network panel; it takes a few minutes. Ask again when it finishes."%v["brain_job"])
        parts.append("Until then, the simple trend rule gives an interim view: %s (the index is %.1f%% %s its %d-day average). This is NOT the neural network's call."%(v["trend_verdict"],abs(v["distance_from_average_pct"]),"above" if v["distance_from_average_pct"]>=0 else "below",n))
        return " ".join(parts)+"\n\nSUMMARY | INTERIM (brain training) | Trend rule: %s | Data as of %s"%(v["trend_verdict"],v["as_of"])
    parts.append("FINAL: BUY - stay invested in the Nasdaq-100." if v["verdict"]=="BUY" else "FINAL: SELL - get defensive (reduce exposure or move to cash).")
    parts.append("Confidence: %s."%v["confidence"])
    state="above" if v["risk_score"]>=v["alarm_level"] else "below"
    parts.append("Why: my neural networks put the chance of a drop of 8%% or more within about a month at %s. My alarm level is %s, and the score is %s it, so the networks say %s."%(p0(v["risk_score"]),p0(v["alarm_level"]),state,v["brain_verdict"]))
    parts.append("Second opinion: the simple trend rule says %s (the index is %.1f%% %s its %d-day average and %.1f%% %s its 50-day average), so the two %s."%(v["trend_verdict"],abs(v["distance_from_average_pct"]),"above" if v["distance_from_average_pct"]>=0 else "below",n,abs(v["distance_from_50day_pct"]),"above" if v["distance_from_50day_pct"]>=0 else "below","agree" if v["agree"] else "disagree, so I lowered my confidence"))
    b=v["brain"]; ho=b["holdout"]; s=ho["strategy"]; bh=ho["buy_and_hold"]
    if ho.get("auc") is not None:
        txt="How good the brain is: on days it never saw during training (%s to %s), its risk score separated stormy from calm periods with a score of %.2f (0.50 would be a coin flip)."%(b["holdout_start"],b["holdout_end"],ho["auc"])
        if ho.get("flagged_drop_rate") is not None and ho.get("calm_drop_rate") is not None:
            txt+=" When it raised the alarm, a drop of 8%%+ followed %s of the time, versus %s when it did not."%(p0(ho["flagged_drop_rate"]),p0(ho["calm_drop_rate"]))
        if s and bh: txt+=" Following its calls in that test earned %s a year versus %s for buy-and-hold, with a worst drop of %.0f%% versus %.0f%%."%(pct(s["cagr"]),pct(bh["cagr"]),s["max_drawdown"]*100,bh["max_drawdown"]*100)
        parts.append(txt)
    if a:
        parts.append("For this time frame, the index has historically been higher %s of the time and typically moved %s, but in the worst 5%% of cases it fell %s or more."%(p0(a["p_up"]),sg(a["median"]),("%.1f%%" if abs(a["p5"])<0.1 else "%.0f%%")%(abs(a["p5"])*100)))
    if h<=1: parts.append("Tomorrow is close to a coin flip for any method, so a day trader should not lean on this call; the brain looks about a month ahead.")
    elif h<=5: parts.append("A week ahead is still mostly noise, so treat this as a mild lean only.")
    elif h>63:
        parts.append("Over a year or longer the market has gone up most of the time, even after warnings, so no timing signal has worked well at this range; staying invested has historically beaten trying to time exits.")
        if v["brain_verdict"]=="SELL": parts.append("Note: the brain's risk alarm is currently on, so consider adding gradually instead of all at once.")
    r=v["live_record"]["graded"]
    if r["n"]>=1: parts.append("Live record: %d call%s graded, the risk call was right %s of the time (always saying BUY would be right %s)."%(r["n"],"" if r["n"]==1 else "s",pct(r["hit_rate"]),pct(r["always_buy_rate"])))
    else: parts.append("Live record: %d call%s logged; each is graded after 1 day and after a month against what the market really did."%(v["live_record"]["calls_logged"],"" if v["live_record"]["calls_logged"]==1 else "s"))
    hist=b.get("history") or []
    if hist:
        last=hist[-1]
        if last.get("event")=="self-improvement check": parts.append("Self-improvement: last check on %s, %s."%(last["date"],"a retrained brain was promoted" if last.get("promoted") else "the current brain was kept because challengers did not qualify"))
        else: parts.append("Self-improvement: the brain was first trained on data through %s; it retrains on new data about every 21 trading days and only replaces itself if it holds up on days it has not seen."%b["trained_through"])
    if v["job"].get("running"): parts.append("(A background retraining job is running now.)")
    if v.get("hourly_note"): parts.append(v["hourly_note"])
    if v.get("data_note"): parts.append(v["data_note"])
    parts.append("This is a research tool, not financial advice.")
    line="SUMMARY | Time frame: %s | FINAL: %s | Confidence: %s | Risk score %s vs alarm %s | Trend rule: %s | Brain test score %s | Data as of %s"%(v["horizon_label"],v["verdict"],v["confidence"],p0(v["risk_score"]),p0(v["alarm_level"]),v["trend_verdict"],("%.2f"%ho["auc"]) if ho.get("auc") is not None else "n/a",v["as_of"])
    return " ".join(parts)+"\n\n"+line

def answer_decision(prompt):
    visible_status("tool_call","MIRA chat: neural risk brain is scoring today's market and grading past calls.")
    v=final_verdict(prompt)
    log_question(prompt,v["verdict"])
    answer=build_final_answer(v)
    visible_status("observation","FINAL %s (%s confidence) as of %s"%(v["verdict"],v["confidence"],v["as_of"]))
    visible_status("complete","Answer ready: FINAL "+v["verdict"],1)
    return {"answer":answer,"summary":v,"actions":[{"tool":"neural_risk_brain","observation":{k:v.get(k) for k in ("verdict","confidence","horizon_label","as_of","risk_score","alarm_level","trend_verdict","interim")}}],"model":"MIRA neural risk brain (self-improving)"}

def ollama_chat(messages):
    body=json.dumps({"model":OLLAMA_MODEL,"messages":messages,"tools":TOOLS,"stream":False,"think":False,"options":{"num_predict":128,"temperature":0.1}}).encode()
    req=Request("http://127.0.0.1:11434/api/chat",data=body,headers={"Content-Type":"application/json"})
    with urlopen(req,timeout=300) as response: return json.loads(response.read())["message"]
def run_agent(payload):
    global ACTIVE_CSV, ACTIVE_TARGET, TASK
    ACTIVE_CSV=payload.get("csv",""); ACTIVE_TARGET=payload.get("target",""); TASK=payload.get("task","bank")
    prompt=payload.get("prompt","").strip()
    if not prompt: raise ValueError("Enter a goal for the sandbox agent.")
    if is_decision_question(prompt): return answer_decision(prompt)
    visible_status("interpreting","Reading your request and selecting from bounded local tools; private chain-of-thought is not exposed.")
    system=("You are the reasoning core of a local research agent. Understand the user's natural-language goal and use provided Python tools when useful. For ANY question about MIRA market forecast accuracy, market false-positive/false-negative rates, or market holdout metrics, you MUST call inspect_market_evaluation and use its returned values. Never use explore_simulation to answer a real market-model metric question. You may inspect the uploaded labeled dataset, train/evaluate the local neural predictor, inspect evaluation output, and explore the generated simulation when asked. Never claim to contact the internet, a real bank, a brokerage, or an exchange. Treat predictions as experiments, not financial advice. Give concise conclusions grounded in tool results. Do not reveal hidden chain-of-thought; report brief plans, actions, observations, and uncertainty.")
    messages=[{"role":"system","content":system},{"role":"user","content":prompt}]
    actions=[]
    for round_index in range(6):
        visible_status("reasoning","Local model is interpreting the request (round %d/6); internal chain-of-thought is private."%(round_index+1))
        message=ollama_chat(messages)
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
            else: result={"error":"Tool is not allowlisted."}
            compact={k:v for k,v in result.items() if k not in ("trace","logs","holdout_trace","learning_trace")}
            actions.append({"tool":name,"observation":compact})
            visible_status("observation","Tool "+name+" returned "+json.dumps(compact,ensure_ascii=False)[:180])
            messages.append({"role":"tool","tool_name":name,"content":json.dumps(compact)})
    answer="Stopped after six tool rounds. Review the action log and narrow the goal if more work is needed."
    visible_status("complete","Agent finished the bounded task; answer and tool observations are in the dashboard.",1)
    return {"answer":answer,"actions":actions,"model":OLLAMA_MODEL}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(ROOT),**kwargs)
    def do_GET(self):
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
        if path=="/api/brain":
            body=json.dumps(brain_status()).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/intraday":
            body=json.dumps({k:v for k,v in INTRADAY_STATE.items()}|{"profile":intraday_profile()}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            return
        if path=="/api/prices":
            body=json.dumps({k:v for k,v in PRICE_STATE.items() if k!="extra"}|{"live_days":[(d,round(v,2),s) for d,v,s in PRICE_STATE["extra"]],"note":price_data_note(),"autopilot":AUTOPILOT}).encode()
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
            payload=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
            path=urlsplit(self.path).path
            if path=="/api/market-data/config":
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
            else: self.send_error(404); return
            body=json.dumps(result).encode(); code=200
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
    
    # --- FIXED FOR RENDER COMPATIBILITY ---
    # 1. Dynamically read Render's port environment variable, default to 8765 locally
    port = int(os.environ.get("PORT", 8765))
    
    print(f"Python model server running 24/7 on port {port}")
    
    # 2. Changed host from "127.0.0.1" to "0.0.0.0" so the network router can bind correctly
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
