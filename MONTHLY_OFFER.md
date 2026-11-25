# MIRA $50/month personal research workflow

**Draft for owner review before sharing.** MIRA is a prototype research tool. Do not promise a trading edge, model accuracy, data availability, investment returns, or continuous uptime. Confirm the data scope and payment arrangement before work begins.

## Offer

**$50 USD per month, month to month.** One personal research workflow, set up around one primary public company and up to three related public tickers, with one monthly review and a concise source-linked handoff.

The workflow records the client's research questions, thesis, assumptions, and invalidation conditions. MIRA can collect configured SEC and Finnhub evidence about every six hours while its server process is awake. Market calculations for a selected ticker run when its chart is loaded and use the latest available daily bars; after the market closes, they describe the most recent completed session. The monthly human-reviewed note summarizes what changed, source dates, gaps, and next questions.

This site does not process payments or auto-renew subscriptions. Agree on the monthly arrangement directly before work begins. Do not collect card details in MIRA.

## Copy to send

> I’m building MIRA into a personal research workflow for someone’s company watchlist. For $50 per month, I’ll set it up around one main public company and up to three related tickers, keep source-linked filings and available news organized, and prepare one plain-language review each month showing what changed, the data dates, and the open questions. The current forecast model has not shown an edge over its baseline, so this is for research organization—not a trading signal or investment advice. News is stored as evidence; it does not train the forecast model. Market calculations use the latest available daily prices, and provider data is included only where its terms allow. If that fits your workflow, we can agree on the watchlist and first research question before starting.

## Monthly workflow

1. Set up the agreed public tickers, research questions, and thesis assumptions.
2. Confirm that each data provider permits the intended use. If a source's terms are unclear, leave that data out.
3. Review source-linked filings and news, when configured, and state the retrieval dates.
4. Load the selected chart to calculate descriptive price statistics from its latest available daily bars. Do not describe those statistics as next-session odds.
5. Prepare a concise monthly note: what changed, evidence for and against the thesis, data gaps, and next questions.
6. Ask the client what was useful and update the workflow scope for the next month.

## Current limitations to explain plainly

- The dashboard screenshot's 77.6% result is for a 21-session drawdown-risk classifier, not next-session direction; it was below the displayed 87.1% no-alert baseline.
- News and SEC records are saved as evidence, not forecast-model training features. Live headlines do not change model weights.
- Chart calculations are refreshed on request. The saved-thesis monitor checks evidence about every six hours only while the host is awake; a free Render web service can sleep and is not a 24/7 scheduler.
- Background per-symbol daily-bar calculations are coded but disabled by default. They can be enabled only after written provider clearance for scheduled queries, derived calculations, and storage: `MIRA_SCHEDULED_ALPACA_CALCS_OK=1` for Alpaca equities/ETFs and `MIRA_SCHEDULED_FRED_CALCS_OK=1` for FRED indices.
- No automatic email or push alerts are implemented. There is no overnight live quote in this workflow; if a chart is loaded while markets are closed, calculations use the latest completed daily bar.
- No client logins, payment processor, automatic renewal, or private portfolio access are part of this offer.
- Market/news display, storage, or derived analytics are included only after the relevant provider permits the specific use. If rights are unclear, use permitted public filings only.

## Monthly handoff outline

- Client's research question and review date
- What changed since last month, with source links and publication dates
- Facts that support or challenge the thesis
- Price-history calculations with period and latest bar date, if permitted
- Assumptions, uncertainties, and missing data
- Next questions to investigate
- Research-only statement; no BUY/SELL call, return target, or personal recommendation
