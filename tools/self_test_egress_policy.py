#!/usr/bin/env python3
"""Focused, offline regression tests for run egress and attempt accounting.

All fixtures live below ``runs/`` and every provider/subprocess boundary is
mocked.  This test must never make a network request or consume paid API use.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import api_burst_openai  # noqa: E402
import common  # noqa: E402
import external_review  # noqa: E402
import harness  # noqa: E402
import launch_claude_deepseek  # noqa: E402
import provider_call  # noqa: E402
import v51_common  # noqa: E402
import verifier_faithful  # noqa: E402


def ns(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def expect_exit(func, *args: object) -> int:
    try:
        func(*args)
    except SystemExit as exc:
        return int(exc.code or 0)
    raise AssertionError("expected SystemExit")


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def set_phase(run: Path, phase: str | None) -> None:
    meta = json.loads((run / "RUN_META.json").read_text(encoding="utf-8"))
    if phase is None:
        meta.pop("phase", None)
    else:
        meta["phase"] = phase
    write_json(run / "RUN_META.json", meta)


def budget_payload() -> dict[str, object]:
    providers = {
        name: {
            "enabled": True,
            "max_usd": 10.0,
            "used_usd": 0.0,
            "default_model": f"test-{name}",
            "call_count": 0,
            "usage_records": [],
        }
        for name in ("openai", "anthropic", "deepseek")
    }
    return {
        "version": "5.1.0",
        "updated_at": common.now_iso(),
        "api_providers": providers,
        "openai_api": {
            **dict(providers["openai"]),
            "max_concurrent_subagents": 3,
        },
        "manual_pro": {"enabled": True, "max_dialogues": 2, "used_dialogues": 0},
    }


def provider_args(run_id: str, ident: str, *, dry_run: bool = False) -> argparse.Namespace:
    return ns(
        run=run_id,
        provider="openai",
        role="research-specialist",
        task="Synthetic offline egress-policy test.",
        task_file=None,
        include_run=[],
        include_project=[],
        model="test-openai",
        id=ident,
        stage="egress-self-test",
        purpose="Offline failure-path regression",
        max_input_bytes=10000,
        max_output_tokens=16,
        reserve_usd=1.0,
        actual_usd=0.0,
        ack_separate_billing=True,
        dry_run=dry_run,
    )


def burst_args(run_id: str, ident: str, *, dry_run: bool = False) -> argparse.Namespace:
    return ns(
        run=run_id,
        id=ident,
        prompt="Synthetic offline burst.",
        prompt_file=None,
        purpose="Offline failure-path regression",
        developer_instructions=None,
        model="test-openai",
        subagents=1,
        max_output_tokens=16,
        reasoning_effort=None,
        reserve_usd=2.0,
        actual_usd=0.0,
        ack_separate_billing=True,
        dry_run=dry_run,
    )


def external_args(run_id: str, packet_id: str, kind: str = "blind-idea") -> argparse.Namespace:
    return ns(
        run=run_id,
        kind=kind,
        packet_id=packet_id,
        out=None,
        max_embedded_bytes=64,
        include_problem_graph=False,
        replace=False,
    )


def faithful_config(run: Path, session: str, output_name: str) -> Path:
    base = run / "modes/reference-verifier-faithful" / session
    candidates = base / "candidates"
    candidates.mkdir(parents=True, exist_ok=True)
    problem = base / "problem.md"
    problem.write_text("Synthetic problem.\n", encoding="utf-8")
    (candidates / "A.txt").write_text("Candidate A.\n", encoding="utf-8")
    (candidates / "B.txt").write_text("Candidate B.\n", encoding="utf-8")
    payload = {
        "session_id": session,
        "problem_file": common.relative_to_root(problem),
        "candidate_files": {
            "A": common.relative_to_root(candidates / "A.txt"),
            "B": common.relative_to_root(candidates / "B.txt"),
        },
        "criteria": {"correct": "synthetic"},
        "model": "test-model",
        "n_evaluations": 1,
        "pivots": 1,
        "seed": 0,
        "output_dir": common.relative_to_root(base / output_name),
    }
    path = base / "config.json"
    write_json(path, payload)
    return path


def faithful_args(
    run_id: str,
    config: Path,
    provider: str,
    *,
    reserve_usd: float,
) -> argparse.Namespace:
    return ns(
        run=run_id,
        config=str(config),
        provider=provider,
        reserve_usd=reserve_usd,
        actual_usd=0.0 if provider != "local" else None,
        ack_separate_billing=provider != "local",
        replace=False,
        allow_flat=False,
        allow_version_mismatch=False,
    )


def launcher_args(run_id: str, ident: str, *, dry_run: bool = False) -> argparse.Namespace:
    return ns(
        run=run_id,
        id=ident,
        purpose="Offline launcher regression",
        reserve_usd=2.0,
        actual_usd=0.0,
        ack_separate_billing=True,
        dry_run=dry_run,
        claude_args=["--", "--version"],
    )


def main() -> int:
    run_id = "_SELFTEST_EGRESS_POLICY"
    run = ROOT / "runs" / run_id
    sibling = ROOT / "runs" / "_SELFTEST_EGRESS_OUTSIDE"
    for candidate in (run, sibling):
        if candidate.exists():
            shutil.rmtree(candidate)
    try:
        (run / "ai").mkdir(parents=True)
        write_json(
            run / "RUN_META.json",
            {
                "version": "5.1.0",
                "run_id": run_id,
                "phase": "competition",
                "project_root": str(ROOT),
                "run_path": f"runs/{run_id}",
            },
        )
        write_json(run / "STATUS.json", {"run_id": run_id})
        initial_budget_payload = budget_payload()
        write_json(run / "BUDGET.json", initial_budget_payload)
        initial_budget = (run / "BUDGET.json").read_bytes()

        # A run reference is a canonical direct child with a matching envelope;
        # runs/ itself and nested directories are rejected before tool writes.
        assert expect_exit(common.resolve_run, ".") == 2
        assert expect_exit(v51_common.resolve_run_flexible, ".") == 2
        nested = run / "nested"
        nested.mkdir()
        assert expect_exit(common.resolve_run, str(nested)) == 2

        # G0 binds immutable policy, not live consumption counters. G7 binds
        # and reconciles the full BUDGET.json separately.
        baseline_policy = harness._budget_policy_projection(budget_payload())
        consumed = budget_payload()
        consumed["updated_at"] = "2099-01-01T00:00:00+00:00"
        consumed["api_providers"]["openai"]["used_usd"] = 4.0
        consumed["api_providers"]["openai"]["call_count"] = 1
        consumed["api_providers"]["openai"]["usage_records"] = [{"id": "X"}]
        consumed["manual_pro"]["used_dialogues"] = 1
        assert harness._budget_policy_projection(consumed) == baseline_policy
        changed_policy = budget_payload()
        changed_policy["api_providers"]["openai"]["max_usd"] = 11.0
        assert harness._budget_policy_projection(changed_policy) != baseline_policy
        g0_gate = {"requires": ["BUDGET.json"]}
        g0_manifest, g0_before = harness._gate_input_snapshot(
            run, "G0", g0_gate, []
        )
        assert any(
            row["kind"] == "semantic-policy" and row["path"] == "BUDGET.json#policy"
            for row in g0_manifest
        )
        assert not any(
            row["kind"] == "artifact" and row["path"] == "BUDGET.json"
            for row in g0_manifest
        )
        write_json(run / "BUDGET.json", consumed)
        _, g0_after_consumption = harness._gate_input_snapshot(
            run, "G0", g0_gate, []
        )
        assert g0_after_consumption == g0_before
        write_json(run / "BUDGET.json", changed_policy)
        _, g0_after_policy_change = harness._gate_input_snapshot(
            run, "G0", g0_gate, []
        )
        assert g0_after_policy_change != g0_before
        write_json(run / "BUDGET.json", initial_budget_payload)
        assert (run / "BUDGET.json").read_bytes() == initial_budget

        # Competition rejects every external boundary before output creation,
        # budget mutation, subprocess execution, or provider invocation.
        provider_called = False

        def forbidden_provider(*_args: object, **_kwargs: object):
            nonlocal provider_called
            provider_called = True
            raise AssertionError("competition policy reached provider")

        with patch.object(provider_call, "_call_openai", forbidden_provider):
            assert expect_exit(provider_call.cmd_run, provider_args(run_id, "COMP_PROVIDER")) == 2
        assert not provider_called
        assert not (run / "modes/direct-api/COMP_PROVIDER").exists()

        assert expect_exit(
            api_burst_openai.cmd_run,
            burst_args(run_id, "COMP_BURST"),
        ) == 2
        assert not (run / "modes/api-burst/COMP_BURST").exists()

        assert expect_exit(
            external_review.cmd_export,
            external_args(run_id, "COMP_PACKET"),
        ) == 2
        assert not (run / "external/COMP_PACKET").exists()

        comp_faithful_config = faithful_config(run, "COMP_FAITHFUL", "result")
        faithful_called = False

        def forbidden_subprocess(*_args: object, **_kwargs: object):
            nonlocal faithful_called
            faithful_called = True
            raise AssertionError("competition policy reached subprocess")

        with patch.object(verifier_faithful.subprocess, "run", forbidden_subprocess):
            assert expect_exit(
                verifier_faithful.cmd_run,
                faithful_args(run_id, comp_faithful_config, "deepseek", reserve_usd=1.0),
            ) == 2
        assert not faithful_called
        assert not (
            run / "modes/reference-verifier-faithful/COMP_FAITHFUL/result"
        ).exists()
        assert (run / "BUDGET.json").read_bytes() == initial_budget
        assert common.read_jsonl(run / "ai/AI_USAGE_LOG.jsonl") == []

        launcher_called = False

        def forbidden_launcher(*_args: object, **_kwargs: object):
            nonlocal launcher_called
            launcher_called = True
            raise AssertionError("competition policy reached Claude launcher")

        with patch.object(launch_claude_deepseek.subprocess, "run", forbidden_launcher):
            assert expect_exit(
                launch_claude_deepseek.cmd_launch,
                launcher_args(run_id, "COMP_LAUNCH"),
            ) == 2
        assert not launcher_called
        assert not (run / "modes/claude-code-deepseek/COMP_LAUNCH").exists()

        with patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": "http://127.0.0.1:8000/v1"},
            clear=False,
        ):
            assert verifier_faithful._provider_is_external("local") is False
        with patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": "https://api.example.invalid/v1"},
            clear=False,
        ):
            assert expect_exit(verifier_faithful._provider_is_external, "local") == 2

        # A competition dry-run is a local preview and remains allowed.
        assert provider_call.cmd_run(provider_args(run_id, "COMP_DRY", dry_run=True)) == 0
        dry_request = common.read_json(
            run / "modes/direct-api/COMP_DRY/REQUEST.json", expect=dict
        )
        assert dry_request["status"] == "prepared"
        assert dry_request["network_request_sent"] is False
        assert common.read_jsonl(run / "ai/AI_USAGE_LOG.jsonl") == []
        assert (run / "BUDGET.json").read_bytes() == initial_budget

        # Missing/unknown phase is rejected even for a dry-run, before writes.
        set_phase(run, "not-a-phase")
        assert expect_exit(
            provider_call.cmd_run,
            provider_args(run_id, "UNKNOWN_DRY", dry_run=True),
        ) == 2
        assert not (run / "modes/direct-api/UNKNOWN_DRY").exists()
        set_phase(run, None)
        assert expect_exit(
            external_review.cmd_export,
            external_args(run_id, "MISSING_PACKET"),
        ) == 2
        assert not (run / "external/MISSING_PACKET").exists()

        # Rehearsal transport failures remain fully visible and conservatively
        # reserve max(reserve, caller_actual), so actual_usd=0 cannot bypass.
        set_phase(run, "rehearsal")

        # Every directory identifier is a single safe component. Traversal is
        # rejected before provider invocation or filesystem creation.
        traversal_target = ROOT / "_SELFTEST_EGRESS_ESCAPE"
        assert not traversal_target.exists()
        traversal_provider = provider_args(run_id, "../../../../_SELFTEST_EGRESS_ESCAPE")
        with patch.object(provider_call, "_call_openai", forbidden_provider):
            assert expect_exit(provider_call.cmd_run, traversal_provider) == 2
        traversal_burst = burst_args(run_id, "../../../../_SELFTEST_EGRESS_ESCAPE")
        assert expect_exit(api_burst_openai.cmd_run, traversal_burst) == 2
        for unsafe in ("CON", "con.txt", "PRN.json", "COM1.log", "LPT9", "safe.", " spaced"):
            unsafe_provider = provider_args(run_id, unsafe)
            assert expect_exit(provider_call.cmd_run, unsafe_provider) == 2
        assert not traversal_target.exists()

        unsafe_faithful = faithful_config(run, "UNSAFE_FAITHFUL", "result")
        unsafe_faithful_payload = common.read_json(unsafe_faithful, expect=dict)
        unsafe_faithful_payload["candidate_files"] = {
            "../../../../_SELFTEST_EGRESS_ESCAPE": next(
                iter(unsafe_faithful_payload["candidate_files"].values())
            ),
            "SAFE": list(unsafe_faithful_payload["candidate_files"].values())[1],
        }
        write_json(unsafe_faithful, unsafe_faithful_payload)
        assert expect_exit(
            verifier_faithful.cmd_run,
            faithful_args(run_id, unsafe_faithful, "deepseek", reserve_usd=1.0),
        ) == 2
        assert not (unsafe_faithful.parent / "result").exists()
        assert not traversal_target.exists()

        provider_called = False

        def failing_provider(*_args: object, **_kwargs: object):
            nonlocal provider_called
            provider_called = True
            raise RuntimeError("synthetic provider failure")

        with patch.object(provider_call, "_call_openai", failing_provider):
            try:
                provider_call.cmd_run(provider_args(run_id, "FAIL_PROVIDER"))
            except RuntimeError as exc:
                assert "synthetic provider failure" in str(exc)
            else:
                raise AssertionError("provider failure did not propagate")
        assert provider_called
        failed_request = common.read_json(
            run / "modes/direct-api/FAIL_PROVIDER/REQUEST.json", expect=dict
        )
        assert failed_request["status"] == "failed-or-unknown"
        assert failed_request["network_request_sent"] is None
        assert (run / "modes/direct-api/FAIL_PROVIDER/ERROR.json").is_file()
        events = common.read_jsonl(run / "ai/AI_USAGE_LOG.jsonl")
        assert [row["event_id"] for row in events] == ["FAIL_PROVIDER"]
        budget = common.load_budget(run)
        assert budget["api_providers"]["openai"]["used_usd"] == 1.0
        record = budget["api_providers"]["openai"]["usage_records"][0]
        assert record["reserved_usd"] == 1.0
        assert record["caller_actual_usd"] == 0.0
        assert record["status"] == "failed-or-unknown"

        # A second one-dollar attempt cannot pass a 1.5-dollar cap: the failed
        # first attempt was not silently refunded.
        budget["api_providers"]["openai"]["max_usd"] = 1.5
        budget["openai_api"]["max_usd"] = 1.5
        common.save_budget(run, budget)
        provider_called = False
        with patch.object(provider_call, "_call_openai", forbidden_provider):
            assert expect_exit(provider_call.cmd_run, provider_args(run_id, "BUDGET_BLOCK")) == 2
        assert not provider_called
        assert common.load_budget(run)["api_providers"]["openai"]["used_usd"] == 1.0

        # Burst uses the same pre-call reservation and logs an exception before
        # propagating it. Restore sufficient budget first.
        budget = common.load_budget(run)
        budget["api_providers"]["openai"]["max_usd"] = 10.0
        budget["openai_api"]["max_usd"] = 10.0
        common.save_budget(run, budget)

        class FakeResponses:
            def create(self, **_kwargs: object) -> object:
                raise ConnectionError("synthetic burst transport failure")

        fake_client = types.SimpleNamespace(
            beta=types.SimpleNamespace(responses=FakeResponses())
        )
        fake_openai = types.SimpleNamespace(OpenAI=lambda: fake_client)
        with (
            patch.dict(sys.modules, {"openai": fake_openai}),
            patch.dict(os.environ, {"OPENAI_API_KEY": "offline-test-key"}),
        ):
            try:
                api_burst_openai.cmd_run(burst_args(run_id, "FAIL_BURST"))
            except ConnectionError:
                pass
            else:
                raise AssertionError("burst failure did not propagate")
        burst_request = common.read_json(
            run / "modes/api-burst/FAIL_BURST/REQUEST.json", expect=dict
        )
        assert burst_request["status"] == "failed-or-unknown"
        assert burst_request["network_request_sent"] is None
        assert (run / "modes/api-burst/FAIL_BURST/ERROR.json").is_file()
        budget = common.load_budget(run)
        assert budget["api_providers"]["openai"]["used_usd"] == 3.0
        assert budget["openai_api"]["used_usd"] == 3.0
        assert any(row["event_id"] == "FAIL_BURST" for row in common.read_jsonl(run / "ai/AI_USAGE_LOG.jsonl"))

        # Faithful output must stay in the explicit run. This rejection occurs
        # before _prepare can create or replace the foreign directory.
        outside_config = faithful_config(run, "OUTSIDE_FAITHFUL", "local-result")
        payload = common.read_json(outside_config, expect=dict)
        payload["output_dir"] = common.relative_to_root(sibling)
        write_json(outside_config, payload)
        assert expect_exit(
            verifier_faithful.cmd_run,
            faithful_args(run_id, outside_config, "deepseek", reserve_usd=1.0),
        ) == 2
        assert not sibling.exists()

        # A non-zero faithful subprocess is still an external AI attempt: its
        # execution, stderr, AI disclosure event, and reserved budget survive.
        faithful_path = faithful_config(run, "FAIL_FAITHFUL", "result")

        def failed_process(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 7, "synthetic stdout\n", "synthetic stderr\n")

        with patch.object(verifier_faithful.subprocess, "run", failed_process):
            code = verifier_faithful.cmd_run(
                faithful_args(run_id, faithful_path, "deepseek", reserve_usd=1.0)
            )
        assert code == 7
        faithful_result = run / "modes/reference-verifier-faithful/FAIL_FAITHFUL/result"
        execution = common.read_json(faithful_result / "EXECUTION.json", expect=dict)
        assert execution["status"] == "failed-or-unknown"
        assert execution["network_request_sent"] is None
        assert (faithful_result / "ERROR.json").is_file()
        assert (faithful_result / "STDERR.log").read_text(encoding="utf-8") == "synthetic stderr\n"
        assert any(
            row["event_id"] == "FAITHFUL_FAIL_FAITHFUL"
            for row in common.read_jsonl(run / "ai/AI_USAGE_LOG.jsonl")
        )
        deepseek = common.load_budget(run)["api_providers"]["deepseek"]
        assert deepseek["used_usd"] == 1.0
        assert deepseek["usage_records"][0]["status"] == "failed-or-unknown"

        # The shell launchers now delegate to this run-aware helper. A failed
        # interactive child is still logged and its conservative reserve stays.
        def failed_launcher(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal launcher_called
            launcher_called = True
            assert _kwargs["env"]["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
            assert "DEEPSEEK_API_KEY" in _kwargs["env"]
            assert "OPENAI_API_KEY" not in _kwargs["env"]
            return subprocess.CompletedProcess(command, 9)

        launcher_called = False
        with (
            patch.object(launch_claude_deepseek.shutil, "which", return_value="claude-test"),
            patch.object(launch_claude_deepseek.subprocess, "run", failed_launcher),
            patch.dict(
                os.environ,
                {
                    "DEEPSEEK_API_KEY": "offline-test-key",
                    "OPENAI_API_KEY": "must-not-reach-child",
                },
            ),
        ):
            code = launch_claude_deepseek.cmd_launch(
                launcher_args(run_id, "FAIL_CLAUDE_DEEPSEEK")
            )
        assert code == 9 and launcher_called
        launch_dir = run / "modes/claude-code-deepseek/FAIL_CLAUDE_DEEPSEEK"
        launch_execution = common.read_json(launch_dir / "EXECUTION.json", expect=dict)
        assert launch_execution["status"] == "failed-or-unknown"
        assert launch_execution["network_request_sent"] is None
        assert (launch_dir / "ERROR.json").is_file()
        deepseek = common.load_budget(run)["api_providers"]["deepseek"]
        assert deepseek["used_usd"] == 3.0
        assert deepseek["usage_records"][-1]["status"] == "failed-or-unknown"

        # Rehearsal external packets correctly disclose what they contain.
        assert external_review.cmd_export(
            external_args(run_id, "PAPER_PACKET", kind="paper-review")
        ) == 0
        packet_meta = common.read_json(run / "external/PAPER_PACKET/META.json", expect=dict)
        assert packet_meta["contains_current_problem"] is True
        assert packet_meta["contains_current_ideas"] is True
        assert packet_meta["intended_external_transfer"] is True
        assert packet_meta["packet_sha256"] == common.sha256_file(
            run / "external/PAPER_PACKET/PACKET.md"
        )

        # Explicit --out is run-confined even with --replace. An outside
        # directory and its marker must survive the rejected request.
        sibling.mkdir(parents=True)
        marker = sibling / "DO_NOT_DELETE.txt"
        marker.write_text("preserve\n", encoding="utf-8")
        outside_args = external_args(run_id, "OUTSIDE_PACKET")
        outside_args.out = str(sibling)
        outside_args.replace = True
        assert expect_exit(external_review.cmd_export, outside_args) == 2
        assert marker.read_text(encoding="utf-8") == "preserve\n"

        # Imported identifiers and external Idea IDs cannot escape their run.
        write_json(run / "ideas/IDEA_REGISTRY.json", {"next_id": 1, "ideas": []})
        malicious_response = run / "malicious_idea.json"
        write_json(
            malicious_response,
            {
                "id": "../../../../_SELFTEST_EGRESS_ESCAPE",
                "title": "malicious",
                "target_nodes": ["Q1"],
                "summary": "must not escape",
                "method_families": ["test"],
                "validation_plan": ["reject traversal"],
                "main_risks": ["path traversal"],
            },
        )
        malicious_import = ns(
            run=run_id,
            kind="blind-idea",
            response=str(malicious_response),
            tool="offline-test",
            model="none",
            packet_id=None,
            import_id="SAFE_IMPORT",
            register_idea=True,
            adopted=False,
            modified=False,
            verified=False,
            by="",
            note="",
        )
        assert expect_exit(external_review.cmd_import, malicious_import) == 2
        assert not traversal_target.exists()
        malicious_import.import_id = "../../../../_SELFTEST_EGRESS_ESCAPE"
        malicious_import.register_idea = False
        assert expect_exit(external_review.cmd_import, malicious_import) == 2
        assert not traversal_target.exists()

        # All real failed attempts are valid terminal records. Their request,
        # AI event, positive reservation, ERROR.json and retained cost reconcile.
        audit = harness._egress_attempt_audit(run)
        assert audit["errors"] == [], audit["errors"]
        assert audit["external_attempt_count"] == 4
        assert "BUDGET.json" in audit["input_paths"]
        assert "modes/direct-api/FAIL_PROVIDER/ERROR.json" in audit["input_paths"]
        g7_manifest, _ = harness._gate_input_snapshot(
            run,
            "G7",
            {"requires": ["BUDGET.json"]},
            [],
            extra_run_paths=audit["input_paths"],
        )
        assert any(
            row["kind"] == "artifact" and row["path"] == "BUDGET.json"
            for row in g7_manifest
        )
        assert any(
            row["kind"] == "referenced-artifact"
            and row["path"] == "modes/direct-api/FAIL_PROVIDER/ERROR.json"
            for row in g7_manifest
        )
        retained_openai = common.load_budget(run)["api_providers"]["openai"]["used_usd"]

        # Non-terminal state, reservation mismatch and orphan reservations each
        # fail closed; restoring the artifacts restores a clean audit.
        request_path = run / "modes/direct-api/FAIL_PROVIDER/REQUEST.json"
        saved_request = common.read_json(request_path, expect=dict)
        nonterminal = dict(saved_request)
        nonterminal["status"] = "attempted"
        nonterminal["completed_at"] = None
        write_json(request_path, nonterminal)
        assert any(
            "non-terminal" in item
            for item in harness._egress_attempt_audit(run)["errors"]
        )
        write_json(request_path, saved_request)

        saved_budget = common.load_budget(run)
        mismatched = json.loads(json.dumps(saved_budget))
        mismatched["api_providers"]["openai"]["usage_records"][0]["status"] = "succeeded"
        mismatched["openai_api"] = {
            **mismatched["openai_api"],
            "usage_records": mismatched["api_providers"]["openai"]["usage_records"],
        }
        write_json(run / "BUDGET.json", mismatched)
        assert any(
            "status does not match" in item
            for item in harness._egress_attempt_audit(run)["errors"]
        )

        orphaned = json.loads(json.dumps(saved_budget))
        orphan_record = {
            "id": "ORPHAN_ATTEMPT",
            "provider": "deepseek",
            "mode": "direct-api",
            "status": "failed-or-unknown",
            "reserved_usd": 0.25,
        }
        orphaned["api_providers"]["deepseek"]["usage_records"].append(orphan_record)
        orphaned["api_providers"]["deepseek"]["call_count"] += 1
        orphaned["api_providers"]["deepseek"]["used_usd"] += 0.25
        write_json(run / "BUDGET.json", orphaned)
        assert any(
            "ORPHAN_ATTEMPT" in item and "orphaned" in item
            for item in harness._egress_attempt_audit(run)["errors"]
        )
        write_json(run / "BUDGET.json", saved_budget)
        assert harness._egress_attempt_audit(run)["errors"] == []
        assert common.load_budget(run)["api_providers"]["openai"]["used_usd"] == retained_openai

        log_path = run / "ai/AI_USAGE_LOG.jsonl"
        saved_log = log_path.read_text(encoding="utf-8")
        common.append_ai_usage_event(
            run,
            event_id="ORPHAN_AI_EVENT",
            tool="OpenAI Responses API Multi-agent beta",
            model="offline-test",
            stage="api-burst",
            purpose="orphan regression",
            prompt_or_task_summary="must be rejected",
            output_artifacts=["modes/api-burst/ORPHAN_AI_EVENT/REQUEST.json"],
        )
        assert any(
            "ORPHAN_AI_EVENT" in item
            for item in harness._egress_attempt_audit(run)["errors"]
        )
        log_path.write_text(saved_log, encoding="utf-8")
        assert harness._egress_attempt_audit(run)["errors"] == []

        print("egress policy self-test passed")
        return 0
    finally:
        for candidate in (run, sibling):
            resolved = candidate.resolve()
            if resolved.parent != (ROOT / "runs").resolve():
                raise AssertionError(f"refusing unsafe cleanup target: {resolved}")
            if resolved.exists():
                shutil.rmtree(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
