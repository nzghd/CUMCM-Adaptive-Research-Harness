def build_model():
    import pyomo.environ as pyo
    m = pyo.ConcreteModel()
    m.x = pyo.Var(domain=pyo.NonNegativeReals)
    m.obj = pyo.Objective(expr=(m.x - 2) ** 2)
    m.c = pyo.Constraint(expr=m.x <= 5)
    return m
