# Geometric / Kinematic Modeling

**Use when:** trajectories, visibility, interception, coverage, distance, angle,
rigid geometry or coordinate transforms dominate.

**Formulation:** define the reference frame, state vectors, time domain and
geometric sets before using point-to-line/segment/surface distances. Extended
objects must not be silently replaced by centroids.

**Parameters:** positions, velocities, radii, dimensions and time origins with
units and provenance.

**Baseline:** exact hand calculation for one static or one-dimensional case.

**Required diagnostics:** coordinate-frame replay, dimensional checks, boundary
cases, collision/visibility convention, discretization convergence for surfaces,
and structural comparison of point/axis/surface/volume representations.

**Common traps:** infinite line instead of segment/ray; wrong time origin; using
one representative point for an extended target; mixing coordinate systems;
ignoring occlusion direction.
