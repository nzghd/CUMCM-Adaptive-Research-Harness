# Dynamic Programming

**Use when:** optimal substructure and a finite/controlled state representation
exist.

**Formulation:** define state, action, transition, stage cost, boundary condition
and recurrence. Prove the state contains all information needed for future costs.

**Baseline:** brute force on a tiny instance.

**Required diagnostics:** state-space size, discretization error, recurrence replay,
complexity, memory, terminal conditions and comparison with brute force.

**Common traps:** non-Markov state, double counting, missing boundary states,
discretization presented as exact, and exponential state growth hidden by notation.
