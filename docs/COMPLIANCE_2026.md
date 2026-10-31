# CUMCM 2026 compliance notes

This is an operational summary, not a substitute for reading the official rules.

- AI tools may include LLMs, generative AI, code assistants and AI agents.
- The team's core modeling and analysis must remain team-led.
- AI-participated content must be manually reviewed and verified item by item.
- Every team needs an `AI工具使用声明` before references, even when no AI was used.
- Supporting material for AI users must include `AI工具使用详情.pdf`, covering tool/model, purpose, prompt/process, adoption, modification and verification.
- Current-problem discussion or answer browsing on external platforms during the contest is prohibited.

These requirements come from the [official 2026 AI-use rules](https://www.mcm.edu.cn/html_cn/node/fef94648f2836ab6cc81586f4c38512b.html).
For a no-AI run, the statement must be exactly
`本参赛队在竞赛过程中未使用任何AI工具。`. For an AI-using run, use the
official prefix and suffix—`本参赛队在竞赛过程中使用了AI工具，主要用于……，详细使用情况见支撑材料。`—and
make its declared purpose agree with `AI_USAGE_LOG.jsonl`. The default paper plan
places this section at `paper/sections/085_ai_usage_declaration.md`.

When the log contains disclosable AI use, the harness records enough metadata to
draft the detailed disclosure, but the team must correct omissions and convert
the reviewed document to PDF. Save it as `deliverables/AI工具使用详情.pdf`, inspect
the rendered PDF, then run
`python tools/harness.py attest-ai-disclosure-pdf --by <队员姓名>`. No-AI runs do
not need that PDF or attestation receipt.

## G7 submission checks

G7 requires `deliverables/FINAL_MANUSCRIPT.pdf`. Build the separate official
support archive with:

```powershell
python tools/harness.py build-support --run <RUN_ID> `
  --include <run内文件> --by <队员姓名>
```

This writes `deliverables/official/支撑材料.zip` and the non-submittable internal
hash receipt `deliverables/internal/SUPPORT_PACKAGE_RECEIPT.json`.
The paper PDF and support ZIP are separate uploads and each must be no larger than
`20,000,000` bytes, per the [official paper-format rules](https://www.mcm.edu.cn/html_cn/node/4cd596519c9eb9fbd866398f6df0caa3.html).
When the log records disclosable AI use, the ZIP must contain the current attested
`AI工具使用详情.pdf`.

The harness scans searchable manuscript/support text, filenames, and PDF metadata
for identity information. That scan cannot reliably read identity text baked into
figures/images or prove that every rendered header, footer and page is anonymous.
A named team member must therefore inspect the complete paper and every support
member and complete the corresponding anonymity items in
`deliverables/FINAL_CHECKLIST.md`.

`python tools/harness.py package --by <队员姓名>` has a different purpose: it is
a named **internal audit archive** containing approvals and receipts. Never submit
that archive as the official anonymous supporting material.

Before approval, generate the concise human decision view with, for example,
`python tools/harness.py review --gate G7`. It is written to
`reviews/gates/G7_REVIEW.md`; approval binds that report and the current underlying
input hashes. The team member must open and read it before running `approve`.
Approval only validates the existing report; if it is missing, stale, tampered,
or contains a broken evidence link, approval fails without regenerating it.
