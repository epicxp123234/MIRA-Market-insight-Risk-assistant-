"""Build reproducible historical stress labels and lagged features from bundled files."""
from pathlib import Path
import csv, json, math

ROOT=Path(__file__).resolve().parent
DATA=ROOT/"data"
EVENTS=[]

def read_rows(name):
    with (DATA/name).open(newline="",encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def price_rows(file,date_key,price_key):
    out=[]
    for row in read_rows(file):
        try:
            p=float(row[price_key])
            if p>0 and math.isfinite(p): out.append((row[date_key],p))
        except (ValueError,KeyError,TypeError): pass
    return out

def drawdowns(series,asset,frequency,threshold=.20):
    if not series: return
    peak_date,peak=series[0]
    active=False; trough_date=peak_date; trough=peak
    for date,price in series[1:]:
        if not active:
            if price>=peak: peak_date,peak=date,price
            elif price/peak-1<=-threshold:
                active=True; trough_date,trough=date,price
        else:
            if price<trough: trough_date,trough=date,price
            if price>=peak:
                EVENTS.append({"asset":asset,"frequency":frequency,"event_type":"peak-to-trough drawdown >=20%","start_date":peak_date,"trough_date":trough_date,"end_date":date,"max_drawdown_pct":round((trough/peak-1)*100,2),"label_source":"computed from price series"})
                peak_date,peak=date,price;active=False
    if active:
        EVENTS.append({"asset":asset,"frequency":frequency,"event_type":"open drawdown >=20%","start_date":peak_date,"trough_date":trough_date,"end_date":"","max_drawdown_pct":round((trough/peak-1)*100,2),"label_source":"computed from price series; unrecovered at sample end"})

def recession_events():
    rows=read_rows("us_recession_monthly.csv"); start=None; last=None
    for r in rows:
        date=r.get("observation_date","")
        try: flag=int(r.get("USREC","0"))
        except ValueError: flag=0
        if flag and start is None: start=date
        if flag: last=date
        elif start is not None:
            EVENTS.append({"asset":"United States economy","frequency":"monthly","event_type":"NBER recession indicator","start_date":start,"trough_date":"","end_date":last,"max_drawdown_pct":"","label_source":"FRED USREC, sourced from NBER chronology"})
            start=None;last=None
    if start is not None:
        EVENTS.append({"asset":"United States economy","frequency":"monthly","event_type":"NBER recession indicator (open)","start_date":start,"trough_date":"","end_date":"","max_drawdown_pct":"","label_source":"FRED USREC, sourced from NBER chronology"})

def vol(a):
    m=sum(a)/len(a);return math.sqrt(sum((x-m)**2 for x in a)/len(a))

def build_features():
    rows=read_rows("nasdaq100_daily.csv")
    prices=[]
    for r in rows:
        try:
            p=float(r["NASDAQ100"])
            if p>0: prices.append((r["observation_date"],p))
        except (ValueError,KeyError): pass
    closes=[p for _,p in prices];rets=[0.0]+[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    out=[]
    for i in range(20,len(closes)-20):
        f=[rets[i],closes[i]/closes[i-2]-1,closes[i]/closes[i-5]-1,closes[i]/closes[i-10]-1,closes[i]/closes[i-20]-1,vol(rets[i-4:i+1]),vol(rets[i-19:i+1])]
        risk=int(min(closes[i+1:i+21])<=closes[i]*.90)
        out.append({"date":prices[i][0],"ret_1d":f[0],"ret_2d":f[1],"ret_5d":f[2],"ret_10d":f[3],"ret_20d":f[4],"vol_5d":f[5],"vol_20d":f[6],"target_next_close_up":int(closes[i+1]>closes[i]),"target_next20d_drawdown10pct":risk})
    with (DATA/"nasdaq100_training_features.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(out[0]));w.writeheader();w.writerows(out)
    return len(out)

drawdowns(price_rows("sp500_monthly.csv","Date","SP500"),"S&P 500 / Shiller long-run proxy","monthly")
drawdowns(price_rows("nasdaq_composite_daily.csv","observation_date","NASDAQCOM"),"Nasdaq Composite","daily")
drawdowns(price_rows("nasdaq100_daily.csv","observation_date","NASDAQ100"),"Nasdaq-100","daily")
recession_events()
with (DATA/"historical_stress_events.csv").open("w",newline="",encoding="utf-8") as f:
    fields=["asset","frequency","event_type","start_date","trough_date","end_date","max_drawdown_pct","label_source"]
    w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(sorted(EVENTS,key=lambda e:e["start_date"]))
features=build_features()
metadata={
 "built_from":["sp500_monthly.csv","nasdaq_composite_daily.csv","nasdaq100_daily.csv","us_recession_monthly.csv"],
 "crash_definition":"At least 20% peak-to-trough decline, computed separately for each index and data frequency; these are episodes in the supplied series, not a complete list of all world financial crises.",
 "nasdaq100_features_rows":features,
 "event_rows":len(EVENTS),
 "nasdaq100_feature_targets":{"target_next_close_up":"next recorded daily close exceeds current close","target_next20d_drawdown10pct":"at least one close during the next 20 sessions is 10% or more below current close"},
 "created_by":"build_history.py"
}
(DATA/"historical_data_manifest.json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")
print(json.dumps(metadata,indent=2))
