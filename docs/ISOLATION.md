# Isolation model

The harness uses several levels of isolation. They are not equivalent.

## 1. Prompt and directory isolation

During first-round exploration, each specialist is instructed to read only:

- original inputs;
- approved Intake artifacts;
- its own bounded brief;
- shared output schemas.

It must not read sibling idea directories. This reduces anchoring and consensus collapse, but it is a **soft protocol**, not a security boundary.

## 2. Shared coding-agent workspace

Codex and Claude Code subagents normally work inside the same project workspace. A worker with filesystem or Bash access may technically inspect files outside its assigned research directory unless the runtime/OS permission model prevents it.

The orchestrator should therefore:

- provide narrow tasks and explicit allowed paths;
- avoid exposing sibling Idea Cards to first-round blind explorers;
- record which artifacts each Agent was permitted to read;
- keep shared-file writes owned by one coordinator;
- treat “independent” as an experimental-design claim that requires audit.

The generated role files improve behavioral separation but are not an OS sandbox.

## 3. Direct API packet isolation

`tools/provider_call.py` is stricter in a different way: only explicitly selected text files are packaged. Common secret files are blocked and unsupported/binary artifacts are skipped. The model receives no arbitrary local shell or file tool.

This makes it useful for bounded independent reviews, but it cannot replace a coding runtime when execution is required.

## 4. Experiment subprocess boundary

`tools/run_experiments.py` launches an experiment with `shell=False`, a canonical
working directory inside the current run, and a deliberately small environment.
It does not copy `os.environ`.

The inherited allowlist is exactly `PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`,
`SYSTEMDRIVE`, `COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `LANG`, `LANGUAGE`, `LC_ALL`,
`LC_CTYPE`, `TZ`, `PYTHONUTF8`, `PYTHONIOENCODING`, plus other `LC_*` locale
categories. Empty and relative `PATH` entries are discarded. Home/config locations
and arbitrary parent variables are not inherited, so API keys present in the
terminal/Codex process are not automatically available to an experiment. A library
that genuinely requires a home/config location must receive a non-sensitive,
preferably run-local value explicitly in manifest `env`.

Manifest `env` entries are explicit, but fail closed for:

- every `CUMCM_*` name, which is reserved for runner-generated run, experiment,
  attempt, output, record and seed values;
- executable/interpreter controls: `PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`,
  `SYSTEMDRIVE`, `COMSPEC`, `LD_PRELOAD`, `LD_LIBRARY_PATH`,
  `DYLD_LIBRARY_PATH`, `DYLD_INSERT_LIBRARIES`, `PYTHONPATH`, `PYTHONHOME`,
  `PYTHONSTARTUP`, `PYTHONINSPECT`, `NODE_OPTIONS`, `RUBYOPT`, `PERL5OPT`,
  `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`, `BASH_ENV` and `ENV`;
- names containing bounded secret markers such as `SECRET`, `TOKEN`, `PASSWORD`,
  `PASSWD`, `CREDENTIAL`, qualified API/access/private/signing/encryption/license
  `KEY`, `DATABASE_URL`, `CONNECTION_STRING` or `DSN`;
- common credential indirections including AWS shared credential/config files,
  Google Cloud credential override, Docker auth/config, Git/SSH askpass and agent,
  GitHub config, Kubernetes config, netrc and npm user config.

Names are checked case-insensitively, case-colliding entries are rejected, and
values containing NUL are rejected. Allowed values remain bound by the manifest
hash rather than copied into runner logs.

The executable is resolved once using the sanitized `PATH` (or an explicit path)
and the absolute executed path is stored in the attempt receipt. A relative
executable path must resolve inside the run. Absolute and `PATH`-resolved system
interpreters may be outside the project, which is recorded rather than hidden.

This boundary prevents accidental environment-secret propagation and common path
hijacks. It is **not** an OS sandbox: the child still has the invoking user's file
permissions, command arguments may name paths outside the run, absolute/system
executables are allowed, network access is not disabled, and descendant processes
are not placed in a restricted process container. A secret deliberately placed
under an innocuous variable name also cannot be recognized by name filtering.
Attempt receipts therefore explicitly record `filesystem_isolated: false` and
`network_isolated: false`.

## 5. Stronger isolation

For higher-stakes comparison, use one of these:

- separate Git worktrees or project copies;
- separate containers or virtual machines;
- manually opened fresh external model conversations supplied only with permitted files;
- direct API workers whose packets/input manifests are generated independently.

Merge outputs only after blind runs finish. Preserve hashes or packet manifests for each worker.

## 6. What the harness does not claim

The Harness does not provide cryptographic, operating-system, or process-level isolation by itself. Same-model rollouts still share weights/training biases, and even different providers can share common internet-derived priors.

Use isolation to increase context diversity, then use executable evidence and human review to decide whether ideas are actually different and valid.
