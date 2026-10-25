# Historical data sources and coverage

The project bundles four source files under this folder. The download date was 2026-10-24.

- `nasdaq100_daily.csv`: daily Nasdaq-100 closes from 1986-01-02 through the latest date in the file, from the FRED series NASDAQ100. FRED identifies Nasdaq, Inc. as the source and notes the series is copyrighted. [FRED NASDAQ100](https://fred.stlouisfed.org/series/NASDAQ100)
- `nasdaq_composite_daily.csv`: daily Nasdaq Composite closes from 1971-02-05 onward, from FRED series NASDAQCOM. This is a separate, broader index. [FRED NASDAQCOM](https://fred.stlouisfed.org/series/NASDAQCOM)
- `sp500_monthly.csv`: monthly S&P 500/Shiller history beginning 1871, provided by the datasets/s-and-p-500 repository and sourced from Robert Shiller with recent price extension from FRED. Some post-Shiller extension columns are zero; use the SP500 price field for the drawdown chronology. [Dataset repository and methodology](https://github.com/datasets/s-and-p-500), [Shiller's source description](https://www.econ.yale.edu/~shiller/data.htm)
- `us_recession_monthly.csv`: monthly USREC recession indicator beginning 1854. FRED documents its recession shading as based on NBER turning points. [FRED USREC](https://fred.stlouisfed.org/series/USREC), [NBER chronology](https://www.nber.org/research/data/us-business-cycle-expansions-and-contractions)

## Generated files

Run `python build_history.py` to generate `historical_stress_events.csv`, `historical_data_manifest.json`, and `nasdaq100_training_features.csv`. A market drawdown event is mechanically defined here as a peak-to-trough loss of at least 20%, measured separately at each source frequency. NBER recession periods are separate event types; a recession is not automatically a market crash. This is not an exhaustive catalogue of every crash globally or in all asset classes.

Run `python train_historical.py` to train and save two Python neural networks using the bundled Nasdaq-100 daily history: next-session direction and a 10% drawdown risk within the next 20 sessions. Features are trailing returns/volatility (and volume change if the uploaded source contains volume). Labels use future closes only. The holdout is the latest 20% of labeled rows; weights are then refit on the full labeled history and saved to `models/nasdaq100_model.json`.

## Important boundary

Nasdaq-100 price history does not extend to 1871. The older S&P 500 series provides historical U.S. equity context, not backfilled Nasdaq-100 training examples. The model's BUY BIAS / SELL-DEFENSIVE BIAS / HOLD output is a thresholded research signal. It is not personalized advice, a calibrated probability, an order, or evidence of future profit. Backtests are vulnerable to overfitting; a single chronological holdout is not enough to establish an edge.
