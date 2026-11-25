# MIRA commercial-readiness review

**Reviewed:** 2026-11-25  
**Scope:** the `MIRA-push` checkout, its checked-in reports and data sources, and the supplied production screenshots. The screenshots and checked-in files are separate snapshots and are reported separately below.

## Decision

MIRA is not ready to be sold as a high-accuracy forecasting or trading product. The current evidence does not show a profitable or reliable directional edge, the 77.6% figure is not next-session direction accuracy, news and SEC records are not model inputs, and the bundled market data has unresolved commercial-use restrictions. Do not advertise an 88% result, fund-grade forecasts, or expected profits.

There is a credible product direction: an auditable equity research workbench that joins price context, filings/news, thesis tracking, risk scenarios, and a forecast scorecard. Its near-term value should come from faster, traceable analyst work. A predictive edge can become a claim only after a licensed point-in-time data pipeline and independent walk-forward evidence establish it.

## What the current numbers mean

### Production screenshots supplied on 2026-11-24

- The dashboard's **77.6%** is accuracy for a **21-session drawdown-risk classifier**, not next-session up/down accuracy.
- Its displayed no-alert baseline is **87.1%**. At that threshold the classifier is 9.5 percentage points below the baseline.
- **AUC 0.67** measures how well scores rank positive cases; it is not accuracy and does not cancel the baseline failure.
- The screenshot reports that 16% of realized 8% drawdowns were detected and that 15% of alerts were followed by a drawdown, versus a 13% base rate. That is a small lift with low event capture, not a demonstrated trading edge.
- The historical strategy simulation reports **13.4% annualized versus 19.3% for buy-and-hold**, with roughly the same **-36% maximum drawdown**. On those displayed figures the strategy loses to its benchmark on return without reducing the worst drawdown.
- The screenshot shows price data through 2026-11-19 but a model trained through 2026-10-23. The forecast model is therefore not trained through the latest displayed market data.

### Checked-in model report

`data/models/nasdaq100_training_report.json` is a different, older evaluation, dated 2026-11-01 and using data through 2026-10-29:

| Task | Holdout result | Simple baseline | What it says |
| --- | ---: | ---: | --- |
| Next-close direction | 55.85% accuracy; 49.90% balanced accuracy | 56.05% majority accuracy | No directional edge shown; accuracy is slightly below the majority rule. |
| 20-session 10% drawdown | 31.85% accuracy at a recall-focused threshold; 59.09% balanced accuracy | 91.80% majority accuracy | The selected threshold produces many alerts; it is unsuitable if presented as an accuracy win. |
| Elevated volatility | 79.95% accuracy; 52.38% balanced accuracy; 5.44% positive recall | 79.37% majority accuracy | Accuracy mostly reflects the common class; it misses most positive periods. |

These tasks have different labels and horizons. They must not be blended into one headline score. A score above 88% is not inherently useful: when positives are rare, an always-negative classifier can exceed 88% accuracy while detecting none of the events.

## What the running server has and has not learned

- Supabase is used as durable storage for the intelligence SQLite database and selected model/data files when the deployment has its Supabase URL and service key configured. Storage is persistence, not model training.
- The ingestion code stores Finnhub news, SEC filings, fundamentals, market observations, and forecast-ledger rows as evidence events. The current Nasdaq and symbol-specific model features are price-derived; the code describes news/filings as evidence memory until a point-in-time labeled corpus exists.
- The supplied production screenshot shows 357 evidence events, zero graded forecasts, and one pending forecast. Those observations do not yet form a supervised training set. “The server has been running” is not equivalent to learning from outcomes.
- The general Nasdaq trainer is trained on Nasdaq-100 price history. Typed symbols can receive a chart and descriptive calculations, but the symbol-specific trainer is a 21-session downside-risk model; it is not a validated next-session direction model for every symbol.
- The local app is a single-process Python/HTML research prototype. The README already notes that the free Render process may sleep or restart, so it does not provide a 24/7 service guarantee.

## Data-rights gate before a commercial release

The owner says they explained the intended use when requesting a FRED API key and were issued a key. I have not seen the application or response, so I cannot determine whether FRED granted a separate written permission for training, caching, or commercial distribution. The standard published terms say that API-key issuance provides API access but does not override data-owner restrictions; they also prohibit using FRED services/API for software or ML development/training and prohibit storing/caching API content. For third-party copyrighted series, FRED says to obtain permission from the data owner. If the owner has a separate written approval that expressly covers these activities and the NASDAQ100 series, retain it and confirm its scope with counsel before commercial release.

The rest of the current source chain also needs written clearance for this product use:

- [FRED's published terms](https://fred.stlouisfed.org/legal/) cover its API as well as its other services. [FRED's NASDAQ100 series](https://fred.stlouisfed.org/series/NASDAQ100) identifies Nasdaq, Inc. as the source and marks the series as copyrighted.
- [Nasdaq's market-data licensing page](https://www.nasdaq.com/products/global-indexes/data/gids) says data used to benchmark a fund, product, or strategy requires an appropriate license.
- [Alpaca's redistribution guidance](https://alpaca.markets/support/redistribute-alpaca-api) and customer terms do not make a free API credential a general right to resell market data.
- SEC EDGAR is a useful public filings source, but automated access must follow the [SEC's fair-access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data) and identify the software in its User-Agent. Public access alone does not provide a historical, point-in-time news/filings feature corpus or a validated signal.

Use a provider contract that specifically covers commercial display, derived analytics, model training, caching, retention, and redistribution. Keep provider attribution and per-source timestamps. Have counsel review the intended markets, customer type, performance statements, and whether any offered feature constitutes investment advice or research. For India, the [SEBI Investment Adviser Regulations](https://www.sebi.gov.in/legal/regulations/feb-2025/securities-and-exchange-board-of-india-investment-advisers-regulations-2013-last-amended-on-february-10-2025-_92319.html) and [SEBI's adviser FAQ](https://www.sebi.gov.in/sebi_data/faqfiles/aug-2025/1755174193178.pdf) are relevant starting points; classification depends on the service and audience. For the U.S., the SEC's [investment-adviser marketing guide](https://www.sec.gov/resources-small-businesses/small-business-compliance-guides/investment-adviser-marketing) explains restrictions on performance advertising by investment advisers; whether it applies depends on the business and offer.

FRED's published API terms also say an app offered to other users must link to its terms, explicitly state that use of the app means users agree to those terms, and publish an appropriate privacy policy. The UI now shows FRED's required non-endorsement notice and links to the terms, but MIRA does not yet have the explicit user agreement or public privacy policy needed for a commercial FRED API application.

## Release gates that would make the product credible

1. **Clear the data rights.** Replace restricted sources with a paid/licensed feed or written permissions covering model training, customer display, derived outputs, and storage. Record rights and expiry per dataset. For an authorized FRED API application, add the terms agreement and privacy policy required by its published API terms.
2. **Build point-in-time features.** Keep publication time, ingestion time, source, filing period, correction/revision history, symbol mapping, and feature version. Do not feed a model a filing before it was public. Start with an auditable news/filing event study; add those features to forecasting only if they beat a price-only benchmark on unseen dates.
3. **Evaluate one task at a time.** Use purged, expanding walk-forward windows across multiple market regimes and a fixed universe. Compare against majority/prevalence, simple momentum/trend, and buy-and-hold baselines. Report accuracy and balanced accuracy together with AUC, Brier/log loss, calibration, precision/recall, coverage, and uncertainty intervals.
4. **Test decisions after costs.** Include spread, commissions, slippage, borrow/financing where relevant, dividends, splits, and realistic execution timing. Publish net results alongside benchmark results; do not select a signal by repeatedly tuning against the final holdout.
5. **Set an evidence-based promotion gate.** A challenger should beat its benchmark on untouched chronological windows, improve probability quality, and show stable net results across assets/regimes. If it fails, show “no validated edge” and keep descriptive chart calculations clearly labeled as diagnostics.
6. **Make the analyst workflow the product.** Prioritize watchlists, portfolio exposure, cited news/filings, thesis and invalidation tracking, scenario comparisons, reproducible exports, and a clear audit trail. Pilot with a small number of buy-side/research teams before setting enterprise prices.
7. **Harden operations.** Add repeatable deployment, monitoring, backups/restores, access controls, tenant isolation, data retention controls, support expectations, and tested recovery from provider failure. A sleeping single process is not an institutional uptime promise.

## Practical positioning

Position the current build as a **research and model-monitoring prototype**. Do not call it a fund-grade predictor, autonomous trader, or “88% accurate” product. The current draft offer is **$50 USD per month, month to month**, for a personal workflow around one main public company and up to three related tickers, with one human-reviewed monthly handoff. It focuses on source-linked evidence and thesis questions; chart calculations run when requested and market/news data is included only where rights permit. The prototype has no client login, payment processor, or automatic renewal; the page explains that clearly. `pilot.html` is the client-facing scope, `MONTHLY_OFFER.md` is the internal monthly workflow, and `sample-brief.html` shows an illustrative hand-built SEC filing brief. This monthly workflow is not a forecasting or investment-advice service.

I reviewed public vendor descriptions from [Koyfin](https://www.koyfin.com/pricing/), [TradingView](https://www.tradingview.com/pricing/), [AlphaSense](https://prod.alpha-sense.com/pricing/), and [Bloomberg Professional](https://professional.bloomberg.com/products/bloomberg-terminal/). These products establish a high bar in market coverage, charting, research content, and institutional workflow. MIRA should differentiate with a small, demonstrably useful workflow and a transparent evidence trail instead of claiming to replace them.

## Work completed in this review

- Mapped the local forecast targets, chronological splits, model inputs, evidence ledger, Supabase snapshot code, and model-promotion flow.
- Compared the production screenshot with the checked-in holdout report and kept their metrics explicitly separate.
- Checked current primary-source data-use and performance-marketing pages.
- Recorded the owner's statement about explaining the FRED use when requesting the key, marked the approval scope as unverified in the repository, and added the published FRED non-endorsement notice to both app views.
- Recast the offer as a $50/month, month-to-month personal research workflow with a plain-language scope, manual payment disclosure, and monthly handoff. The sample remains clearly marked as manually prepared and does not imply the current app automatically creates that brief.
- Added a plain-language quick read for selected tickers and clarified in the thesis monitor that source collection does not train the forecast model. Updated dashboard links to say $50/month.
- Added an opt-in six-hour background calculation for saved tickers that reports the latest daily close and 20/60-session changes. It is off by default behind separate FRED and Alpaca environment flags until each provider has cleared the intended scheduled use and storage.
- Paused a fresh local retrain after finding that published FRED/Nasdaq terms restrict this use and before reviewing the owner's FRED approval correspondence. No model report or model file was changed by that interrupted run.
- Kept the existing client-view full-width fix in `index.html`; no production deploy, external sales contact, or publication was made.
