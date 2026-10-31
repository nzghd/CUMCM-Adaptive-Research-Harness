#!/usr/bin/env python3
"""Offline static and isolated smoke tests for the harness.

No network request and no model/API call is made. The optional API adapter is
exercised only with ``--dry-run``. The Faithful verifier wrapper is exercised
only through input preparation and environment diagnosis.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def console_safe(value: object) -> str:
    """Keep Windows legacy consoles from hiding the real child-process error."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return str(value).encode(encoding, errors="backslashreplace").decode(encoding)


def invoke(
    cmd: list[str],
    env: dict[str, str],
    *,
    expected: Iterable[int] = (0,),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        check=False,
    )
    allowed = set(expected)
    if result.returncode not in allowed:
        print("COMMAND FAILED:", " ".join(cmd))
        print("EXPECTED:", sorted(allowed), "GOT:", result.returncode)
        print(console_safe(result.stdout))
        print(console_safe(result.stderr))
        raise SystemExit(result.returncode or 1)
    return result


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def refresh_ai_disclosure(run_dir: Path, env: dict[str, str]) -> None:
    """Export the current AI log and bind a tiny synthetic test PDF to it."""
    invoke(
        [
            sys.executable,
            str(TOOLS / "harness.py"),
            "export-ai",
            "--run",
            run_dir.name,
            "--by",
            "SELFTEST_REVIEWER",
        ],
        env,
    )

    # The harness only needs a non-empty PDF artifact for this offline receipt
    # test. Build a small, structurally valid blank PDF without extra packages.
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Contents 4 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Length 0 >>\nstream\n\nendstream\nendobj\n",
    ]
    pdf = bytearray(header)
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    pdf_path = run_dir / "deliverables/AI工具使用详情.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(pdf)
    invoke(
        [
            sys.executable,
            str(TOOLS / "harness.py"),
            "attest-ai-disclosure-pdf",
            "--run",
            run_dir.name,
            "--by",
            "SELFTEST_REVIEWER",
        ],
        env,
    )


def static_checks() -> None:
    required = [
        "README.md",
        "QUICKSTART_WINDOWS.md",
        "AGENTS.md",
        "CLAUDE.md",
        "CONTEXT.md",
        "RUNTIME_RULES.md",
        ".codex/config.toml",
        ".agents/skills/cumcm-adaptive-research/SKILL.md",
        ".claude/agents/research-specialist.md",
        ".claude/skills/cumcm-adaptive-research/SKILL.md",
        "skills/cumcm-adaptive-research/SKILL.md",
        "config/agent_roles.json",
        "config/runtime_registry.json",
        "config/default_budget.json",
        "config/artifact_schema_registry.json",
        "upstream/LLM_AS_A_VERIFIER.lock.json",
        "requirements-core.txt",
        "requirements-api.txt",
        "requirements-reference-verifier.txt",
        "scripts/install_reference_verifier.ps1",
        "scripts/install_reference_verifier.sh",
        "tools/harness.py",
        "tools/gate_review.py",
        "tools/self_test_gate_review_unit.py",
        "tools/self_test_gate_review.py",
        "tools/submission_compliance.py",
        "tools/self_test_submission_compliance.py",
        "tools/support_package.py",
        "tools/self_test_support_package.py",
        "schemas/support_package_receipt.schema.json",
        "tools/diversity.py",
        "tools/verifier_faithful.py",
        "tools/verifier_lite.py",
        "tools/external_review.py",
        "tools/relay.py",
        "tools/api_burst_openai.py",
        "tools/provider_call.py",
        "tools/runtime.py",
        "tools/paper.py",
        "tools/schema_validate.py",
        "tools/migrate_v51.py",
        "tools/README.md",
        "scripts/start_claude_deepseek.ps1",
        "scripts/start_claude_deepseek.sh",
        "docs/ISOLATION.md",
        "docs/PAPER_WRITING.md",
        "config/paper_profile_cumcm.json",
        "skills/cumcm-paper-writing/SKILL.md",
        "prompts/06_PAPER_WRITE.md",
        "prompts/07_PAPER_AUDIT.md",
    ]
    missing = [rel for rel in required if not (ROOT / rel).exists()]
    if missing:
        raise SystemExit(f"Missing required files: {missing}")

    json_files = (
        list((ROOT / "config").glob("*.json"))
        + list((ROOT / "templates").rglob("*.json"))
        + list((ROOT / "schemas").glob("*.json"))
        + list((ROOT / "upstream").glob("*.json"))
        + list((ROOT / "integrations").rglob("*.json"))
    )
    for path in json_files:
        json.loads(path.read_text(encoding="utf-8-sig"))

    result = subprocess.run(
        [sys.executable, str(TOOLS / "schema_validate.py"), "--static"],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit("Static schema/producer-contract validation failed:\n" + result.stdout + result.stderr)

    config_text = (ROOT / ".codex/config.toml").read_text(encoding="utf-8")
    for fragment in (
        "[agents]",
        "enabled = true",
        "max_concurrent_threads_per_session = 4",
    ):
        if fragment not in config_text:
            raise SystemExit(f"Codex config missing {fragment!r}")

    agent_files = list((ROOT / ".codex/agents").glob("*.toml"))
    if len(agent_files) < 10:
        raise SystemExit("Expected at least 10 custom agent files")
    names: set[str] = set()
    for path in agent_files:
        text = path.read_text(encoding="utf-8")
        for key in ("name =", "description =", "developer_instructions ="):
            if key not in text:
                raise SystemExit(f"Agent file missing {key}: {path}")
        name_line = next(line for line in text.splitlines() if line.startswith("name ="))
        if name_line in names:
            raise SystemExit(f"Duplicate custom-agent name line: {name_line}")
        names.add(name_line)

    for writable_intake_agent in ("problem-cartographer.toml", "data-auditor.toml"):
        agent_text = (ROOT / ".codex/agents" / writable_intake_agent).read_text(encoding="utf-8")
        if 'sandbox_mode = "workspace-write"' not in agent_text:
            raise SystemExit(f"Intake agent must be able to write its assigned artifacts: {writable_intake_agent}")

    roles = load(ROOT / "config/agent_roles.json")
    canonical_names = {str(item.get("name")) for item in roles.get("roles", [])}
    if len(canonical_names) < 10 or "research_specialist" not in canonical_names:
        raise SystemExit("Canonical agent role registry is incomplete")
    claude_files = list((ROOT / ".claude/agents").glob("*.md"))
    if len(claude_files) != len(canonical_names):
        raise SystemExit("Claude Code generated agent count does not match canonical role count")
    # Check generated runtime files without mutating them.
    result = subprocess.run(
        [sys.executable, str(TOOLS / "runtime.py"), "check"],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit("Runtime adapter check failed:\n" + result.stdout + result.stderr)

    lock = load(ROOT / "upstream/LLM_AS_A_VERIFIER.lock.json")
    if lock.get("commit") != "8db8a114355a9d7fdf9a8d1d5c87f6aeebd18770":
        raise SystemExit("Unexpected upstream lock commit")
    if lock.get("version") != "0.2.0":
        raise SystemExit("Unexpected upstream package version")

    api_adapter = (ROOT / "tools/api_burst_openai.py").read_text(encoding="utf-8")
    if '"xhigh"' not in api_adapter:
        raise SystemExit("API adapter reasoning choices must include xhigh")

    # There must be one canonical installer path, not two divergent copies.
    for obsolete in (
        ROOT / "integrations/llm_as_a_verifier/install_reference_verifier.ps1",
        ROOT / "integrations/llm_as_a_verifier/install_reference_verifier.sh",
    ):
        if obsolete.exists():
            raise SystemExit(f"Obsolete duplicate installer still present: {obsolete}")


def _fill_lite_reviews(session: Path, stage_key: str) -> None:
    state = load(session / "STATE.json")
    quality = {"A": 4.0, "B": 0.5, "C": 2.5}
    for assignment in state.get(stage_key, []):
        review_path = Path(assignment["review"])
        review = load(review_path)
        slot_a = review["orientation"]["slot_a"]
        slot_b = review["orientation"]["slot_b"]
        for criterion in review["criterion_scores"]:
            criterion["candidate_a_score"] = quality[slot_a]
            criterion["candidate_b_score"] = quality[slot_b]
            criterion["hard_failure_a"] = False
            criterion["hard_failure_b"] = False
            criterion["evidence"] = ["deterministic synthetic self-test evidence"]
            criterion["reason"] = "Known synthetic ranking A > C > B"
        review["reviewer_id"] = "self-test"
        review["overall_preference"] = (
            "a" if quality[slot_a] > quality[slot_b]
            else "b" if quality[slot_b] > quality[slot_a]
            else "tie"
        )
        review["confidence"] = 0.9
        review["completed"] = True
        save(review_path, review)


def _prepare_pre_freeze_approvals(run_dir: Path, env: dict[str, str]) -> None:
    """Complete the smallest real G0-G4 fixture accepted by v5.1."""
    (run_dir / "problem/PROBLEM_BRIEF.md").write_text(
        "# Problem Brief\n\nChoose a nonnegative synthetic decision, report its value, "
        "and verify feasibility against the supplied two-row data set.\n",
        encoding="utf-8",
    )
    (run_dir / "problem/QUESTION_MAP.md").write_text(
        "# Question Map\n\n| Question | Input | Output | Acceptance |\n"
        "|---|---|---|---|\n| Q1 | sample.csv | decision z | deterministic feasibility replay |\n",
        encoding="utf-8",
    )
    (run_dir / "problem/ASSUMPTION_REGISTER.csv").write_text(
        "assumption_id,statement,source,status,impact,validation_method,owner,notes\n"
        "A001,The synthetic rows are exact,statement,accepted,low,direct replay,Self-test reviewer,offline fixture\n",
        encoding="utf-8",
    )
    (run_dir / "problem/VALIDATION_MAP.md").write_text(
        "# Validation Map\n\n| Object | Falsifier | Method | Evidence | Reviewer | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| Q1 | z violates nonnegativity | deterministic runner | "
        "experiments/runs/EXP_SMOKE/seed_1/metrics.json | Self-test reviewer | checked |\n",
        encoding="utf-8",
    )
    (run_dir / "data/DATA_AUDIT.md").write_text(
        "# Data Audit\n\nThe fixture contains two complete numeric rows, no duplicates, "
        "no missing values, one consistent unit convention, and no train/test or temporal leakage. "
        "Direct inspection is sufficient for this offline deterministic smoke case.\n",
        encoding="utf-8",
    )
    (run_dir / "interpretation/PROBLEM_SEMANTICS.md").write_text(
        "# Problem Semantics\n\nThe statement asks for one feasible nonnegative decision. "
        "The only retained reporting fork concerns whether the result is presented in full or brief form; "
        "that choice does not change the objective, feasible set, units, coordinates, or time semantics.\n",
        encoding="utf-8",
    )
    (run_dir / "interpretation/INTERPRETATION_AUDIT.md").write_text(
        "# Interpretation Audit\n\nTwo explicit assumption fingerprints cover the harmless reporting fork. "
        "The scientific meaning and feasibility rule are identical in both candidates.\n",
        encoding="utf-8",
    )
    invoke(
        [
            sys.executable,
            str(TOOLS / "interpretation_engine.py"),
            "fork-add",
            "--run",
            run_dir.name,
            "--id",
            "IF_SMOKE",
            "--dimension",
            "reporting_style",
            "--question",
            "Should the synthetic result use a full or brief report?",
            "--option",
            "FULL=full report",
            "--option",
            "BRIEF=brief report",
            "--impact",
            "low",
            "--uncertainty",
            "low",
            "--irreversibility",
            "low",
            "--note",
            "Non-structural offline smoke-test fork.",
        ],
        env,
    )
    for candidate, style, algorithm in (
        ("IDEA_A", "full", "exact"),
        ("IDEA_B", "brief", "simulation"),
    ):
        invoke(
            [
                sys.executable,
                str(TOOLS / "interpretation_engine.py"),
                "fingerprint-set",
                "--run",
                run_dir.name,
                "--candidate",
                candidate,
                "--set",
                f"reporting_style={style}",
                "--set",
                f"algorithm={algorithm}",
                "--source",
                f"research/ideas/{candidate}/IDEA_CARD.json",
            ],
            env,
        )
    invoke(
        [sys.executable, str(TOOLS / "interpretation_engine.py"), "analyze", "--run", run_dir.name],
        env,
    )

    team_plan = load(run_dir / "planning/TEAM_PLAN.json")
    team_plan["goal"] = "Compare three bounded synthetic approaches and retain one auditable module."
    for stream in team_plan.get("workstreams", []):
        stream["objective"] = "Produce one falsifiable offline modeling route."
        stream["method_prior"] = "deterministic baseline"
        stream["expected_information_gain"] = "Tests whether a feasible decision can be reproduced."
    save(run_dir / "planning/TEAM_PLAN.json", team_plan)
    save(
        run_dir / "selection/FOCUS_SET.json",
        {
            "selected_ideas": ["IDEA_A"],
            "selected_modules": ["MOD_SMOKE"],
            "selected_by": "SELFTEST_REVIEWER",
            "selected_at": "2026-09-03T00:00:00+08:00",
            "evidence": ["research/diversity/DIVERSITY_REPORT.json"],
            "rationale": "The exact route is deterministic and sufficient for this smoke test.",
            "rejected_or_deferred": ["IDEA_B", "IDEA_C"],
        },
    )
    (run_dir / "reviews/FEASIBILITY_REVIEW.md").write_text(
        "# Feasibility Review\n\nThe retained exact route is executable offline, uses explicit constraints, "
        "and has a deterministic metrics artifact for replay.\n",
        encoding="utf-8",
    )
    invoke(
        [
            sys.executable,
            str(TOOLS / "sensitivity_engine.py"),
            "add",
            "--run",
            run_dir.name,
            "--kind",
            "structural",
            "--id",
            "SS_SMOKE",
            "--target",
            "reporting style",
            "--alternatives",
            "full report versus brief report",
            "--metric",
            "scientific decision invariance",
            "--status",
            "not-applicable",
            "--by",
            "SELFTEST_REVIEWER",
            "--waiver-reason",
            "The registered fork changes presentation only and leaves the scientific structure unchanged.",
        ],
        env,
    )

    (run_dir / "experiments/EXPERIMENT_PLAN.md").write_text(
        "# Experiment Plan\n\nRun the deterministic exact baseline at seeds 0 and 1. "
        "Record RMSE and feasibility rate in canonical metrics files, retain runner receipts, "
        "and accept only a successful process with complete expected artifacts.\n",
        encoding="utf-8",
    )
    (run_dir / "experiments/BASELINE_REGISTRY.csv").write_text(
        "baseline_id,name,purpose,implementation_path,metric,expected_role,status,notes\n"
        "B001,Exact smoke baseline,verify deterministic execution,code/smoke_experiment.py,feasible_rate,reference,completed,offline fixture\n",
        encoding="utf-8",
    )
    (run_dir / "reviews/SYNTHESIS_REVIEW.md").write_text(
        "# Synthesis Review\n\nThe retained synthetic model matches Q1, its evidence artifact is replayable, "
        "and the empty dispute register contains no unresolved critical issue.\n",
        encoding="utf-8",
    )

    invoke(
        [
            sys.executable,
            str(TOOLS / "math_engine.py"),
            "init-module",
            "--run",
            run_dir.name,
            "--module",
            "MOD_SMOKE",
            "--title",
            "Synthetic feasibility model",
            "--question-ids",
            "Q1",
            "--method-family",
            "exact-optimization",
        ],
        env,
    )
    module_dir = run_dir / "research/modules/MOD_SMOKE"
    module_spec = load(module_dir / "MODULE_SPEC.json")
    module_spec["requires_code"] = False
    module_spec["requires_parameters"] = False
    module_spec["source_idea_ids"] = ["IDEA_A"]
    module_spec["human_owner"] = "SELFTEST_REVIEWER"
    save(module_dir / "MODULE_SPEC.json", module_spec)
    prose = (
        "The offline synthetic model defines a dimensionless nonnegative decision and a deterministic "
        "feasibility relation. Every input, output, domain, unit, boundary condition, numerical step, "
        "acceptance rule, failure mode, and evidence path is stated explicitly. The exact baseline is "
        "replayed at fixed seeds, checked against the constraint, and compared with the recorded metric. "
    )
    for filename, title in (
        ("MODEL_INTENT.md", "Model Intent"),
        ("ASSUMPTIONS.md", "Assumptions"),
        ("FORMULATION.md", "Mathematical Formulation"),
        ("DERIVATION.md", "Derivation"),
        ("PARAMETER_IDENTIFICATION.md", "Parameter Identification"),
        ("THEORY_CHECKS.md", "Theory Checks"),
        ("NUMERICAL_METHOD.md", "Numerical Method"),
        ("VALIDATION_PROTOCOL.md", "Validation Protocol"),
    ):
        (module_dir / filename).write_text(f"# {title}\n\n{prose * 3}\n", encoding="utf-8")
    invoke(
        [
            sys.executable,
            str(TOOLS / "math_engine.py"),
            "symbol-add",
            "--run",
            run_dir.name,
            "--module",
            "MOD_SMOKE",
            "--symbol-id",
            "SYM_SMOKE",
            "--latex",
            "z",
            "--name",
            "decision",
            "--meaning",
            "synthetic nonnegative decision",
            "--role",
            "decision_variable",
            "--domain",
            "nonnegative real numbers",
            "--unit",
            "dimensionless",
            "--source",
            "FORMULATION.md",
        ],
        env,
    )
    invoke(
        [
            sys.executable,
            str(TOOLS / "math_engine.py"),
            "equation-add",
            "--run",
            run_dir.name,
            "--module",
            "MOD_SMOKE",
            "--equation-id",
            "EQ_SMOKE",
            "--latex",
            "z \\ge 0",
            "--type",
            "constraint",
            "--description",
            "nonnegativity feasibility constraint",
            "--units-check",
            "pass",
            "--derivation-location",
            "DERIVATION.md",
            "--verification-ref",
            "experiments/runs/EXP_SMOKE/seed_1/metrics.json",
            "--status",
            "checked",
        ],
        env,
    )

    for stage_id in ("interpretation", "structural_validation", "mathematical_construction"):
        invoke(
            [
                sys.executable,
                str(TOOLS / "v51_orchestrator.py"),
                "approve",
                "--run",
                run_dir.name,
                "--stage",
                stage_id,
                "--by",
                "SELFTEST_REVIEWER",
                "--note",
                "Isolated base compatibility fixture.",
            ],
            env,
        )


def isolated_smoke() -> None:
    global TOOLS
    source_tools = TOOLS
    with tempfile.TemporaryDirectory(prefix="cumcm-harness-v51-base-") as tmp_value:
        tmp = Path(tmp_value)
        for name in ("config", "templates", "schemas", "upstream"):
            shutil.copytree(ROOT / name, tmp / name)
        # Run the harness from inside the isolated project too.  v5.1 approval
        # receipts deliberately bind validator controls and reject controls
        # outside the project root.
        shutil.copytree(source_tools, tmp / "tools")
        TOOLS = tmp / "tools"
        shutil.copy2(ROOT / "harness_config.json", tmp / "harness_config.json")
        shutil.copy2(ROOT / "RUNTIME_RULES.md", tmp / "RUNTIME_RULES.md")
        (tmp / "inputs/problem").mkdir(parents=True)
        (tmp / "inputs/data").mkdir(parents=True)
        (tmp / "inputs/problem/problem.txt").write_text(
            "Synthetic modeling problem: choose a feasible decision and justify it.",
            encoding="utf-8",
        )
        (tmp / "inputs/data/sample.csv").write_text("x,y\n1,2\n2,4\n", encoding="utf-8")
        (tmp / "runs").mkdir()
        (tmp / ".harness").mkdir()
        env = os.environ.copy()
        env["CUMCM_HARNESS_ROOT"] = str(tmp)

        invoke(
            [
                sys.executable,
                str(TOOLS / "harness.py"),
                "init",
                "--contest",
                "SMOKE",
                "--problem",
                "A",
                "--phase",
                "rehearsal",
                "--runtime",
                "claude-code-deepseek",
                "--run-id",
                "SMOKE-A",
                "--api-budget-usd",
                "5",
                "--deepseek-api-budget-usd",
                "5",
            ],
            env,
        )
        run_dir = tmp / "runs/SMOKE-A"
        meta = load(run_dir / "RUN_META.json")
        budget0 = load(run_dir / "BUDGET.json")
        if meta.get("runtime") != "claude-code-deepseek":
            raise SystemExit("Runtime selection was not persisted")
        if not budget0.get("api_providers", {}).get("deepseek", {}).get("enabled"):
            raise SystemExit("DeepSeek provider budget was not enabled")

        # Run resolution must not allow escaping runs/.
        invoke(
            [sys.executable, str(TOOLS / "harness.py"), "status", "--run", "../inputs"],
            env,
            expected=(2,),
        )

        # Problem graph sufficient for local diagnostics.
        graph_path = run_dir / "problem/PROBLEM_GRAPH.json"
        graph = load(graph_path)
        graph["nodes"] = [
            {
                "id": "Q1",
                "title": "synthetic optimization",
                "type": "optimization",
                "inputs": ["sample.csv"],
                "outputs": ["decision"],
                "decision_variables": ["z"],
                "constraints": ["z >= 0"],
                "uncertainties": [],
                "depends_on": [],
                "validation_requirements": ["constraint audit"],
                "status": "open",
            }
        ]
        graph["desired_method_families"] = ["exact-optimization", "simulation"]
        graph["human_approved"] = True
        save(graph_path, graph)

        ideas = [
            {
                "schema_version": 1,
                "id": "IDEA_A",
                "title": "MILP route",
                "target_nodes": ["Q1"],
                "summary": "Formulate a mixed integer linear program with explicit constraints and a small-case lower-bound check.",
                "method_families": ["exact-optimization"],
                "core_assumptions": ["linear costs"],
                "validation_plan": ["constraint audit", "brute force on small cases"],
                "main_risks": ["scale"],
                "status": "proposed",
            },
            {
                "schema_version": 1,
                "id": "IDEA_B",
                "title": "Monte Carlo route",
                "target_nodes": ["Q1"],
                "summary": "Use scenario simulation to quantify uncertainty and compare robust decisions under perturbed inputs.",
                "method_families": ["simulation"],
                "core_assumptions": ["scenario distribution is adequate"],
                "validation_plan": ["multi-seed stability", "sensitivity analysis"],
                "main_risks": ["distribution misspecification"],
                "status": "proposed",
            },
            {
                "schema_version": 1,
                "id": "IDEA_C",
                "title": "Enumeration route",
                "target_nodes": ["Q1"],
                "summary": "Enumerate a bounded synthetic decision set and replay feasibility as an independent baseline.",
                "method_families": ["brute-force"],
                "core_assumptions": ["bounded synthetic domain"],
                "validation_plan": ["compare with exact route"],
                "main_risks": ["limited scale"],
                "status": "proposed",
            },
        ]
        for card in ideas:
            folder = run_dir / "research/ideas" / card["id"]
            folder.mkdir(parents=True)
            save(folder / "IDEA_CARD.json", card)
        save(
            run_dir / "ideas/IDEA_REGISTRY.json",
            {
                "next_id": 1,
                "ideas": [
                    {
                        "id": card["id"],
                        "title": card["title"],
                        "path": f"research/ideas/{card['id']}/IDEA_CARD.json",
                        "source": "self-test",
                        "status": card["status"],
                        "created_at": "2026-09-03T00:00:00+08:00",
                    }
                    for card in ideas
                ],
            },
        )

        invoke([sys.executable, str(TOOLS / "diversity.py"), "analyze"], env)
        if not (run_dir / "research/diversity/DIVERSITY_REPORT.json").exists():
            raise SystemExit("Diversity report missing")

        # Reproducible experiment runner: dry-run and one real local command.
        experiment_script = run_dir / "code/smoke_experiment.py"
        experiment_script.write_text(
            "from __future__ import annotations\n"
            "import argparse, json\n"
            "from pathlib import Path\n"
            "p=argparse.ArgumentParser(); p.add_argument('--seed', type=int, required=True); "
            "p.add_argument('--output-dir', required=True); a=p.parse_args()\n"
            "out=Path(a.output_dir); out.mkdir(parents=True, exist_ok=True)\n"
            "payload={'metrics':["
            "{'metric':'rmse','value':1/(a.seed+1),'split':'synthetic','unit':'dimensionless'},"
            "{'metric':'feasible_rate','value':1.0,'split':'synthetic','unit':'fraction'}]}\n"
            "(out/'metrics.json').write_text(json.dumps(payload)+'\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        experiment_manifest = {
            "run_id": "SMOKE-A",
            "experiments": [
                {
                    "id": "EXP_SMOKE",
                    "module_id": "MOD_SMOKE",
                    "description": "offline experiment-runner smoke test",
                    "command": [
                        sys.executable,
                        "code/smoke_experiment.py",
                        "--seed",
                        "{seed}",
                        "--output-dir",
                        "{output_dir}",
                    ],
                    "cwd": ".",
                    "seeds": [0, 1],
                    "timeout_sec": 30,
                    "expected_artifacts": ["experiments/runs/EXP_SMOKE/seed_{seed}/metrics.json"],
                    "metrics_file": "experiments/runs/EXP_SMOKE/seed_{seed}/metrics.json",
                    "env": {},
                }
            ],
        }
        experiment_manifest_path = run_dir / "experiments/smoke_manifest.json"
        save(experiment_manifest_path, experiment_manifest)
        invoke(
            [sys.executable, str(TOOLS / "run_experiments.py"), "--manifest", str(experiment_manifest_path), "--dry-run"],
            env,
        )
        invoke([sys.executable, str(TOOLS / "run_experiments.py"), "--manifest", str(experiment_manifest_path)], env)
        if not (run_dir / "experiments/runs/EXP_SMOKE/seed_1/metrics.json").exists():
            raise SystemExit("Experiment runner did not create expected artifact")
        metrics_text = (run_dir / "experiments/METRICS.csv").read_text(encoding="utf-8")
        if "rmse" not in metrics_text or "feasible_rate" not in metrics_text:
            raise SystemExit("Experiment metrics were not registered")

        # Gate-order safety: G1 must not be approved before G0.
        invoke(
            [sys.executable, str(TOOLS / "harness.py"), "approve", "--run", "SMOKE-A", "--gate", "G1", "--by", "SELFTEST"],
            env,
            expected=(2,),
        )
        if load(run_dir / "STATUS.json")["gates"]["G1"]["status"] == "approved":
            raise SystemExit("Gate prerequisite protection failed")

        # Normalized AI log and disclosure export preserve inputs/outputs.
        invoke(
            [
                sys.executable,
                str(TOOLS / "harness.py"),
                "ai-log",
                "--run",
                "SMOKE-A",
                "--tool",
                "Codex",
                "--model",
                "smoke-model",
                "--stage",
                "test",
                "--purpose",
                "exercise normalized AI audit logging",
                "--prompt-summary",
                "synthetic offline task",
                "--input",
                "problem/PROBLEM_GRAPH.json",
                "--output",
                "research/diversity/DIVERSITY_REPORT.json",
                "--adopted",
                "--modified",
                "--verified",
                "--by",
                "SELFTEST",
            ],
            env,
        )
        invoke(
            [
                sys.executable,
                str(TOOLS / "harness.py"),
                "export-ai",
                "--run",
                "SMOKE-A",
                "--by",
                "SELFTEST_REVIEWER",
            ],
            env,
        )
        details = (run_dir / "ai/AI_TOOL_USAGE_DETAILS.md").read_text(encoding="utf-8")
        for expected_text in ("problem/PROBLEM_GRAPH.json", "research/diversity/DIVERSITY_REPORT.json", "smoke-model"):
            if expected_text not in details:
                raise SystemExit(f"AI usage export missing {expected_text}")
        if "SELFTEST_REVIEWER" in details or "Run:" in details:
            raise SystemExit("Public AI usage details leaked internal run/reviewer identity")

        # Manual independent Pro packet export/import without UI automation.
        invoke(
            [
                sys.executable,
                str(TOOLS / "external_review.py"),
                "export",
                "--run",
                "SMOKE-A",
                "--kind",
                "blind-idea",
                "--packet-id",
                "PACKET_TEST",
                "--include-problem-graph",
            ],
            env,
        )
        packet = run_dir / "external/PACKET_TEST/PACKET.md"
        if not packet.exists():
            raise SystemExit("External review packet missing")
        external_response = tmp / "external_idea.json"
        save(
            external_response,
            {
                "id": "IDEA_EXT",
                "title": "Independent robust route",
                "target_nodes": ["Q1"],
                "summary": "Use a robust counterpart and compare it with the deterministic baseline.",
                "method_families": ["robust-optimization"],
                "core_assumptions": ["bounded uncertainty"],
                "validation_plan": ["stress test uncertainty radius"],
                "main_risks": ["overconservatism"],
            },
        )
        invoke(
            [
                sys.executable,
                str(TOOLS / "external_review.py"),
                "import",
                "--run",
                "SMOKE-A",
                "--kind",
                "blind-idea",
                "--response",
                str(external_response),
                "--tool",
                "ChatGPT",
                "--model",
                "smoke-model",
                "--packet-id",
                "PACKET_TEST",
                "--import-id",
                "IMPORT_TEST",
                "--register-idea",
                "--verified",
                "--by",
                "SELFTEST",
            ],
            env,
        )
        if not (run_dir / "research/ideas/IDEA_EXT/IDEA_CARD.json").exists():
            raise SystemExit("External Idea Card import failed")
        # The external import legitimately expands the active idea pool, so
        # refresh the canonical diversity receipt before G1 approval.
        invoke([sys.executable, str(TOOLS / "diversity.py"), "analyze"], env)

        # Fresh-context relay state and structured handoff.
        invoke(
            [
                sys.executable,
                str(TOOLS / "relay.py"),
                "init",
                "--run",
                "SMOKE-A",
                "--id",
                "RELAY_TEST",
                "--goal",
                "finish a bounded synthetic task",
                "--task",
                "inspect one artifact",
                "--max-rounds",
                "2",
            ],
            env,
        )
        relay_session = run_dir / "relay/RELAY_TEST"
        handoff1 = load(relay_session / "round_001/HANDOFF.json")
        handoff1.update(
            {
                "status": "continue",
                "summary": "round one checked the artifact",
                "evidence": ["problem/PROBLEM_GRAPH.json"],
                "next_steps": ["finish verification"],
                "blockers": [],
                "do_not_repeat": ["do not re-read raw inputs unnecessarily"],
                "workspace_truth": ["problem graph exists"],
                "next_bounded_task": "finish verification",
                "stop_condition": "verification recorded",
                "completed": True,
            }
        )
        save(relay_session / "round_001/HANDOFF.json", handoff1)
        invoke([sys.executable, str(TOOLS / "relay.py"), "advance", "--session", str(relay_session)], env)
        if not (relay_session / "round_002/PROMPT.md").exists():
            raise SystemExit("Relay round two missing")
        handoff2 = load(relay_session / "round_002/HANDOFF.json")
        handoff2.update(
            {
                "status": "complete",
                "summary": "verification complete",
                "evidence": ["problem/PROBLEM_GRAPH.json"],
                "next_steps": [],
                "blockers": [],
                "do_not_repeat": [],
                "workspace_truth": ["verification recorded"],
                "next_bounded_task": "none",
                "stop_condition": "complete",
                "completed": True,
            }
        )
        save(relay_session / "round_002/HANDOFF.json", handoff2)
        invoke([sys.executable, str(TOOLS / "relay.py"), "advance", "--session", str(relay_session)], env)
        if load(relay_session / "STATE.json").get("status") != "complete":
            raise SystemExit("Relay did not complete")

        # Shared task and candidates for both verifier modes.
        problem_file = run_dir / "problem/VERIFIER_PROBLEM.md"
        problem_file.write_text("Choose the correct numeric answer.", encoding="utf-8")
        lite_mode = run_dir / "modes/reference-verifier-lite"
        faithful_mode = run_dir / "modes/reference-verifier-faithful"
        candidates = {"A": "answer=2; test passed", "B": "answer=3; test failed", "C": "answer=2; no test"}
        for candidate_id, text in candidates.items():
            (lite_mode / "candidates" / f"{candidate_id}.txt").write_text(text, encoding="utf-8")
            (faithful_mode / "candidates" / f"{candidate_id}.txt").write_text(text, encoding="utf-8")

        # Faithful wrapper dry-run prepares auditable inputs but never calls a verifier backend.
        faithful_config = {
            "session_id": "SMOKE_FAITHFUL",
            "problem_file": "runs/SMOKE-A/problem/VERIFIER_PROBLEM.md",
            "candidate_files": {cid: f"runs/SMOKE-A/modes/reference-verifier-faithful/candidates/{cid}.txt" for cid in candidates},
            "criteria": {"Correctness": "Is the numeric answer correct?"},
            "model": "deepseek-v4-flash",
            "n_evaluations": 2,
            "pivots": 2,
            "seed": 0,
            "output_dir": "runs/SMOKE-A/modes/reference-verifier-faithful/result",
        }
        faithful_config_path = faithful_mode / "config.json"
        save(faithful_config_path, faithful_config)
        invoke(
            [
                sys.executable,
                str(TOOLS / "verifier_faithful.py"),
                "dry-run",
                "--run",
                "SMOKE-A",
                "--provider",
                "deepseek",
                "--config",
                str(faithful_config_path),
            ],
            env,
            expected=(0, 3),  # 3 means the optional package is intentionally absent.
        )
        faithful_dry = load(faithful_mode / "result/DRY_RUN.json")
        if faithful_dry.get("network_request_sent") is not False or faithful_dry.get("candidate_count") != 3:
            raise SystemExit("Faithful dry-run audit is invalid")

        # Lite verifier synthetic session.
        prechecks = {name: {"pass": True, "checks": [], "notes": ""} for name in candidates}
        save(lite_mode / "prechecks.json", prechecks)
        lite_config = {
            "session_id": "SMOKE_LITE",
            "problem_file": "runs/SMOKE-A/problem/VERIFIER_PROBLEM.md",
            "candidate_files": {cid: f"runs/SMOKE-A/modes/reference-verifier-lite/candidates/{cid}.txt" for cid in candidates},
            "criteria": [{"id": "correct", "description": "correct answer", "weight": 1.0}],
            "prechecks_file": "runs/SMOKE-A/modes/reference-verifier-lite/prechecks.json",
            "pivots": 2,
            "seed": 0,
            "review_repeats_per_orientation": 1,
            "min_winner_margin": 0.03,
            "output_dir": "runs/SMOKE-A/modes/reference-verifier-lite/session",
        }
        lite_config_path = lite_mode / "config.json"
        save(lite_config_path, lite_config)
        invoke([sys.executable, str(TOOLS / "verifier_lite.py"), "init", "--config", str(lite_config_path)], env)
        lite_session = lite_mode / "session"
        _fill_lite_reviews(lite_session, "stage1_assignments")
        invoke([sys.executable, str(TOOLS / "verifier_lite.py"), "advance", "--session", str(lite_session)], env)
        _fill_lite_reviews(lite_session, "stage2_assignments")
        invoke([sys.executable, str(TOOLS / "verifier_lite.py"), "aggregate", "--session", str(lite_session)], env)
        lite_result = load(lite_session / "RESULT.json")
        if lite_result.get("winner") != "A" or lite_result.get("faithful") is not False:
            raise SystemExit(f"Unexpected Lite verifier result: {lite_result}")

        # Runtime-neutral direct provider packet: DeepSeek dry-run only, no network and no budget use.
        invoke(
            [
                sys.executable,
                str(TOOLS / "provider_call.py"),
                "--run",
                "SMOKE-A",
                "--provider",
                "deepseek",
                "--role",
                "research-specialist",
                "--task",
                "Develop one bounded alternative route and state a falsification test.",
                "--include-run",
                "problem/PROBLEM_GRAPH.json",
                "--id",
                "DIRECT_DRY_TEST",
                "--reserve-usd",
                "1",
                "--ack-separate-billing",
                "--dry-run",
            ],
            env,
        )
        direct_request = load(run_dir / "modes/direct-api/DIRECT_DRY_TEST/REQUEST.json")
        direct_packet = (run_dir / "modes/direct-api/DIRECT_DRY_TEST/PACKET.md").read_text(encoding="utf-8")
        budget_after_direct = load(run_dir / "BUDGET.json")
        if direct_request.get("network_request_sent") is not False:
            raise SystemExit("Direct provider dry-run sent a request")
        if "research_specialist" not in direct_packet or "PROBLEM_GRAPH.json" not in direct_packet:
            raise SystemExit("Direct provider packet is missing role or included artifact")
        if float(budget_after_direct["api_providers"]["deepseek"].get("used_usd", 0)) != 0:
            raise SystemExit("Direct provider dry-run consumed DeepSeek budget")

        # Optional OpenAI adapter: request preview only; budget remains untouched.
        invoke(
            [
                sys.executable,
                str(TOOLS / "api_burst_openai.py"),
                "--run",
                "SMOKE-A",
                "--id",
                "API_DRY_TEST",
                "--prompt",
                "Investigate two independent interpretations of this synthetic issue.",
                "--subagents",
                "2",
                "--reserve-usd",
                "1",
                "--ack-separate-billing",
                "--dry-run",
            ],
            env,
        )
        request = load(run_dir / "modes/api-burst/API_DRY_TEST/REQUEST.json")
        budget = load(run_dir / "BUDGET.json")
        if request.get("network_request_sent") is not False or float(budget["openai_api"].get("used_usd", 0)) != 0:
            raise SystemExit("API dry-run sent a request or consumed budget")

        # Run packaging before tamper detection.
        refresh_ai_disclosure(run_dir, env)
        package_path = run_dir / "deliverables/packages/SMOKE-A.zip"
        invoke(
            [
                sys.executable,
                str(TOOLS / "harness.py"),
                "package",
                "--run",
                "SMOKE-A",
                "--out",
                str(package_path),
                "--allow-unvalidated",
                "--by",
                "SELFTEST",
                "--note",
                "Offline smoke-test package before final gate approval.",
            ],
            env,
        )
        if not package_path.exists() or not zipfile.is_zipfile(package_path):
            raise SystemExit("Run package is invalid")

        # Register one human-verified core claim so the paper pipeline can enforce evidence tracing.
        (run_dir / "results/smoke_result.json").write_text(
            json.dumps({"objective": 2.0, "feasible": True}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (run_dir / "synthesis/FINAL_MODEL_SPEC.md").write_text(
            "# Final model\n\nSynthetic final model uses z=2 and satisfies the test constraint.\n",
            encoding="utf-8",
        )
        invoke(
            [
                sys.executable, str(TOOLS / "harness.py"), "claim-add", "--run", "SMOKE-A",
                "--id", "CLAIM_SMOKE", "--claim", "The synthetic decision z=2 is feasible.",
                "--type", "result", "--paper-section", "model_and_solution", "--core",
            ],
            env,
        )
        invoke(
            [
                sys.executable, str(TOOLS / "harness.py"), "evidence-add", "--run", "SMOKE-A",
                "--id", "EVID_SMOKE", "--claim", "CLAIM_SMOKE", "--type", "experiment",
                "--artifact", str(run_dir / "experiments/runs/EXP_SMOKE/seed_1/metrics.json"),
                "--result", "objective=2.0; feasible=true", "--human-verified", "--by", "SELFTEST",
            ],
            env,
        )

        _prepare_pre_freeze_approvals(run_dir, env)
        for gate_id in ("G0", "G1", "G2", "G3", "G4"):
            invoke(
                [
                    sys.executable,
                    str(TOOLS / "harness.py"),
                    "validate",
                    "--run",
                    "SMOKE-A",
                    "--gate",
                    gate_id,
                    "--strict",
                ],
                env,
            )
            invoke(
                [
                    sys.executable,
                    str(TOOLS / "harness.py"),
                    "review",
                    "--run",
                    "SMOKE-A",
                    "--gate",
                    gate_id,
                ],
                env,
            )
            invoke(
                [
                    sys.executable,
                    str(TOOLS / "harness.py"),
                    "approve",
                    "--run",
                    "SMOKE-A",
                    "--gate",
                    gate_id,
                    "--by",
                    "SELFTEST_REVIEWER",
                ],
                env,
            )

        # Freeze verification succeeds. The retained paper core then writes a
        # complete evidence-traceable synthetic paper independently of the v5.1
        # overlay regression in self_test_v51.py.
        invoke(
            [
                sys.executable,
                str(TOOLS / "harness.py"),
                "freeze",
                "--run",
                "SMOKE-A",
                "--by",
                "SELFTEST_REVIEWER",
                "--allow-before-g5",
            ],
            env,
        )
        invoke([sys.executable, str(TOOLS / "harness.py"), "verify-freeze", "--run", "SMOKE-A"], env)

        invoke([sys.executable, str(TOOLS / "paper.py"), "init", "--run", "SMOKE-A"], env)
        # An initialized-but-unwritten manuscript must not be able to pass G6.
        invoke(
            [sys.executable, str(TOOLS / "harness.py"), "validate", "--run", "SMOKE-A", "--gate", "G6", "--strict"],
            env,
            expected=(3,),
        )
        paper_dir = run_dir / "paper"
        plan = load(paper_dir / "PAPER_PLAN.json")
        section_ids = [item["id"] for item in plan["sections"]]
        if "ai_usage_declaration" not in section_ids:
            raise SystemExit("Default paper profile lacks ai_usage_declaration")
        if not (
            section_ids.index("evaluation_and_extension")
            < section_ids.index("ai_usage_declaration")
            < section_ids.index("references")
        ):
            raise SystemExit("AI usage declaration is not immediately before references")
        plan["working_title"] = "Synthetic evidence-first modeling paper"
        plan["narrative_thesis"] = "A simple feasible decision is derived and verified by a deterministic artifact."
        save(paper_dir / "PAPER_PLAN.json", plan)
        invoke(
            [sys.executable, str(TOOLS / "paper.py"), "map-claim", "--run", "SMOKE-A", "--claim", "CLAIM_SMOKE", "--section", "model_and_solution"],
            env,
        )
        (paper_dir / "PAPER_BRIEF.md").write_text(
            "# Paper brief\n\nWrite a short synthetic manuscript from frozen artifacts only; the only core claim is CLAIM_SMOKE.\n",
            encoding="utf-8",
        )
        (paper_dir / "SYMBOL_TABLE.csv").write_text(
            "symbol,meaning,unit,scope,source_path,verified,notes\nz,synthetic decision,1,global,synthesis/FINAL_MODEL_SPEC.md,true,\n",
            encoding="utf-8",
        )
        for section in plan["sections"]:
            section_path = run_dir / section["file"]
            body = f"# {section['title']}\n\nSynthetic self-test content for {section['id']}."
            if section["id"] == "ai_usage_declaration":
                body = (
                    "# AI工具使用声明\n\n"
                    "本参赛队在竞赛过程中使用了AI工具，主要用于离线回归测试，"
                    "详细使用情况见支撑材料。"
                )
            if section["id"] == "model_and_solution":
                body += "\n\n<!-- CLAIM:CLAIM_SMOKE -->\nThe synthetic decision is z=2 and the frozen result records feasible=true."
            section_path.write_text(body + "\n", encoding="utf-8")
        (paper_dir / "CONSISTENCY_AUDIT.md").write_text(
            "# Consistency audit\n\nChecked claim trace, frozen result, symbol and final statement; no blockers.\n",
            encoding="utf-8",
        )
        invoke([sys.executable, str(TOOLS / "paper.py"), "packet", "--run", "SMOKE-A", "--all"], env)
        invoke([sys.executable, str(TOOLS / "paper.py"), "assemble", "--run", "SMOKE-A"], env)
        # Paper audit inputs now require a canonical full-corpus scan receipt;
        # a header-only registry preloaded by the run template is intentionally
        # no longer accepted as proof that the scan ran.
        invoke(
            [
                sys.executable,
                str(TOOLS / "claim_guard.py"),
                "scan",
                "--run",
                "SMOKE-A",
                "--scope",
                "all",
            ],
            env,
        )
        invoke([sys.executable, str(TOOLS / "paper.py"), "lint", "--run", "SMOKE-A", "--strict", "--pre-audit"], env)
        invoke(
            [sys.executable, str(TOOLS / "paper.py"), "audit-set", "--run", "SMOKE-A", "--status", "passed", "--by", "SELFTEST", "--note", "synthetic audit passed"],
            env,
        )
        invoke([sys.executable, str(TOOLS / "paper.py"), "lint", "--run", "SMOKE-A", "--strict", "--pre-final"], env)
        invoke([sys.executable, str(TOOLS / "paper.py"), "finalize", "--run", "SMOKE-A"], env)
        integrated_g6 = invoke(
            [sys.executable, str(TOOLS / "harness.py"), "validate", "--run", "SMOKE-A", "--gate", "G6", "--strict"],
            env,
            expected=(3,),
        )
        if "v5.1" not in integrated_g6.stdout:
            raise SystemExit("G6 did not expose its integrated v5.1 stage blockers")
        final_text = (paper_dir / "FINAL_MANUSCRIPT.md").read_text(encoding="utf-8")
        if "CLAIM:CLAIM_SMOKE" in final_text or "SECTION_ID:" in final_text:
            raise SystemExit("Final manuscript leaked internal trace tags")
        if "The synthetic decision is z=2" not in final_text:
            raise SystemExit("Final manuscript is missing section content")

        # Audit approval is bound to the exact assembled draft: a later prose edit invalidates it.
        drift_section = paper_dir / "sections/020_problem_restatement.md"
        original_section = drift_section.read_text(encoding="utf-8")
        drift_section.write_text(original_section + "\nUnreviewed prose drift.\n", encoding="utf-8")
        invoke([sys.executable, str(TOOLS / "paper.py"), "assemble", "--run", "SMOKE-A"], env)
        invoke(
            [sys.executable, str(TOOLS / "paper.py"), "lint", "--run", "SMOKE-A", "--strict", "--pre-final"],
            env,
            expected=(4,),
        )
        drift_section.write_text(original_section, encoding="utf-8")
        invoke([sys.executable, str(TOOLS / "paper.py"), "assemble", "--run", "SMOKE-A"], env)
        invoke(
            [sys.executable, str(TOOLS / "paper.py"), "audit-set", "--run", "SMOKE-A", "--status", "passed", "--by", "SELFTEST", "--note", "synthetic re-audit after restoring section"],
            env,
        )
        invoke([sys.executable, str(TOOLS / "paper.py"), "finalize", "--run", "SMOKE-A"], env)

        # Package once more after paper finalization so delivery packaging covers the manuscript layer.
        refresh_ai_disclosure(run_dir, env)
        paper_package = run_dir / "deliverables/packages/SMOKE-A-paper.zip"
        invoke(
            [
                sys.executable,
                str(TOOLS / "harness.py"),
                "package",
                "--run",
                "SMOKE-A",
                "--out",
                str(paper_package),
                "--allow-unvalidated",
                "--by",
                "SELFTEST",
                "--note",
                "Offline smoke-test package after paper finalization.",
            ],
            env,
        )
        if not paper_package.exists() or not zipfile.is_zipfile(paper_package):
            raise SystemExit("Paper-inclusive run package is invalid")

        # Tamper detection still catches scientific changes after the paper has been finalized.
        graph_path.write_text(graph_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        invoke(
            [sys.executable, str(TOOLS / "harness.py"), "verify-freeze", "--run", "SMOKE-A"],
            env,
            expected=(4,),
        )


def main() -> None:
    static_checks()
    isolated_smoke()
    print(
        "OK: CUMCM Adaptive Research Harness v5.1 base-compatibility static + isolated smoke tests passed; "
        "no network/model/API calls made."
    )


if __name__ == "__main__":
    main()
