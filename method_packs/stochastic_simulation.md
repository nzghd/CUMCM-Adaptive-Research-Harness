# Stochastic Process / Monte Carlo Simulation

**Use when:** uncertainty and random evolution are central or analytic integration
is impractical.

**Formulation:** specify random variables/process, dependence, distribution,
transition law, estimand and sampling scheme.

**Parameters:** distribution fitting, external priors or scenario design with
uncertainty.

**Baseline:** deterministic mean-case and simple analytic special case.

**Required diagnostics:** seed control, confidence intervals, effective sample
size, convergence versus sample count, rare-event handling, distributional
sensitivity and correlation alternatives.

**Common traps:** one seed; treating simulated frequency as exact probability;
assuming independence without testing; reporting no Monte Carlo uncertainty.
