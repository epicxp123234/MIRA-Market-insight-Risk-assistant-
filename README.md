# MIRA — Market Insight & Risk Assistant

A local research dashboard with MIRA, a small experimental Python neural-network forecast, historical Nasdaq data, a visual network activity sphere, and an optional local language-model agent.

## Start the dashboard

From VS Code, open a terminal in this folder and run:

```powershell
python server.py
```

Open http://127.0.0.1:8765. Alternatively, run `start.bat`.

The dashboard has client and operator views. Client view gives a concise market brief; operator view adds the animated sphere, actual weighted-layer diagram and activations, live task/tool trace, forecast probabilities, 20-session drawdown-risk estimate, and chronological holdout metrics. The network diagram updates from the saved model and snapshots during Python training. Use **Run fresh evaluation** to train and score both classifiers again. You may upload a dated daily-price CSV with a close column.

## Free live quote feed

The **Live market data · QQQ** panel uses Alpaca Basic's free IEX feed for real-time, single-venue QQQ quotes. During the overnight session the free `overnight` feed provides real-time indicative quotes and 15-minute delayed trades. It polls the local Python server every five seconds. Alpaca API key ID and secret can be entered in the panel; the server keeps them in memory only and sends them over HTTPS to `data.alpaca.markets`. They are not written to disk or displayed in the dashboard. Restarting the server clears them. The free feed is not the consolidated all-exchange SIP tape.

QQQ is an ETF proxy for the Nasdaq-100. The live quote display is separate from the existing daily-trained neural forecast; connecting live quotes does not make that model an intraday or pre-market predictor. Nasdaq equities are not active all the time: the free overnight session operates five days a week and markets close on weekends and exchange holidays.

The sphere renders the selected model’s actual learned weights and forward-pass activations as signed paths and signal pulses; it is a visualization, not a literal brain scan or a display of private chain-of-thought. Forecast probabilities are uncalibrated. The latest chronological holdout did not beat a majority-class direction baseline. A recall-focused stress threshold catches more labeled events at the cost of many false alarms. MIRA reports that tradeoff; it does not promise 80% accuracy and should be treated as research only.

## Local reasoning model

The agent panel accepts natural-language goals and can call bounded local tools to inspect data, run the predictor, inspect evaluation output, and explore synthetic sandbox cases. It does not have a general web browser, arbitrary shell access, brokerage access, or bank-system access.

Install Ollama from [the official Windows download](https://ollama.com/download/windows), then run:

```powershell
ollama pull qwen3.5:4b
```

The language model is a separate pretrained model used for explanations and choosing bounded tools. It does not make the numerical market forecast and is not trained from scratch by this project.

## Historical inputs

Bundled data covers Nasdaq-100 daily closes from 1986, Nasdaq Composite daily closes from 1971, S&P 500/Shiller monthly history from 1871, and U.S. recession labels from 1854. These are separate assets and frequencies; they do not represent every global financial crisis. See [data/SOURCES.md](data/SOURCES.md) for sources and label definitions. The 20% drawdown episodes are mechanical peak-to-trough measurements.

## Future company-integrity module

The page shows a planned module for reviewing owner-authorized sample logs, schemas, and test fixtures. It is inactive and does not scan production systems. A real deployment would need scoped data access, privacy controls, and evaluation against labeled incidents and false-positive costs.
