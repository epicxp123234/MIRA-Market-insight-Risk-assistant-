from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json, math, random, csv, io, time, os
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
 {"type":"function","function":{"name":"forecast_nasdaq","description":"Train/evaluate next-session direction and 20-session drawdown-risk models on bundled Nasdaq-100 daily history, or on supplied historical prices. Produces an experimental latest-data signal.","parameters":{"type":"object","properties":{},"required":[]}}},
 {"type":"function","function":{"name":"explore_simulation","description":"Run 100 new generated cases in the simulated bank or market environment. Never accesses external systems.","parameters":{"type":"object","properties":{},"required":[]}}}
]

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
    examples=[{"x":features(i),"up":int(close[i+1]>close[i]),"risk":int(min(close[i+1:i+21])<=close[i]*.90),"id":prices[i][0].date().isoformat()} for i in range(20,len(close)-20)]
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
    valid_risk=scaled(raw_validation,"risk")
    def fit(data,epochs=55,label="direction",base=0.2,span=0.18):
        m={"w1":[[random.uniform(-.15,.15) for _ in range(9)] for _ in range(8)],"w2":[random.uniform(-.15,.15) for _ in range(9)]}
        logs=[];step=max(1,epochs//10)
        for ep in range(epochs):
            random.shuffle(data);train_pass(m,data,.008/(1+ep*.01))
            if ep%step==0 or ep==epochs-1:
                logs.append({"epoch":ep+1,"loss":cross_entropy(m,data)})
                visible_status("training","%s neural network · epoch %d/%d"%(label,ep+1,epochs),base+span*(ep+1)/epochs,{"label":label,"epoch":ep+1,"epochs":epochs,"model":m,"sample":data[-1]["x"]})
        return m,logs
    random.seed(17)
    visible_status("preparing","Building chronological examples and normalizing eight price features.",0.08)
    eval_up,logs=fit(train_up,label="direction validation fit",base=0.12,span=0.16)
    eval_risk,_=fit(train_risk,label="drawdown-risk validation fit",base=0.28,span=0.16)
    stress_threshold,threshold_selection=choose_threshold_f2(eval_risk,valid_risk) if valid_risk else (0.5,{"f2":None,"validation_positive_rows":0})
    up=evaluate(eval_up,test_up);risk=evaluate(eval_risk,test_risk,stress_threshold)
    up_base=max(sum(o["y"] for o in test_up),len(test_up)-sum(o["y"] for o in test_up))/len(test_up)
    risk_base=max(sum(o["y"] for o in test_risk),len(test_risk)-sum(o["y"] for o in test_risk))/len(test_risk)
    direction_balanced=(up["recall"]+1-up["fpr"])/2
    stress_balanced=(risk["recall"]+1-risk["fpr"])/2
    # Refit serving models with normalization from their full training history.
    full_mean=[sum(o["x"][j] for o in examples)/len(examples) for j in range(8)]
    full_scale=[math.sqrt(sum((o["x"][j]-full_mean[j])**2 for o in examples)/len(examples)) or 1 for j in range(8)]
    full_up,_=fit(scaled(examples,"up",full_mean,full_scale),label="direction final fit",base=0.47,span=0.20)
    full_risk,_=fit(scaled(examples,"risk",full_mean,full_scale),label="drawdown-risk final fit",base=0.68,span=0.20)
    latest=[(v-full_mean[j])/full_scale[j] for j,v in enumerate(features(len(close)-1))]
    p_up=predict(full_up,latest);p_risk=predict(full_risk,latest)
    if direction_balanced<=.52 or stress_balanced<=.52: signal="HOLD / NO EDGE DETECTED"
    elif p_up>=.55 and p_risk<.25: signal="BUY BIAS (RESEARCH)"
    elif p_up<=.45 or p_risk>=.35: signal="SELL / DEFENSIVE BIAS (RESEARCH)"
    else: signal="HOLD / UNCERTAIN"
    try:
        event_rows=list(csv.DictReader((DATA_DIR/"historical_stress_events.csv").open(encoding="utf-8")))
        episode_count=sum(1 for e in event_rows if e["asset"]=="Nasdaq-100" and "drawdown" in e["event_type"])
    except OSError: episode_count=0
    model_dir=DATA_DIR/"models";model_dir.mkdir(exist_ok=True)
    saved={"model_name":"MIRA — Market Insight & Risk Assistant","instrument":payload.get("instrument","Nasdaq-100"),"as_of":prices[-1][0].date().isoformat(),"features":["return_1d","return_2d","return_5d","return_10d","return_20d","volatility_5d","volatility_20d","volume_log_change"],"mean":full_mean,"scale":full_scale,"direction_model":full_up,"drawdown_risk_model":full_risk,"stress_threshold":stress_threshold,"train_rows":len(examples),"architecture":"8 inputs → 8 tanh units → sigmoid"}
    (model_dir/"nasdaq100_model.json").write_text(json.dumps(saved,indent=2),encoding="utf-8")
    MODEL=full_up
    NASDAQ_RESULT={"instrument":saved["instrument"],"as_of":saved["as_of"],"signal":signal,"next_session_up_probability":round(p_up,5),"next20_session_10pct_drawdown_risk":round(p_risk,5),"holdout_direction_accuracy":up["accuracy"],"holdout_direction_balanced_accuracy":direction_balanced,"direction_majority_baseline":up_base,"holdout_direction_precision":up["precision"],"holdout_direction_recall":up["recall"],"holdout_direction_fpr":up["fpr"],"holdout_direction_fnr":up["fnr"],"direction_confusion":up["counts"],"holdout_stress_accuracy":risk["accuracy"],"holdout_stress_balanced_accuracy":stress_balanced,"stress_majority_baseline":risk_base,"stress_precision":risk["precision"],"stress_recall":risk["recall"],"stress_fpr":risk["fpr"],"stress_fnr":risk["fnr"],"stress_confusion":risk["counts"],"stress_threshold":stress_threshold,"stress_threshold_validation_f2":threshold_selection["f2"],"stress_validation_precision":threshold_selection.get("validation_precision"),"stress_validation_recall":threshold_selection.get("validation_recall"),"validation_rows":len(raw_validation),"holdout_rows":len(raw_test),"training_rows":len(raw_fit),"purged_boundary_rows":20,"historical_nasdaq100_20pct_drawdowns":episode_count,"target":"up/down: next close; stress: >=10% drop within next 20 sessions","learning_trace":logs,"holdout_trace":up["trace"][-30:],"saved_model":"data/models/nasdaq100_model.json","probability_warning":"Experimental classifier output; not calibrated, not a trade instruction."}
    confusion=lambda counts:{"true_positive":counts["tp"],"false_positive":counts["fp"],"true_negative":counts["tn"],"false_negative":counts["fn"]}
    report={"model_name":saved["model_name"],"instrument":saved["instrument"],"data_as_of":saved["as_of"],"trained_on":datetime.now().date().isoformat(),"signal":signal,"next_session_up_probability":round(p_up,5),"next20_session_10pct_drawdown_risk":round(p_risk,5),"direction":{"holdout_accuracy":up["accuracy"],"majority_baseline_accuracy":up_base,"balanced_accuracy":direction_balanced,"precision":up["precision"],"recall":up["recall"],"false_positive_rate":up["fpr"],"false_negative_rate":up["fnr"],"confusion":confusion(up["counts"])},"stress":{"holdout_accuracy":risk["accuracy"],"majority_baseline_accuracy":risk_base,"balanced_accuracy":stress_balanced,"precision":risk["precision"],"recall":risk["recall"],"false_positive_rate":risk["fpr"],"false_negative_rate":risk["fnr"],"confusion":confusion(risk["counts"]),"decision_threshold":stress_threshold,"threshold_selection":"F2 on separate chronological validation block","validation_precision":threshold_selection.get("validation_precision"),"validation_recall":threshold_selection.get("validation_recall")},"train_rows":len(raw_fit),"validation_rows":len(raw_validation),"holdout_rows":len(raw_test),"purged_boundary_rows":20,"features":["1/2/5/10/20-session returns","5/20-session volatility","volume change if provided"],"target_definitions":{"direction":"next recorded close higher than current close","stress":"any close in next 20 sessions at least 10% below current close"},"crash_event_counts":{"nasdaq100_daily_20pct_drawdowns":episode_count},"limitation":"One chronological holdout after a 20-session purge; the stress decision threshold is selected on a separate validation period to favor recall. Probabilities are not calibrated. Research output only."}
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
def ollama_chat(messages):
    body=json.dumps({"model":OLLAMA_MODEL,"messages":messages,"tools":TOOLS,"stream":False,"think":False}).encode()
    req=Request("http://127.0.0.1:11434/api/chat",data=body,headers={"Content-Type":"application/json"})
    with urlopen(req,timeout=300) as response: return json.loads(response.read())["message"]
def run_agent(payload):
    global ACTIVE_CSV, ACTIVE_TARGET, TASK
    ACTIVE_CSV=payload.get("csv",""); ACTIVE_TARGET=payload.get("target",""); TASK=payload.get("task","bank")
    prompt=payload.get("prompt","").strip()
    if not prompt: raise ValueError("Enter a goal for the sandbox agent.")
    visible_status("interpreting","Reading your request and selecting from bounded local tools; private chain-of-thought is not exposed.")
    system=("You are the reasoning core of a local research agent. Understand the user's natural-language goal and use provided Python tools when useful. You may inspect the uploaded labeled dataset, train/evaluate the local neural predictor, inspect holdout metrics, and explore the generated simulation. Never claim to contact the internet, a real bank, a brokerage, or an exchange. You have no such tools. Treat predictions as experiments, not financial advice. Give concise conclusions grounded in tool results. Do not reveal hidden chain-of-thought; report brief plans, actions, observations, and uncertainty.")
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
            elif name=="explore_simulation": result=scan()
            elif name=="forecast_nasdaq": result=forecast_nasdaq({"csv":ACTIVE_CSV,"close_col":ACTIVE_TARGET,"instrument":"Nasdaq-100 (NDX/QQQ)"})
            else: result={"error":"Tool is not allowlisted."}
            compact={k:v for k,v in result.items() if k not in ("trace","logs")}
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
                body=json.dumps({"connected":True,"models":[m.get("name","") for m in tags.get("models",[])]}).encode()
            except Exception:
                body=json.dumps({"connected":False,"models":[]}).encode()
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
                if not key and not secret:
                    MARKET_API_KEY=MARKET_API_SECRET=""; MARKET_CACHE={"at":0.0,"data":None}
                    result={"connected":False,"configured":False,"message":"Market-data keys cleared from server memory."}
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
                    result={"connected":True,"message":"Connected to the free Alpaca market-data feed.","snapshot":snapshot}
            elif path=="/api/train": result=train(payload)
            elif path=="/api/scan": result=scan()
            elif path=="/api/agent": result=run_agent(payload)
            elif path=="/api/nasdaq": result=forecast_nasdaq(payload)
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
    print("Python model server at http://127.0.0.1:8765")
    ThreadingHTTPServer(("127.0.0.1",8765),Handler).serve_forever()
