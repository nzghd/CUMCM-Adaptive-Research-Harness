def build_problem():
    import cvxpy as cp
    x = cp.Variable(nonneg=True)
    return cp.Problem(cp.Minimize((x - 2) ** 2), [x <= 5])
