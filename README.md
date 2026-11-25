# MIRA — Market Insight & Risk Assistant

A local research dashboard with MIRA, an experimental Python neural-network research models, historical Nasdaq data, a visual network activity sphere, and an optional local language-model agent.

## Start the dashboard

From VS Code, open a terminal in this folder and run:

```powershell
python server.py
```

Open http://127.0.0.1:8765. Alternatively, run `start.bat`.

The dashboard has client and operator views. Client view gives a concise market brief; operator view adds the animated sphere, actual weighted-layer diagram and activations, live task/tool trace, forecast probabilities, 20-session drawdown and volatility-regime estimates, and chronological holdout metrics. It trains three independently seeded MLPs for each of three tasks (direction, drawdown risk, and elevated-volatility regime) and averages their probabilities. The network diagram shows representative learned weights and the ensemble output. Use **Run fresh evaluation** to retrain and score these models; additional members do not guarantee better accuracy. You may upload a dated daily-price CSV with a close column.

## Free live quote feed

The **Live market data · QQQ** panel uses Alpaca Basic's free IEX feed for real-time, single-venue QQQ quotes. During the overnight session the free `overnight` feed provides real-time indicative quotes and 15-minute delayed trades. It polls the local Python server every five seconds. Alpaca API key ID and secret can be entered in the panel; the server sends them over HTTPS to `data.alpaca.markets`. On Windows, they are also encrypted with the current user account’s DPAPI and stored in a small SQLite file under `%LOCALAPPDATA%\MIRA\credentials.sqlite3`. They are never returned to the browser. Use **Forget saved keys** to clear the encrypted store. If Windows DPAPI is unavailable, keys work for the current session only. The free feed is not the consolidated all-exchange SIP tape.

QQQ is an ETF proxy for the Nasdaq-100. The live quote display is separate from the existing daily-trained neural forecast; connecting live quotes does not make that model an intraday or pre-market predictor. Nasdaq equities are not active all the time: the free overnight session operates five days a week and markets close on weekends and exchange holidays.

The sphere renders the selected model’s actual learned weights and forward-pass activations as signed paths and signal pulses; it is a visualization, not a literal brain scan or a display of private chain-of-thought. Forecast probabilities are uncalibrated. The current one-period chronological holdout did not beat a majority-class direction baseline. A recall-focused stress threshold catches more labeled events at the cost of many false alarms. MIRA reports that tradeoff; it does not promise 80% accuracy and should be treated as research only.

## Local reasoning model

The Ollama status badge reports whether the local service and the configured model are both available. If it is offline, start Ollama; if the model is missing, run `ollama pull qwen3.5:4b`. The agent panel accepts natural-language goals and can call bounded local tools to inspect data, run the predictor, inspect evaluation output, and explore synthetic sandbox cases. It does not have a general web browser, arbitrary shell access, brokerage access, or bank-system access.

Install Ollama from [the official Windows download](https://ollama.com/download/windows), then run:

```powershell
ollama pull qwen3.5:4b
```

The language model is a separate pretrained model used for explanations and choosing bounded tools. It does not make the numerical market forecast and is not trained from scratch by this project.

## Historical inputs

Bundled data covers Nasdaq-100 daily closes from 1986, Nasdaq Composite daily closes from 1971, S&P 500/Shiller monthly history from 1871, and U.S. recession labels from 1854. These are separate assets and frequencies; they do not represent every global financial crisis. See [data/SOURCES.md](data/SOURCES.md) for sources and label definitions. The 20% drawdown episodes are mechanical peak-to-trough measurements.

## News and SEC event-learning path

MIRA now stores the source event time separately from the time the server first captured an item. Legacy evidence rows are left without a fabricated capture time; only newly observed, timestamped Finnhub articles and SEC filings can enter the event-label path. SEC filing metadata uses EDGAR's acceptance timestamp when supplied. The current features are intentionally simple and inspectable: prior daily-price returns/volatility, keyword counts from Finnhub headline/summary or SEC filing description, filing-form flags, and accession-matched as-reported XBRL values where available (revenue growth, income margin, liabilities/assets, and operating-cash-flow margin). MIRA does **not** yet parse full filing text or claim to understand news like an analyst.

When enabled, a background job (about every six hours while the server is awake) grades each eligible event against five-session post-capture-close direction. It fits a shadow logistic challenger and compares it with price-only and training-majority baselines across three purged expanding walk-forward windows. Evaluation waits for at least 100 test symbol-days, 30 distinct test dates, three symbols, and all three folds. Results are diagnostics only; this first version never changes the live call, and a good score is not proof of a dependable edge. The 14-day Finnhub fetch is not a historical point-in-time archive, so the sample must accumulate prospectively before a meaningful evaluation exists.

The job is **off by default**. Set these Render/local environment flags only after the data contracts or written approvals cover this exact use, including model training, derived labels, retention, and any customer-facing display:

- `MIRA_EVENT_LABELS_ALPACA_OK=1` — permits Alpaca IEX daily closes to be retained as event-outcome labels.
- `MIRA_FINNHUB_NEWS_MODEL_OK=1` — permits Finnhub news to enter feature training.
- `MIRA_SEC_EVENT_MODEL_OK=1` — permits SEC filing metadata to enter feature training.

All three default to off. The challenger uses Alpaca for equity price labels and does not use FRED. FRED's published terms prohibit ML training and storage of FRED content; SEC data access must use an identifying User-Agent and observe SEC fair-access limits. See [FRED's current terms](https://fred.stlouisfed.org/legal/terms/), [SEC EDGAR API guidance](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), [SEC fair access](https://www.sec.gov/about/webmaster-frequently-asked-questions), [Finnhub commercial plans](https://api.finnhub.io/pricing-startups-and-enterprise), and [Alpaca's redistribution guidance](https://alpaca.markets/support/redistribute-alpaca-api). An API key by itself is not a commercial data license.

## Future company-integrity module

The page shows a planned module for reviewing owner-authorized sample logs, schemas, and test fixtures. It is inactive and does not scan production systems. A real deployment would need scoped data access, privacy controls, and evaluation against labeled incidents and false-positive costs.

## Commercial readiness

MIRA is currently a research prototype, not a validated trading edge or institutional production service. The owner reports that they described the intended use when requesting a FRED API key and were issued a key; the scope of any separate permission still needs to be checked against the written response and the underlying series rights. Alpaca's feed also has commercial display/redistribution restrictions. See [COMMERCIAL_READINESS.md](COMMERCIAL_READINESS.md) for the measured model results, provider terms, and release gates.

## $50/month workflow preview

The month-to-month personal research workflow is described in [pilot.html](pilot.html), with the internal offer and monthly handoff outline in [MONTHLY_OFFER.md](MONTHLY_OFFER.md) and a source-linked example at [sample-brief.html](sample-brief.html). It costs $50/month and currently relies on a human-reviewed monthly handoff; the page does not process or automatically renew payments. The workflow is for research organization, not a validated prediction or investment-advice service. News and market data are included only if their terms allow the intended use.
