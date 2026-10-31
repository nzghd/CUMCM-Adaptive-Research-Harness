#!/usr/bin/env python3
"""Focused regression tests for the G7 submission-compliance audit."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from submission_compliance import (  # noqa: E402
    FINAL_PDF_RELATIVE,
    audit_submission_compliance,
)

UNUSED_DECLARATION = "本参赛队在竞赛过程中未使用任何AI工具。"


def _used_declaration(purpose: str) -> str:
    return (
        "本参赛队在竞赛过程中使用了AI工具，主要用于"
        f"{purpose}，详细使用情况见支撑材料。"
    )


def _pdf_bytes(size: int, *, author: str | None = None) -> bytes:
    metadata = f" /Author ({author})" if author is not None else ""
    base = (f"%PDF-1.4\n1 0 obj <<{metadata} >> endobj\n%%EOF\n").encode("utf-8")
    assert size >= len(base), (size, len(base))
    return base[:-6] + (b" " * (size - len(base))) + base[-6:]


def _case(root: Path, name: str, manuscript: str | None, pdf: bytes | None) -> Path:
    run = root / name
    if manuscript is not None:
        path = run / "paper" / "FINAL_MANUSCRIPT.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manuscript, encoding="utf-8")
    if pdf is not None:
        path = run / FINAL_PDF_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pdf)
    return run


def _compile(*, output: str | None = FINAL_PDF_RELATIVE, errors: list[str] | None = None) -> dict:
    return {"output_path": output, "errors": list(errors or [])}


def main() -> int:
    runs_root = ROOT / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    fixture_root = Path(tempfile.mkdtemp(prefix="_submission_compliance_", dir=runs_root))
    try:
        plain = (
            "# 模型正文\n\n结果经过复核。\n\n"
            "## AI工具使用声明\n\n"
            f"{UNUSED_DECLARATION}\n\n"
            "## 参考文献\n\n- Example.\n"
        )

        # The byte limit is inclusive and injectable for a cheap boundary test.
        boundary = _case(fixture_root, "boundary", plain, _pdf_bytes(128))
        result = audit_submission_compliance(
            boundary,
            _compile(),
            ai_event_count=1,  # trusted initialization event only
            ai_disclosure_count=0,
            policy={"submission_pdf_max_bytes": 128, "max_bytes": 1},
        )
        assert not result["errors"], result
        assert result["final_pdf"]["within_limit"] is True, result
        assert result["final_pdf"]["size"] == 128, result
        assert result["ai_statement"]["required"] is True, result
        assert result["ai_statement"]["declared_usage"] == "none", result
        assert result["ai_statement"]["log_usage"] == "none", result
        assert result["ai_statement"]["usage_consistent"] is True, result
        assert result["input_paths"] == [
            "deliverables/FINAL_MANUSCRIPT.pdf",
            "paper/FINAL_MANUSCRIPT.md",
        ], result

        widened = audit_submission_compliance(
            boundary,
            _compile(),
            policy={"submission_pdf_max_bytes": 20_000_001},
        )
        assert any("不得超过官方上限" in item for item in widened["errors"]), widened
        assert widened["final_pdf"]["max_bytes"] == 20_000_000, widened

        # Reference/anonymity policy stays injectable, but the official AI
        # declaration heading is fixed and cannot be configured away.
        custom_text = """# 正文

团队：测试队

## AI 使用说明

本参赛队在竞赛过程中使用了AI工具，主要用于代码调试，详细使用情况见支撑材料。

## Bibliography

- Example.
"""
        custom = _case(fixture_root, "custom_policy", custom_text, _pdf_bytes(160))
        result = audit_submission_compliance(
            custom,
            _compile(),
            ai_event_count=1,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={
                "submission_pdf_max_bytes": 160,
                "ai_statement_heading": "AI 使用说明",
                "reference_headings": ["Bibliography"],
                "anonymity_field_labels": ["团队"],
            },
        )
        assert result["ai_statement"]["heading_count"] == 0, result
        assert result["ai_statement"]["reference_heading_count"] == 1, result
        assert result["ai_statement"]["before_references"] is None, result
        assert result["ai_statement"]["declared_usage"] == "missing", result
        assert result["ai_statement"]["log_usage"] == "used", result
        assert result["ai_statement"]["usage_consistent"] is False, result
        assert any("官方固定标题" in item for item in result["errors"]), result
        assert result["anonymity"]["field_labels"] == ["团队"], result
        assert result["anonymity"]["count"] == 1, result
        assert result["anonymity"]["leaks"][0]["field"] == "团队", result

        over = audit_submission_compliance(
            boundary,
            _compile(),
            policy={"max_bytes": 127},
        )
        assert over["final_pdf"]["within_limit"] is False, over
        assert any("超过上限" in item for item in over["errors"]), over

        wrong_output = audit_submission_compliance(
            boundary,
            _compile(output="deliverables/other.pdf"),
            policy={"max_bytes": 128},
        )
        assert wrong_output["final_pdf"]["locked"] is False, wrong_output
        assert any("output_path" in item for item in wrong_output["errors"]), wrong_output

        missing = _case(fixture_root, "missing", plain, None)
        missing_required = audit_submission_compliance(missing, _compile(output=None))
        assert any("缺少最终提交 PDF" in item for item in missing_required["errors"]), missing_required
        missing_optional = audit_submission_compliance(
            missing,
            _compile(output=None),
            policy={"submission_pdf_required": False},
        )
        assert not missing_optional["errors"], missing_optional
        assert all("deliverables/" not in item for item in missing_optional["input_paths"]), missing_optional

        # Fenced fake headings are ignored; the real disclosure must be unique,
        # substantive, and precede the unique references heading.
        disclosed_text = """---
title: Fixture
# AI工具使用声明
# References
---

# 正文

```markdown
# AI工具使用声明
# References
```

## AI工具使用声明

本参赛队在竞赛过程中使用了AI工具，主要用于代码审查，详细使用情况见支撑材料。

## 参考文献

作者：该行属于参考文献，不是参赛者身份字段。
"""
        disclosed = _case(fixture_root, "disclosed", disclosed_text, _pdf_bytes(160))
        result = audit_submission_compliance(
            disclosed,
            _compile(),
            ai_event_count=2,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"max_bytes": 160},
        )
        assert not result["errors"], result
        assert result["ai_statement"]["heading_count"] == 1, result
        assert result["ai_statement"]["reference_heading_count"] == 1, result
        assert result["ai_statement"]["before_references"] is True, result
        assert result["ai_statement"]["body_present"] is True, result
        assert result["ai_statement"]["declared_usage"] == "used", result
        assert result["ai_statement"]["log_usage"] == "used", result
        assert result["ai_statement"]["usage_consistent"] is True, result
        assert result["ai_statement"]["purpose_consistency_attested"] is True, result
        assert result["anonymity"]["count"] == 0, result

        unreviewed_purpose = audit_submission_compliance(
            disclosed,
            _compile(),
            ai_event_count=2,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=False,
            policy={"max_bytes": 160},
        )
        assert any("ai-log-completeness-reviewed" in item for item in unreviewed_purpose["errors"]), unreviewed_purpose

        # The declaration is mandatory even with an empty/no-use log, and its
        # official binary statement must agree with real (non-sentinel) events.
        no_statement_text = "# 正文\n\n结果。\n\n## 参考文献\n\n- Example.\n"
        no_statement = _case(
            fixture_root,
            "no_statement",
            no_statement_text,
            _pdf_bytes(160),
        )
        result = audit_submission_compliance(
            no_statement,
            _compile(),
            ai_event_count=0,
            ai_disclosure_count=0,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert result["ai_statement"]["required"] is True, result
        assert result["ai_statement"]["declared_usage"] == "missing", result
        assert result["ai_statement"]["log_usage"] == "none", result
        assert result["ai_statement"]["usage_consistent"] is False, result
        assert any("必须且只能包含一个 ATX" in item for item in result["errors"]), result

        false_used_text = (
            "# 正文\n\n## AI工具使用声明\n\n"
            f"{_used_declaration('语言润色')}\n\n"
            "## 参考文献\n\n- Example.\n"
        )
        false_used = _case(fixture_root, "false_used", false_used_text, _pdf_bytes(160))
        result = audit_submission_compliance(
            false_used,
            _compile(),
            ai_event_count=0,
            ai_disclosure_count=0,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert result["ai_statement"]["declared_usage"] == "used", result
        assert result["ai_statement"]["log_usage"] == "none", result
        assert result["ai_statement"]["usage_consistent"] is False, result
        assert any("AI 日志" in item for item in result["errors"]), result

        impossible_counts = audit_submission_compliance(
            false_used,
            _compile(),
            ai_event_count=0,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert any("不能大于" in item for item in impossible_counts["errors"]), impossible_counts

        false_unused_text = (
            "# 正文\n\n## AI工具使用声明\n\n"
            f"{UNUSED_DECLARATION}\n\n"
            "## 参考文献\n\n- Example.\n"
        )
        false_unused = _case(
            fixture_root,
            "false_unused",
            false_unused_text,
            _pdf_bytes(160),
        )
        result = audit_submission_compliance(
            false_unused,
            _compile(),
            ai_event_count=2,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert result["ai_statement"]["declared_usage"] == "none", result
        assert result["ai_statement"]["log_usage"] == "used", result
        assert result["ai_statement"]["usage_consistent"] is False, result
        assert any("AI 日志" in item for item in result["errors"]), result

        both_text = (
            "# 正文\n\n## AI工具使用声明\n\n"
            f"{UNUSED_DECLARATION}\n\n"
            f"{_used_declaration('代码调试')}\n\n"
            "## 参考文献\n\n- Example.\n"
        )
        both = _case(fixture_root, "both_declarations", both_text, _pdf_bytes(160))
        result = audit_submission_compliance(
            both,
            _compile(),
            ai_event_count=1,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert result["ai_statement"]["declared_usage"] == "ambiguous", result
        assert result["ai_statement"]["usage_consistent"] is False, result
        assert any("二选一" in item for item in result["errors"]), result

        placeholder_used_text = false_used_text.replace("语言润色", "〖简要用途，如语言润色、代码调试等〗")
        placeholder_used = _case(
            fixture_root,
            "placeholder_used",
            placeholder_used_text,
            _pdf_bytes(160),
        )
        result = audit_submission_compliance(
            placeholder_used,
            _compile(),
            ai_event_count=1,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert result["ai_statement"]["declared_usage"] == "invalid", result
        assert result["ai_statement"]["usage_consistent"] is False, result
        assert any("用途" in item for item in result["errors"]), result

        punctuation_used_text = false_used_text.replace("语言润色", "。")
        punctuation_used = _case(
            fixture_root,
            "punctuation_used",
            punctuation_used_text,
            _pdf_bytes(160),
        )
        result = audit_submission_compliance(
            punctuation_used,
            _compile(),
            ai_event_count=1,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert result["ai_statement"]["declared_usage"] == "invalid", result
        assert any("用途" in item for item in result["errors"]), result

        nonexact_unused_text = false_unused_text.replace("任何AI工具", "任何 AI 工具")
        nonexact_unused = _case(
            fixture_root,
            "nonexact_unused",
            nonexact_unused_text,
            _pdf_bytes(160),
        )
        result = audit_submission_compliance(
            nonexact_unused,
            _compile(),
            ai_disclosure_count=0,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert result["ai_statement"]["declared_usage"] == "invalid", result
        assert result["ai_statement"]["usage_consistent"] is False, result

        # Multi-line HTML comments are invisible to both heading and identity
        # scans, and filtering must not renumber the remaining source lines.
        commented_text = """# 正文

<!--
## AI工具使用声明
队员姓名：注释中的示例
-->

## 9 参考文献

- Example.
"""
        commented = _case(fixture_root, "commented", commented_text, _pdf_bytes(160))
        result = audit_submission_compliance(
            commented,
            _compile(),
            ai_event_count=1,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert result["ai_statement"]["heading_count"] == 0, result
        assert result["ai_statement"]["reference_heading_count"] == 1, result
        assert result["ai_statement"]["reference_heading_lines"] == [8], result
        assert result["anonymity"]["count"] == 0, result
        assert any("AI工具使用声明" in item for item in result["errors"]), result

        chinese_numbered = commented_text.replace(
            "## 9 参考文献", "## 九、参考文献"
        ).replace(
            "<!--\n## AI工具使用声明\n队员姓名：注释中的示例\n-->",
            f"## AI工具使用声明\n\n{_used_declaration('代码调试')} ",
        )
        numbered = _case(
            fixture_root,
            "numbered_reference",
            chinese_numbered,
            _pdf_bytes(160),
        )
        result = audit_submission_compliance(
            numbered,
            _compile(),
            ai_event_count=1,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"submission_pdf_max_bytes": 160},
        )
        assert not result["errors"], result
        assert result["ai_statement"]["before_references"] is True, result

        reverse_text = """# 正文

## References

- Example.

## AI工具使用声明

本参赛队在竞赛过程中使用了AI工具，主要用于语言润色，详细使用情况见支撑材料。
"""
        reverse = _case(fixture_root, "reverse", reverse_text, _pdf_bytes(160))
        result = audit_submission_compliance(
            reverse,
            _compile(),
            ai_event_count=1,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"max_bytes": 160},
        )
        assert any("位于参考文献之前" in item for item in result["errors"]), result

        duplicate_text = disclosed_text.replace(
            "## 参考文献", "## AI工具使用声明\n\n再次声明。\n\n## 参考文献"
        )
        duplicate = _case(fixture_root, "duplicate", duplicate_text, _pdf_bytes(160))
        result = audit_submission_compliance(
            duplicate,
            _compile(),
            ai_event_count=1,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"max_bytes": 160},
        )
        assert result["ai_statement"]["heading_count"] == 2, result
        assert any("必须且只能包含一个 ATX" in item for item in result["errors"]), result

        empty_text = "# 正文\n\n## AI工具使用声明\n\nTODO\n\n## 参考文献\n\n- Example.\n"
        empty = _case(fixture_root, "empty", empty_text, _pdf_bytes(160))
        result = audit_submission_compliance(
            empty,
            _compile(),
            ai_event_count=1,
            ai_disclosure_count=1,
            ai_purpose_consistency_attested=True,
            policy={"max_bytes": 160},
        )
        assert any("没有可识别的正文" in item for item in result["errors"]), result

        # Narrow anonymity scan: explicit valued identity fields are caught,
        # while prose, physical units, fenced examples, and bibliography authors
        # are not treated as team identity leaks.
        identity_text = """---
author: 张三
authors:
  - 赵六
affiliation: Example University
affiliations: [Example Institute]
institute: Example Research Institute
单位: 北京大学数学科学学院
---

# 模型正文

某大学数据集仅用作公开样例。

单位：m/s

```yaml
队员姓名: 围栏中的示例
```

队员姓名：李四

## AI工具使用声明

本参赛队在竞赛过程中未使用任何AI工具。

## 参考文献

作者：王五
"""
        identity = _case(fixture_root, "identity", identity_text, _pdf_bytes(180))
        result = audit_submission_compliance(
            identity,
            _compile(),
            policy={"max_bytes": 180},
        )
        markdown_leaks = [
            item for item in result["anonymity"]["leaks"] if item["source"] != "pdf_metadata"
        ]
        assert {(item["field"].casefold(), item["value"]) for item in markdown_leaks} == {
            ("author", "张三"),
            ("authors", "- 赵六"),
            ("affiliation", "Example University"),
            ("affiliations", "[Example Institute]"),
            ("institute", "Example Research Institute"),
            ("单位", "北京大学数学科学学院"),
            ("队员姓名", "李四"),
        }, result
        assert all(item["value"] != "王五" for item in markdown_leaks), result
        assert all(item["value"] != "m/s" for item in markdown_leaks), result

        metadata = _case(
            fixture_root,
            "metadata",
            plain,
            _pdf_bytes(180, author="Alice Chen"),
        )
        result = audit_submission_compliance(
            metadata,
            _compile(),
            policy={"max_bytes": 180},
        )
        assert any(
            item["field"] == "PDF /Author" and item["value"] == "Alice Chen"
            for item in result["anonymity"]["leaks"]
        ), result

        print("submission compliance self-test: PASS")
        return 0
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
