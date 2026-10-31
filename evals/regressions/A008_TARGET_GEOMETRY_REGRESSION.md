# Regression: shared target-geometry worldview and false numerical bound

This regression reproduces two failure modes observed in a real rehearsal run.

## Case A — method diversity, one shared structural assumption

Candidates use geometry, continuous optimization, scheduling and combinatorial
optimization, but every candidate encodes `target_geometry=point_centroid`.
The diversity engine must report algorithm/method diversity **and** the shared
structural assumption. It must not claim independent problem interpretations.

## Case B — searched maximum mislabeled as rigorous upper bound

A finite numerical or heuristic search returns the largest value observed in
its candidate set. That value is a lower bound on the unknown true maximum
(best known feasible value), not automatically an upper bound. The claim guard
must reject `numerical_search` as proof provenance for `rigorous_upper_bound`
and require either a valid certificate/proof or downgraded wording such as
"reference envelope in the tested search region".

`tools/self_test_v51.py` executes both cases offline.
