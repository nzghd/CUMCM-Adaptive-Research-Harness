#!/usr/bin/env python3
"""Low-coupling G7 checks for the final submission artifacts.

The caller remains responsible for validating the compile and AI-export
receipts.  This module consumes those audit summaries and adds the three
human-facing delivery checks: canonical PDF/size, in-paper AI disclosure
placement, and a deliberately narrow anonymity scan.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Mapping


FINAL_PDF_RELATIVE = "deliverables/FINAL_MANUSCRIPT.pdf"
FINAL_MARKDOWN_RELATIVE = "paper/FINAL_MANUSCRIPT.md"
DEFAULT_MAX_BYTES = 20_000_000

_DEFAULT_AI_HEADING = "AI工具使用声明"
_DEFAULT_REFERENCE_HEADINGS = ("参考文献", "References")
UNUSED_AI_DECLARATION = "本参赛队在竞赛过程中未使用任何AI工具。"
USED_AI_DECLARATION_PREFIX = "本参赛队在竞赛过程中使用了AI工具，主要用于"
USED_AI_DECLARATION_SUFFIX = "，详细使用情况见支撑材料。"
_IDENTITY_LABELS = (
    "参赛学校",
    "参赛队号",
    "队员姓名",
    "指导教师",
    "联系电话",
    "学校",
    "单位",
    "队号",
    "队员",
    "作者",
    "邮箱",
    "author",
    "authors",
    "affiliation",
    "affiliations",
    "institute",
)
_AFFILIATION_RE = re.compile(
    r"大学|学院|学校|中学|研究院|研究所|实验室|公司|中心|医院|协会|组织|"
    r"university|college|school|institute|laborator(?:y|ies)|\blab\b|"
    r"company|corporation|centre|center|hospital",
    re.IGNORECASE,
)
_NON_IDENTITY_VALUES = frozenset(
    {
        "匿名",
        "anonymous",
        "none",
        "null",
        "n/a",
        "na",
        "不适用",
        "未填写",
        "待填写",
        "todo",
        "tbd",
        "无",
        "-",
    }
)
_GENERIC_TABLE_VALUES = frozenset({"值", "内容", "信息", "填写内容", "value", "单位"})
_PURPOSE_PLACEHOLDER_TOKENS = (
    "todo",
    "tbd",
    "待填写",
    "待补充",
    "简要用途",
    "〖",
    "〗",
    "{{",
    "}}",
    "<用途>",
    "[用途]",
)


def _normalise_count(value: Any, *, label: str, errors: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"{label} 必须是非负整数")
        return 0
    return value


def _string_list_policy(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[str, ...],
    errors: list[str],
) -> tuple[str, ...]:
    value = raw.get(key, list(default))
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        errors.append(f"submission policy: {key} 必须是非空字符串数组")
        return default
    unique: list[str] = []
    seen: set[str] = set()
    for item in value:
        clean = item.strip()
        folded = clean.casefold()
        if folded not in seen:
            seen.add(folded)
            unique.append(clean)
    return tuple(unique)


def _policy_values(
    policy: Mapping[str, Any] | None,
    errors: list[str],
) -> tuple[bool, int, str, tuple[str, ...], tuple[str, ...]]:
    raw = dict(policy or {})
    required = raw.get("submission_pdf_required", True)
    if not isinstance(required, bool):
        errors.append("submission policy: submission_pdf_required 必须是布尔值")
        required = True
    max_bytes = raw.get(
        "submission_pdf_max_bytes",
        raw.get("max_bytes", raw.get("max_submission_bytes", DEFAULT_MAX_BYTES)),
    )
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        errors.append("submission policy: submission_pdf_max_bytes 必须是正整数")
        max_bytes = DEFAULT_MAX_BYTES
    elif max_bytes > DEFAULT_MAX_BYTES:
        errors.append(
            "submission policy: submission_pdf_max_bytes 不得超过官方上限 "
            f"{DEFAULT_MAX_BYTES} bytes"
        )
        max_bytes = DEFAULT_MAX_BYTES
    ai_heading = raw.get("ai_statement_heading", _DEFAULT_AI_HEADING)
    if not isinstance(ai_heading, str) or not ai_heading.strip():
        errors.append("submission policy: ai_statement_heading 必须是非空字符串")
        ai_heading = _DEFAULT_AI_HEADING
    elif ai_heading.strip() != _DEFAULT_AI_HEADING:
        errors.append(
            "submission policy: ai_statement_heading 是官方固定标题，必须为"
            f"“{_DEFAULT_AI_HEADING}”"
        )
        ai_heading = _DEFAULT_AI_HEADING
    references = _string_list_policy(
        raw,
        "reference_headings",
        _DEFAULT_REFERENCE_HEADINGS,
        errors,
    )
    labels = _string_list_policy(
        raw,
        "anonymity_field_labels",
        _IDENTITY_LABELS,
        errors,
    )
    return required, max_bytes, ai_heading.strip(), references, labels


def _field_patterns(labels: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    label_pattern = "|".join(
        re.escape(item) for item in sorted(labels, key=len, reverse=True)
    )
    field = re.compile(
        rf"^(?:\*\*|__)?(?P<label>{label_pattern})(?:\*\*|__)?"
        r"\s*[：:]\s*(?P<value>.+?)\s*$",
        re.IGNORECASE,
    )
    table = re.compile(
        rf"^\s*\|\s*(?:\*\*|__)?(?P<label>{label_pattern})(?:\*\*|__)?"
        r"\s*\|\s*(?P<value>[^|]+?)\s*\|",
        re.IGNORECASE,
    )
    yaml = re.compile(
        rf"^(?P<indent>\s*)(?:['\"])?(?P<label>{label_pattern})(?:['\"])?"
        r"\s*:\s*(?P<value>.*?)\s*$",
        re.IGNORECASE,
    )
    return field, table, yaml


def _canonical_existing_file(run_root: Path, relative: str) -> tuple[Path | None, str | None]:
    candidate = run_root / relative
    if not candidate.is_file():
        return None, None
    try:
        resolved = candidate.resolve()
        actual_relative = resolved.relative_to(run_root).as_posix()
    except (OSError, ValueError) as exc:
        return None, f"{relative} 不在当前 run 内：{exc}"
    if actual_relative != relative:
        return None, f"{relative} 是符号链接或非 canonical 路径"
    return resolved, None


def _parse_atx_heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^[ ]{0,3}(#{1,6})(?:[ \t]+|$)(.*)$", line)
    if not match:
        return None
    title = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2)).strip()
    return len(match.group(1)), title


def _normalise_heading(title: str) -> str:
    value = title.strip()
    for marker in ("**", "__", "`"):
        if value.startswith(marker) and value.endswith(marker) and len(value) > len(marker) * 2:
            value = value[len(marker) : -len(marker)].strip()
    return re.sub(r"\s+", " ", value).casefold()


def _normalise_reference_heading(title: str) -> str:
    """Normalise a reference heading, accepting a conservative section number."""
    value = _normalise_heading(title)
    return re.sub(
        r"^(?:(?:\d+(?:\.\d+)*)|(?:[零〇一二三四五六七八九十百]+))"
        r"(?:\s*[、．.)]\s*|\s+)",
        "",
        value,
        count=1,
    ).strip()


def _without_html_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Remove HTML comments from one line while carrying block state forward."""
    visible: list[str] = []
    cursor = 0
    while cursor < len(line):
        if in_comment:
            closing = line.find("-->", cursor)
            if closing < 0:
                return "".join(visible), True
            cursor = closing + 3
            in_comment = False
            continue
        opening = line.find("<!--", cursor)
        if opening < 0:
            visible.append(line[cursor:])
            break
        visible.append(line[cursor:opening])
        cursor = opening + 4
        in_comment = True
    return "".join(visible), in_comment


def _visible_markdown_lines(text: str) -> list[dict[str, Any]]:
    """Return non-fenced lines with line numbers and parsed ATX headings."""
    visible: list[dict[str, Any]] = []
    fence_char: str | None = None
    fence_length = 0
    in_html_comment = False
    raw_lines = text.splitlines()
    frontmatter = _yaml_frontmatter_range(raw_lines)
    frontmatter_lines = (
        set(range(frontmatter[0] + 1, frontmatter[1] + 2)) if frontmatter else set()
    )
    for number, line in enumerate(raw_lines, 1):
        if number in frontmatter_lines:
            continue
        if fence_char is not None:
            closing = re.match(r"^[ ]{0,3}([`~]+)[ \t]*$", line)
            if (
                closing
                and closing.group(1)[0] == fence_char
                and len(closing.group(1)) >= fence_length
            ):
                fence_char = None
                fence_length = 0
            continue
        # A fence opener at the beginning of a visible line wins over comment
        # markers contained in the code info/content.  Outside fences, remove
        # both one-line and multi-line HTML comments before any heading or
        # identity-field parsing while retaining the original line number.
        if not in_html_comment:
            opening = re.match(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$", line)
            if opening:
                fence_char = opening.group(1)[0]
                fence_length = len(opening.group(1))
                continue
        line, in_html_comment = _without_html_comments(line, in_html_comment)
        opening = re.match(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$", line)
        if opening:
            fence_char = opening.group(1)[0]
            fence_length = len(opening.group(1))
            continue
        heading = _parse_atx_heading(line)
        visible.append(
            {
                "line": number,
                "text": line,
                "heading": heading,
            }
        )
    return visible


def _headings(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(lines):
        heading = item["heading"]
        if heading is None:
            continue
        level, title = heading
        result.append(
            {
                "index": index,
                "line": item["line"],
                "level": level,
                "title": title,
                "normalised": _normalise_heading(title),
            }
        )
    return result


def _substantive_body_line(line: str) -> bool:
    value = re.sub(r"<!--.*?-->", "", line).strip()
    value = re.sub(r"^(?:>\s*)?(?:[-+*]|\d+[.)])\s+", "", value).strip()
    value = value.strip("*_`~ ")
    if not value or re.fullmatch(r"[-=_]{3,}", value):
        return False
    return value.casefold() not in {"todo", "tbd", "待填写", "待补充", "暂无", "...", "……"}


def _statement_body_lines(
    lines: list[dict[str, Any]],
    statement_heading: dict[str, Any],
) -> list[dict[str, Any]]:
    body: list[dict[str, Any]] = []
    for item in lines[statement_heading["index"] + 1 :]:
        heading = item["heading"]
        if heading is not None and heading[0] <= statement_heading["level"]:
            break
        if heading is None and item["text"].strip():
            body.append(item)
    return body


def _purpose_is_valid(value: str) -> bool:
    purpose = value.strip()
    compact = re.sub(r"\s+", "", purpose)
    if not compact or compact.casefold() in {"...", "……", "无", "不适用", "n/a", "na"}:
        return False
    meaningful = re.findall(r"[A-Za-z0-9\u3400-\u9fff]", compact)
    if len(meaningful) < 2:
        return False
    folded = purpose.casefold()
    return not any(token in folded for token in _PURPOSE_PLACEHOLDER_TOKENS)


def _classify_ai_declaration(body_text: str) -> tuple[str, str | None, str | None]:
    """Classify the official binary declaration and return an optional defect."""
    declaration = body_text.strip()
    unused_count = declaration.count(UNUSED_AI_DECLARATION)
    used_pattern = re.compile(
        re.escape(USED_AI_DECLARATION_PREFIX)
        + r"(?P<purpose>.*?)"
        + re.escape(USED_AI_DECLARATION_SUFFIX)
    )
    used_matches = list(used_pattern.finditer(declaration))
    if unused_count and used_matches:
        return "ambiguous", None, "AI 工具使用声明必须在官方二选一文本中选择且只能出现一条"
    if unused_count > 1 or len(used_matches) > 1:
        return "ambiguous", None, "AI 工具使用声明必须在官方二选一文本中选择且只能出现一条"
    if unused_count == 1:
        if declaration == UNUSED_AI_DECLARATION:
            return "none", None, None
        return "invalid", None, "未使用声明必须逐字采用官方文本且不得附加其他内容"
    if len(used_matches) == 1:
        purpose = used_matches[0].group("purpose")
        if not _purpose_is_valid(purpose):
            return "invalid", purpose or None, "已使用声明的用途不得为空或使用占位文本"
        if used_matches[0].span() != (0, len(declaration)):
            return "invalid", purpose, "已使用声明必须采用官方前缀、用途和支撑材料后缀"
        return "used", purpose, None
    return (
        "invalid",
        None,
        "声明正文必须逐字使用官方未使用句，或使用官方已使用模板并填写非占位用途",
    )


def _ai_statement_audit(
    lines: list[dict[str, Any]] | None,
    *,
    required: bool,
    expected_heading: str,
    reference_headings: tuple[str, ...],
    ai_event_count: int,
    disclosure_event_count: int,
    purpose_consistency_attested: bool,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    heading_rows = _headings(lines or [])
    expected_normalised = _normalise_heading(expected_heading)
    reference_normalised = frozenset(
        _normalise_reference_heading(item) for item in reference_headings
    )
    statement_rows = [
        item for item in heading_rows if item["normalised"] == expected_normalised
    ]
    reference_rows = [
        item
        for item in heading_rows
        if _normalise_reference_heading(item["title"]) in reference_normalised
    ]
    body_rows = (
        _statement_body_lines(lines or [], statement_rows[0])
        if len(statement_rows) == 1
        else []
    )
    body_line_count = sum(_substantive_body_line(item["text"]) for item in body_rows)
    body_text = "\n".join(item["text"].strip() for item in body_rows).strip()
    if not statement_rows:
        declared_usage = "missing"
        declared_purpose = None
        declaration_defect = None
    elif len(statement_rows) > 1:
        declared_usage = "ambiguous"
        declared_purpose = None
        declaration_defect = "AI 工具使用声明必须在官方二选一文本中选择且只能出现一条"
    else:
        declared_usage, declared_purpose, declaration_defect = _classify_ai_declaration(
            body_text
        )
    log_usage = "used" if disclosure_event_count > 0 else "none"
    usage_consistent = declared_usage == log_usage
    before_references: bool | None = None
    if len(statement_rows) == 1 and len(reference_rows) == 1:
        before_references = statement_rows[0]["line"] < reference_rows[0]["line"]

    if required:
        if lines is None:
            errors.append("所有参赛队均须设置 AI 工具使用声明，但无法读取 paper/FINAL_MANUSCRIPT.md")
        if len(statement_rows) != 1:
            errors.append(
                "所有参赛队的正文必须且只能包含一个 ATX 标题"
                f"“{expected_heading}”"
            )
        if len(reference_rows) != 1:
            errors.append(
                "所有参赛队的正文必须且只能包含一个参考文献 ATX 标题（"
                + " / ".join(reference_headings)
                + "）"
            )
        if len(statement_rows) == 1 and body_line_count == 0:
            errors.append(f"“{expected_heading}”节没有可识别的正文")
        if before_references is False:
            errors.append(f"“{expected_heading}”必须位于参考文献之前")
        if declaration_defect:
            errors.append(declaration_defect)
        if declared_usage in {"used", "none"} and not usage_consistent:
            errors.append(
                f"AI 日志表明 {log_usage}，但论文声明为 {declared_usage}；二者必须一致"
            )
        if declared_usage == "used" and usage_consistent and not purpose_consistency_attested:
            errors.append(
                "已使用声明的简要用途必须由具名队员在 FINAL_CHECKLIST 的 "
                "ai-log-completeness-reviewed 项确认与 AI_USAGE_LOG 一致"
            )
    elif len(statement_rows) > 1 or len(reference_rows) > 1:
        warnings.append("正文中的 AI 声明或参考文献标题不唯一")

    return {
        "required": required,
        "expected_heading": expected_heading,
        "accepted_reference_headings": list(reference_headings),
        "ai_event_count": ai_event_count,
        "disclosure_event_count": disclosure_event_count,
        "manuscript_path": FINAL_MARKDOWN_RELATIVE,
        "heading_count": len(statement_rows),
        "heading_lines": [item["line"] for item in statement_rows],
        "reference_heading_count": len(reference_rows),
        "reference_heading_lines": [item["line"] for item in reference_rows],
        "before_references": before_references,
        "body_present": body_line_count > 0,
        "body_line_count": body_line_count,
        "declared_usage": declared_usage,
        "declared_purpose": declared_purpose,
        "log_usage": log_usage,
        "usage_consistent": usage_consistent,
        "purpose_consistency_attested": purpose_consistency_attested,
    }


def _reference_line_numbers(
    lines: list[dict[str, Any]],
    headings: list[dict[str, Any]],
    reference_headings: tuple[str, ...],
) -> set[int]:
    excluded: set[int] = set()
    accepted = frozenset(
        _normalise_reference_heading(item) for item in reference_headings
    )
    references = [
        item
        for item in headings
        if _normalise_reference_heading(item["title"]) in accepted
    ]
    for reference in references:
        for item in lines[reference["index"] :]:
            heading = item["heading"]
            if (
                item["line"] != reference["line"]
                and heading is not None
                and heading[0] <= reference["level"]
            ):
                break
            excluded.add(item["line"])
    return excluded


def _clean_field_value(value: str) -> str:
    cleaned = re.sub(r"<!--.*?-->", "", value).strip()
    cleaned = cleaned.strip("*_`| ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:240]


def _identity_value_is_sensitive(label: str, value: str) -> bool:
    clean = _clean_field_value(value)
    if not clean or clean.casefold() in _NON_IDENTITY_VALUES:
        return False
    if label.casefold() == "单位":
        # "单位：m/s" is normal scientific prose, not an affiliation leak.
        return _AFFILIATION_RE.search(clean) is not None
    return True


def _yaml_frontmatter_range(raw_lines: list[str]) -> tuple[int, int] | None:
    if not raw_lines or raw_lines[0].lstrip("\ufeff").strip() != "---":
        return None
    for index in range(1, min(len(raw_lines), 200)):
        if raw_lines[index].strip() in {"---", "..."}:
            return 0, index
    return None


def _yaml_identity_leaks(
    raw_lines: list[str],
    yaml_field_re: re.Pattern[str],
) -> tuple[list[dict[str, Any]], set[int]]:
    bounds = _yaml_frontmatter_range(raw_lines)
    if bounds is None:
        return [], set()
    start, end = bounds
    leaks: list[dict[str, Any]] = []
    yaml_lines = set(range(start + 1, end + 2))
    for index in range(start + 1, end):
        match = yaml_field_re.match(raw_lines[index])
        if not match:
            continue
        label = match.group("label")
        value = match.group("value")
        if not value.strip():
            indent = len(match.group("indent"))
            nested: list[str] = []
            for following in raw_lines[index + 1 : end]:
                if not following.strip() or following.lstrip().startswith("#"):
                    continue
                next_indent = len(following) - len(following.lstrip())
                if next_indent <= indent:
                    break
                nested.append(following.strip())
            value = " ".join(nested)
        if _identity_value_is_sensitive(label, value):
            leaks.append(
                {
                    "source": "markdown_yaml",
                    "line": index + 1,
                    "field": label,
                    "value": _clean_field_value(value),
                }
            )
    return leaks, yaml_lines


def _strip_markdown_field_prefix(line: str) -> str:
    value = line.strip()
    # Remove only structural prefixes at the beginning; prose remains prose.
    for _ in range(3):
        updated = re.sub(r"^(?:>|#{1,6}|[-+*]|\d+[.)])\s+", "", value).strip()
        if updated == value:
            break
        value = updated
    if (
        (value.startswith("**") and value.endswith("**"))
        or (value.startswith("__") and value.endswith("__"))
    ):
        value = value[2:-2].strip()
    return value


def _markdown_identity_leaks(
    text: str,
    lines: list[dict[str, Any]],
    *,
    reference_headings: tuple[str, ...],
    field_labels: tuple[str, ...],
) -> list[dict[str, Any]]:
    raw_lines = text.splitlines()
    field_re, table_field_re, yaml_field_re = _field_patterns(field_labels)
    leaks, yaml_line_numbers = _yaml_identity_leaks(raw_lines, yaml_field_re)
    headings = _headings(lines)
    reference_lines = _reference_line_numbers(lines, headings, reference_headings)
    seen = {(item["line"], item["field"].casefold(), item["value"]) for item in leaks}
    for item in lines:
        line_number = item["line"]
        if line_number in yaml_line_numbers or line_number in reference_lines:
            continue
        raw = item["text"]
        match = table_field_re.match(raw)
        table_field = match is not None
        if match is None:
            match = field_re.match(_strip_markdown_field_prefix(raw))
        if match is None:
            continue
        label = match.group("label")
        value = _clean_field_value(match.group("value"))
        if table_field and value.casefold() in _GENERIC_TABLE_VALUES:
            continue
        if not _identity_value_is_sensitive(label, value):
            continue
        key = (line_number, label.casefold(), value)
        if key in seen:
            continue
        seen.add(key)
        leaks.append(
            {
                "source": "markdown_field",
                "line": line_number,
                "field": label,
                "value": value,
            }
        )
    return leaks


def _decode_pdf_literal(raw: bytes) -> str:
    value = re.sub(rb"\\([nrtbf()\\])", lambda m: {
        b"n": b"\n",
        b"r": b"\r",
        b"t": b"\t",
        b"b": b"\b",
        b"f": b"\f",
        b"(": b"(",
        b")": b")",
        b"\\": b"\\",
    }[m.group(1)], raw)
    value = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) % 256]), value)
    if value.startswith(b"\xfe\xff"):
        return value[2:].decode("utf-16-be", errors="replace").strip()
    return value.decode("utf-8", errors="replace").strip()


def _decode_pdf_hex(raw: bytes) -> str:
    compact = re.sub(rb"\s+", b"", raw)
    if len(compact) % 2:
        compact += b"0"
    try:
        value = bytes.fromhex(compact.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if value.startswith(b"\xfe\xff"):
        return value[2:].decode("utf-16-be", errors="replace").strip()
    return value.decode("utf-8", errors="replace").strip()


def _pdf_author_leaks(pdf_path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        size = pdf_path.stat().st_size
        with pdf_path.open("rb") as handle:
            if size <= 8_000_000:
                payload = handle.read()
            else:
                payload = handle.read(4_000_000)
                handle.seek(max(0, size - 4_000_000))
                payload += b"\n" + handle.read(4_000_000)
    except OSError as exc:
        return [], f"无法读取 PDF 元数据字节：{exc}"

    values: list[tuple[str, str]] = []
    for match in re.finditer(rb"/Author\s*\(((?:\\.|[^\\)])*)\)", payload, re.DOTALL):
        values.append(("PDF /Author", _decode_pdf_literal(match.group(1))))
    for match in re.finditer(rb"/Author\s*<(?!<)([0-9A-Fa-f\s]+)>", payload):
        values.append(("PDF /Author", _decode_pdf_hex(match.group(1))))
    for match in re.finditer(rb"/Author\s*/([^\s/<>{}\[\]()]+)", payload):
        values.append(("PDF /Author", match.group(1).decode("latin-1", errors="replace")))

    text = payload.decode("latin-1", errors="ignore")
    for match in re.finditer(
        r"<dc:creator\b[^>]*>.*?<rdf:li\b[^>]*>(.*?)</rdf:li>.*?</dc:creator>",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        values.append(("PDF XMP dc:creator", html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))))

    leaks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for field, raw_value in values:
        value = re.sub(r"\s+", " ", raw_value).strip()[:240]
        if not value or value.casefold() in _NON_IDENTITY_VALUES:
            continue
        key = (field, value)
        if key in seen:
            continue
        seen.add(key)
        leaks.append(
            {
                "source": "pdf_metadata",
                "line": None,
                "field": field,
                "value": value,
            }
        )
    scope_note = (
        "PDF 元数据使用原始字节轻量扫描；大于 8,000,000 bytes 时只扫描首尾各 "
        "4,000,000 bytes，不能替代完整 PDF 解析或 OCR。"
        if size > 8_000_000
        else None
    )
    return leaks, scope_note


def audit_submission_compliance(
    run: Path,
    paper_compile_audit: Mapping[str, Any],
    ai_event_count: int = 0,
    ai_disclosure_count: int = 0,
    ai_purpose_consistency_attested: bool = False,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Audit final-delivery policy without reading harness global state.

    ``paper_compile_audit`` must be the current compile audit result.  The
    in-paper declaration is always required.  ``ai_disclosure_count > 0``
    selects the official "used" branch; the trusted initialization sentinel may
    contribute to ``ai_event_count`` without selecting that branch.
    """
    errors: list[str] = []
    warnings: list[str] = []
    input_paths: list[str] = []
    run_root = Path(run).resolve()
    (
        required,
        max_bytes,
        ai_statement_heading,
        reference_headings,
        anonymity_field_labels,
    ) = _policy_values(policy, errors)
    ai_events = _normalise_count(ai_event_count, label="ai_event_count", errors=errors)
    disclosure_events = _normalise_count(
        ai_disclosure_count,
        label="ai_disclosure_count",
        errors=errors,
    )
    if not isinstance(ai_purpose_consistency_attested, bool):
        errors.append("ai_purpose_consistency_attested 必须是布尔值")
        ai_purpose_consistency_attested = False
    if disclosure_events > ai_events:
        errors.append("ai_disclosure_count 不能大于 ai_event_count")

    compile_result = paper_compile_audit if isinstance(paper_compile_audit, Mapping) else {}
    if not isinstance(paper_compile_audit, Mapping):
        errors.append("paper_compile_audit 必须是对象")
    raw_output = compile_result.get("output_path")
    bound_output = raw_output if isinstance(raw_output, str) and raw_output else None
    compile_errors = compile_result.get("errors", [])
    compile_clean = isinstance(compile_errors, list) and not compile_errors
    if not isinstance(compile_errors, list):
        compile_clean = False
    output_is_canonical = bound_output == FINAL_PDF_RELATIVE
    if bound_output is not None and not output_is_canonical:
        errors.append(
            "paper compile audit 的 output_path 必须是 canonical "
            f"{FINAL_PDF_RELATIVE}"
        )
    if compile_errors:
        errors.append("paper compile audit 尚未通过，最终 PDF 不能视为已锁定")
    if required and bound_output is None:
        errors.append("G7 要求 compile audit 以 output_path 锁定最终提交 PDF")

    final_path, final_path_error = _canonical_existing_file(run_root, FINAL_PDF_RELATIVE)
    if final_path_error:
        errors.append(final_path_error)
    final_size: int | None = None
    within_limit: bool | None = None
    if final_path is not None:
        input_paths.append(FINAL_PDF_RELATIVE)
        try:
            final_size = final_path.stat().st_size
        except OSError as exc:
            errors.append(f"无法读取最终 PDF 大小：{exc}")
        if final_size is not None:
            within_limit = 0 < final_size <= max_bytes
            if final_size <= 0:
                errors.append("最终提交 PDF 为空文件")
            elif final_size > max_bytes:
                errors.append(
                    f"最终提交 PDF 为 {final_size} bytes，超过上限 {max_bytes} bytes"
                )
        if not output_is_canonical or not compile_clean:
            errors.append("现有 FINAL_MANUSCRIPT.pdf 未被干净的 compile audit 锁定")
    elif required:
        errors.append(f"缺少最终提交 PDF：{FINAL_PDF_RELATIVE}")

    manuscript_path, manuscript_path_error = _canonical_existing_file(
        run_root, FINAL_MARKDOWN_RELATIVE
    )
    if manuscript_path_error:
        errors.append(manuscript_path_error)
    manuscript_text: str | None = None
    visible_lines: list[dict[str, Any]] | None = None
    if manuscript_path is not None:
        input_paths.append(FINAL_MARKDOWN_RELATIVE)
        try:
            manuscript_text = manuscript_path.read_text(encoding="utf-8-sig")
            visible_lines = _visible_markdown_lines(manuscript_text)
        except (OSError, UnicodeError) as exc:
            errors.append(f"无法读取 {FINAL_MARKDOWN_RELATIVE}：{exc}")
    else:
        warnings.append("缺少 FINAL_MANUSCRIPT.md，匿名性正文扫描不可用")

    ai_statement = _ai_statement_audit(
        visible_lines,
        required=True,
        expected_heading=ai_statement_heading,
        reference_headings=reference_headings,
        ai_event_count=ai_events,
        disclosure_event_count=disclosure_events,
        purpose_consistency_attested=ai_purpose_consistency_attested,
        errors=errors,
        warnings=warnings,
    )

    anonymity_leaks: list[dict[str, Any]] = []
    if manuscript_text is not None and visible_lines is not None:
        anonymity_leaks.extend(
            _markdown_identity_leaks(
                manuscript_text,
                visible_lines,
                reference_headings=reference_headings,
                field_labels=anonymity_field_labels,
            )
        )
    pdf_scope_note: str | None = None
    if final_path is not None:
        pdf_leaks, pdf_scope_note = _pdf_author_leaks(final_path)
        anonymity_leaks.extend(pdf_leaks)
        if pdf_scope_note:
            warnings.append(pdf_scope_note)
    if anonymity_leaks:
        locations = ", ".join(
            (
                f"line {item['line']} ({item['field']})"
                if item["line"] is not None
                else str(item["field"])
            )
            for item in anonymity_leaks[:8]
        )
        suffix = " …" if len(anonymity_leaks) > 8 else ""
        errors.append(f"匿名性扫描发现 {len(anonymity_leaks)} 个明确身份字段：{locations}{suffix}")

    return {
        "errors": errors,
        "warnings": warnings,
        "input_paths": list(dict.fromkeys(input_paths)),
        "final_pdf": {
            "path": FINAL_PDF_RELATIVE,
            "bound_output_path": bound_output,
            "required": required,
            "exists": final_path is not None,
            "locked": bool(final_path is not None and output_is_canonical and compile_clean),
            "size": final_size,
            "max_bytes": max_bytes,
            "within_limit": within_limit,
        },
        "ai_statement": ai_statement,
        "anonymity": {
            "leaks": anonymity_leaks,
            "count": len(anonymity_leaks),
            "machine_scope": (
                "扫描 FINAL_MANUSCRIPT.md 中 fenced-code 之外、参考文献节之外的显式身份字段与 "
                "YAML front matter，并轻量扫描最终 PDF 的 /Author 与 XMP dc:creator；"
                "不做正文实体识别、完整 PDF 解析或 OCR，仍需人工终审。"
            ),
            "field_labels": list(anonymity_field_labels),
            "pdf_scope_note": pdf_scope_note,
        },
    }


__all__ = [
    "DEFAULT_MAX_BYTES",
    "FINAL_MARKDOWN_RELATIVE",
    "FINAL_PDF_RELATIVE",
    "UNUSED_AI_DECLARATION",
    "USED_AI_DECLARATION_PREFIX",
    "USED_AI_DECLARATION_SUFFIX",
    "audit_submission_compliance",
]
