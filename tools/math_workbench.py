#!/usr/bin/env python3
"""Optional executable checks for symbols, units, CVXPY and Pyomo models.

The workbench records finite checks. It never upgrades numerical evidence into
a universal proof without a separate mathematical obligation and provenance.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any

from common import now_iso, write_json

OPTIONAL = ["sympy", "pint", "numpy", "scipy", "cvxpy", "pyomo"]
CVXPY_SOLVE_SUCCESS = {"optimal"}
PYOMO_SOLVE_SUCCESS = {"optimal", "globallyoptimal", "locallyoptimal"}
PYOMO_SOLVER_STATUS_SUCCESS = {"ok"}


def _normalized_status(value: Any) -> str:
    return "".join(ch for ch in str(value).strip().lower() if ch.isalnum())


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _sympy_matrix(value: Any, *, sp: Any, symbols: dict[str, Any]) -> Any:
    """Parse a matrix expectation without treating an unevaluated check as PASS."""
    if isinstance(value, list):
        parsed = []
        for row in value:
            cells = row if isinstance(row, list) else [row]
            parsed.append([sp.sympify(cell, locals=symbols) for cell in cells])
        return sp.Matrix(parsed)
    parsed = sp.sympify(value, locals={**symbols, "Matrix": sp.Matrix})
    return parsed if isinstance(parsed, sp.MatrixBase) else sp.Matrix(parsed)


def _load(path: str) -> dict[str, Any]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _save(path: str, payload: dict[str, Any]) -> None:
    write_json(Path(path), payload)


def cmd_doctor(args: argparse.Namespace) -> int:
    rows = []
    for name in OPTIONAL:
        try:
            module = __import__(name)
            version = getattr(module, "__version__", "installed")
            rows.append({"package": name, "installed": True, "version": str(version)})
        except Exception as exc:
            rows.append({"package": name, "installed": False, "error": str(exc)})
    rows.append({"command": "pandoc", "installed": bool(shutil.which("pandoc"))})
    rows.append({"command": "xelatex", "installed": bool(shutil.which("xelatex"))})
    print(json.dumps({"checked_at": now_iso(), "components": rows}, ensure_ascii=False, indent=2))
    return 0


def cmd_symbolic(args: argparse.Namespace) -> int:
    try:
        import sympy as sp
    except Exception as exc:
        print(f"SymPy unavailable: {exc}", file=sys.stderr)
        return 4
    spec = _load(args.spec)
    results = []
    for item in spec.get("checks", []):
        kind = item.get("type")
        symbols = {name: sp.Symbol(name) for name in item.get("symbols", [])}
        if item.get("variable") and item["variable"] not in symbols:
            symbols[item["variable"]] = sp.Symbol(item["variable"])
        try:
            if kind == "equivalence":
                lhs = sp.sympify(item["lhs"], locals=symbols)
                rhs = sp.sympify(item["rhs"], locals=symbols)
                residual = sp.simplify(lhs - rhs)
                passed = residual == 0
                detail = str(residual)
            elif kind == "derivative":
                expr = sp.sympify(item["expression"], locals=symbols)
                var = symbols[item["variable"]]
                actual = sp.simplify(sp.diff(expr, var))
                expected = sp.sympify(item["expected"], locals=symbols)
                passed = sp.simplify(actual - expected) == 0
                detail = str(actual)
            elif kind == "limit":
                expr = sp.sympify(item["expression"], locals=symbols)
                var = symbols[item["variable"]]
                point = sp.sympify(item["point"], locals=symbols)
                actual = sp.limit(expr, var, point, dir=item.get("direction", "+-"))
                expected = sp.sympify(item["expected"], locals=symbols)
                passed = sp.simplify(actual - expected) == 0
                detail = str(actual)
            elif kind in {"jacobian", "hessian"}:
                variables = [symbols.setdefault(n, sp.Symbol(n)) for n in item["variables"]]
                if kind == "jacobian":
                    expressions = [sp.sympify(x, locals=symbols) for x in item["expressions"]]
                    actual = sp.Matrix(expressions).jacobian(variables)
                else:
                    expr = sp.sympify(item["expression"], locals=symbols)
                    actual = sp.hessian(expr, variables)
                if "expected" not in item:
                    raise ValueError(f"{kind} check requires an expected matrix")
                expected = _sympy_matrix(item["expected"], sp=sp, symbols=symbols)
                if actual.shape != expected.shape:
                    passed = False
                else:
                    residual = actual - expected
                    passed = all(sp.simplify(cell) == 0 for cell in residual)
                detail = str(actual)
            else:
                raise ValueError(f"unsupported symbolic check type: {kind}")
            results.append({"id": item.get("id"), "type": kind, "status": "PASS" if passed else "FAIL", "detail": detail})
        except Exception as exc:
            results.append({"id": item.get("id"), "type": kind, "status": "ERROR", "error": str(exc)})
    payload = {
        "generated_at": now_iso(), "tool": "sympy", "results": results,
        "all_passed": bool(results) and all(x["status"] == "PASS" for x in results),
    }
    _save(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["all_passed"] else 3


def cmd_units(args: argparse.Namespace) -> int:
    try:
        import pint
    except Exception as exc:
        print(f"Pint unavailable: {exc}", file=sys.stderr)
        return 4
    spec = _load(args.spec)
    ureg = pint.UnitRegistry()
    for definition in spec.get("definitions", []):
        try:
            ureg.define(definition)
        except Exception:
            # Built-in units may already exist.
            pass
    results = []
    scope = {name: getattr(ureg, name) for name in dir(ureg) if not name.startswith("_")}
    scope["ureg"] = ureg
    for item in spec.get("checks", []):
        try:
            quantity = eval(item["expression"], {"__builtins__": {}}, scope)
            actual = str(quantity.dimensionality)
            expected = item["expected_dimensionality"]
            passed = actual.replace(" ", "") == expected.replace(" ", "")
            results.append({"id": item.get("id"), "status": "PASS" if passed else "FAIL", "actual": actual, "expected": expected})
        except Exception as exc:
            results.append({"id": item.get("id"), "status": "ERROR", "error": str(exc)})
    payload = {
        "generated_at": now_iso(), "tool": "pint", "results": results,
        "all_passed": bool(results) and all(x["status"] == "PASS" for x in results),
    }
    _save(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["all_passed"] else 3


def _load_plugin(path: str):
    p = Path(path).resolve()
    spec = importlib.util.spec_from_file_location("cumcm_math_plugin", p)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin: {p}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_cvxpy(args: argparse.Namespace) -> int:
    try:
        import cvxpy as cp
    except Exception as exc:
        print(f"CVXPY unavailable: {exc}", file=sys.stderr)
        return 4
    try:
        module = _load_plugin(args.plugin)
        problem = module.build_problem()
        result: dict[str, Any] = {
            "generated_at": now_iso(), "tool": "cvxpy", "is_dcp": bool(problem.is_dcp()),
            "is_dpp": bool(problem.is_dpp()), "status": "NOT_SOLVED", "objective": None,
        }
        if args.require_dcp and not problem.is_dcp():
            result["status"] = "FAIL_DCP"
        elif args.solve:
            if not math.isfinite(args.feasibility_tol) or args.feasibility_tol < 0:
                raise ValueError("--feasibility-tol must be a finite nonnegative number")
            import numpy as np

            value = problem.solve(solver=args.solver or None, verbose=False)
            objective = _finite_float(value)
            status = str(problem.status)
            unassigned_variables: list[str] = []
            nonfinite_variables: list[str] = []
            for variable in problem.variables():
                name = str(variable.name())
                if variable.value is None:
                    unassigned_variables.append(name)
                    continue
                try:
                    values = np.asarray(variable.value, dtype=float)
                    finite = bool(np.isfinite(values).all())
                except (TypeError, ValueError, OverflowError):
                    finite = False
                if not finite:
                    nonfinite_variables.append(name)

            invalid_constraints: list[str] = []
            violated_constraints: list[str] = []
            max_constraint_violation = 0.0
            for index, constraint in enumerate(problem.constraints, start=1):
                name_attr = getattr(constraint, "name", None)
                name_value = name_attr() if callable(name_attr) else name_attr
                name = str(name_value or f"constraint-{index}")
                try:
                    values = np.asarray(constraint.violation(), dtype=float)
                    if values.size == 0 or not bool(np.isfinite(values).all()):
                        raise ValueError("non-finite or empty violation")
                    violation = float(np.max(np.abs(values)))
                except (AttributeError, TypeError, ValueError, OverflowError):
                    invalid_constraints.append(name)
                    continue
                max_constraint_violation = max(max_constraint_violation, violation)
                if violation > args.feasibility_tol:
                    violated_constraints.append(name)

            finite_feasible_solution = not (
                unassigned_variables
                or nonfinite_variables
                or invalid_constraints
                or violated_constraints
            )
            solve_succeeded = (
                _normalized_status(status) in CVXPY_SOLVE_SUCCESS
                and objective is not None
                and finite_feasible_solution
            )
            result.update({
                "status": status,
                "objective": objective,
                "solver": args.solver or "default",
                "unassigned_variables": unassigned_variables,
                "nonfinite_variables": nonfinite_variables,
                "invalid_constraints": invalid_constraints,
                "violated_constraints": violated_constraints,
                "feasibility_tolerance": args.feasibility_tol,
                "max_constraint_violation": max_constraint_violation,
                "finite_feasible_solution": finite_feasible_solution,
                "solve_succeeded": solve_succeeded,
            })
            if objective is None:
                result["failure_reason"] = "objective is not finite"
            elif not finite_feasible_solution:
                result["failure_reason"] = (
                    "solver returned no finite, assigned solution satisfying the recorded "
                    "constraint tolerance"
                )
            elif not solve_succeeded:
                result["failure_reason"] = f"non-success solver status: {status}"
        else:
            result["status"] = "CHECKED"
        _save(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] == "FAIL_DCP":
            return 3
        if args.solve and not result.get("solve_succeeded", False):
            return 3
        return 0
    except Exception as exc:
        payload = {"generated_at": now_iso(), "tool": "cvxpy", "status": "ERROR", "error": str(exc)}
        _save(args.output, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 3


def cmd_pyomo(args: argparse.Namespace) -> int:
    try:
        import pyomo.environ as pyo
    except Exception as exc:
        print(f"Pyomo unavailable: {exc}", file=sys.stderr)
        return 4
    try:
        module = _load_plugin(args.plugin)
        model = module.build_model()
        result: dict[str, Any] = {
            "generated_at": now_iso(), "tool": "pyomo", "status": "BUILT",
            "variables": sum(1 for _ in model.component_data_objects(pyo.Var)),
            "constraints": sum(1 for _ in model.component_data_objects(pyo.Constraint, active=True)),
        }
        if args.solve:
            if not math.isfinite(args.feasibility_tol) or args.feasibility_tol < 0:
                raise ValueError("--feasibility-tol must be a finite nonnegative number")
            solver = pyo.SolverFactory(args.solver)
            if not solver.available(False):
                raise RuntimeError(f"solver not available: {args.solver}")
            solved = solver.solve(model, tee=False)
            termination = str(solved.solver.termination_condition)
            solver_status = str(solved.solver.status)
            normalized_termination = _normalized_status(termination)
            status_succeeded = (
                normalized_termination in PYOMO_SOLVE_SUCCESS
                and _normalized_status(solver_status) in PYOMO_SOLVER_STATUS_SUCCESS
            )

            variable_count = 0
            unassigned_variables: list[str] = []
            nonfinite_variables: list[str] = []
            invalid_variable_bounds: list[str] = []
            violated_variable_bounds: list[str] = []
            max_variable_bound_violation = 0.0
            for variable in model.component_data_objects(pyo.Var, active=True):
                variable_count += 1
                value = pyo.value(variable, exception=False)
                if value is None:
                    unassigned_variables.append(str(variable.name))
                    continue
                numeric_value = _finite_float(value)
                if numeric_value is None:
                    nonfinite_variables.append(str(variable.name))
                    continue
                lower = _finite_float(pyo.value(variable.lb, exception=False)) if variable.has_lb() else None
                upper = _finite_float(pyo.value(variable.ub, exception=False)) if variable.has_ub() else None
                if (variable.has_lb() and lower is None) or (variable.has_ub() and upper is None):
                    invalid_variable_bounds.append(str(variable.name))
                    continue
                violation = 0.0
                if lower is not None:
                    violation = max(violation, lower - numeric_value)
                if upper is not None:
                    violation = max(violation, numeric_value - upper)
                violation = max(0.0, violation)
                max_variable_bound_violation = max(max_variable_bound_violation, violation)
                if violation > args.feasibility_tol:
                    violated_variable_bounds.append(str(variable.name))

            objective_values: list[dict[str, Any]] = []
            invalid_objectives: list[str] = []
            for objective in model.component_data_objects(pyo.Objective, active=True):
                value = _finite_float(pyo.value(objective, exception=False))
                objective_values.append({"name": str(objective.name), "value": value})
                if value is None:
                    invalid_objectives.append(str(objective.name))

            max_constraint_violation = 0.0
            invalid_constraints: list[str] = []
            violated_constraints: list[str] = []
            for constraint in model.component_data_objects(pyo.Constraint, active=True):
                body = _finite_float(pyo.value(constraint.body, exception=False))
                lower = _finite_float(pyo.value(constraint.lower, exception=False)) if constraint.has_lb() else None
                upper = _finite_float(pyo.value(constraint.upper, exception=False)) if constraint.has_ub() else None
                if body is None or (constraint.has_lb() and lower is None) or (constraint.has_ub() and upper is None):
                    invalid_constraints.append(str(constraint.name))
                    continue
                violation = 0.0
                if lower is not None:
                    violation = max(violation, lower - body)
                if upper is not None:
                    violation = max(violation, body - upper)
                violation = max(0.0, violation)
                max_constraint_violation = max(max_constraint_violation, violation)
                if violation > args.feasibility_tol:
                    violated_constraints.append(str(constraint.name))

            lower_bound: float | None = None
            upper_bound: float | None = None
            try:
                if len(solved.problem):
                    problem_record = solved.problem[0]
                    lower_bound = _finite_float(getattr(problem_record, "lower_bound", None))
                    upper_bound = _finite_float(getattr(problem_record, "upper_bound", None))
            except (AttributeError, IndexError, KeyError, TypeError):
                pass
            relative_gap = None
            if lower_bound is not None and upper_bound is not None:
                relative_gap = abs(upper_bound - lower_bound) / max(1.0, abs(lower_bound), abs(upper_bound))

            guarantee_level = {
                "globallyoptimal": "solver-reported-global",
                "locallyoptimal": "local",
                "optimal": "solver-reported-optimal",
            }.get(normalized_termination, "none")
            finite_solution = not (
                unassigned_variables
                or nonfinite_variables
                or invalid_variable_bounds
                or violated_variable_bounds
                or invalid_objectives
                or invalid_constraints
                or violated_constraints
            )
            solve_succeeded = status_succeeded and finite_solution
            result.update({
                "status": termination,
                "solver": args.solver,
                "solver_status": solver_status,
                "termination_condition": termination,
                "guarantee_level": guarantee_level,
                "claim_scope": (
                    "Numerical solver status only; it is not a mathematical proof or a universal global-optimality certificate."
                ),
                "solution_variable_count": variable_count,
                "unassigned_variables": unassigned_variables,
                "nonfinite_variables": nonfinite_variables,
                "invalid_variable_bounds": invalid_variable_bounds,
                "violated_variable_bounds": violated_variable_bounds,
                "max_variable_bound_violation": max_variable_bound_violation,
                "objective_values": objective_values,
                "invalid_objectives": invalid_objectives,
                "invalid_constraints": invalid_constraints,
                "violated_constraints": violated_constraints,
                "feasibility_tolerance": args.feasibility_tol,
                "max_constraint_violation": max_constraint_violation,
                "lower_bound": lower_bound,
                "upper_bound": upper_bound,
                "relative_gap": relative_gap,
                "finite_feasible_solution": finite_solution,
                "solve_succeeded": solve_succeeded,
            })
            if not status_succeeded:
                result["failure_reason"] = (
                    f"non-success solver result: status={solver_status}, termination={termination}"
                )
            elif not finite_solution:
                result["failure_reason"] = "solver returned no finite, loaded solution satisfying the recorded feasibility tolerance"
        _save(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not args.solve or result.get("solve_succeeded", False) else 3
    except Exception as exc:
        payload = {"generated_at": now_iso(), "tool": "pyomo", "status": "ERROR", "error": str(exc)}
        _save(args.output, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    x = sub.add_parser("doctor"); x.set_defaults(func=cmd_doctor)
    x = sub.add_parser("symbolic"); x.add_argument("--spec", required=True); x.add_argument("--output", required=True); x.set_defaults(func=cmd_symbolic)
    x = sub.add_parser("units"); x.add_argument("--spec", required=True); x.add_argument("--output", required=True); x.set_defaults(func=cmd_units)
    x = sub.add_parser("cvxpy"); x.add_argument("--plugin", required=True); x.add_argument("--output", required=True); x.add_argument("--require-dcp", action="store_true"); x.add_argument("--solve", action="store_true"); x.add_argument("--solver"); x.add_argument("--feasibility-tol", type=float, default=1e-7); x.set_defaults(func=cmd_cvxpy)
    x = sub.add_parser("pyomo"); x.add_argument("--plugin", required=True); x.add_argument("--output", required=True); x.add_argument("--solve", action="store_true"); x.add_argument("--solver", default="highs"); x.add_argument("--feasibility-tol", type=float, default=1e-7); x.set_defaults(func=cmd_pyomo)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
