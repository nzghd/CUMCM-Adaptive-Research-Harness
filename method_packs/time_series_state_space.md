# Time Series / State Space

**Use when:** order, dynamics, seasonality, hidden state or forecasting horizon
matter.

**Formulation:** define sampling interval, state/observation equations or lag
structure, innovations and forecasting target.

**Baseline:** naive/seasonal-naive forecast.

**Required diagnostics:** chronological split, rolling-origin evaluation,
stationarity/transform checks, residual autocorrelation, horizon-specific error,
parameter stability and uncertainty intervals.

**Common traps:** random train/test split; future feature leakage; fitting trend on
the full series; one-step performance used to claim long-horizon accuracy.
