# Machine Learning

**Use when:** sufficient labeled/structured data exist and predictive performance
is central.

**Formulation:** define sample unit, features available at decision time, target,
loss and deployment distribution.

**Baseline:** constant/linear/tree baseline appropriate to the target.

**Required diagnostics:** leakage audit, nested or held-out evaluation, class
imbalance, calibration, feature ablation, uncertainty, shift sensitivity, multiple
seeds and interpretability appropriate to the decision.

**Common traps:** tiny data with large models; target leakage; hyperparameter tuning
on the test set; accuracy without calibration or domain relevance.
