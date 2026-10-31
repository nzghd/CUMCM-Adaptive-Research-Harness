# Subscription/manual workflow without direct API orchestration

A no-direct-API setup can still use:

1. Codex project subagents **or** Claude Code project subagents;
2. manually opened independent external model conversations when allowed;
3. local Python experiments;
4. Lite verifier packets;
5. human review gates.

The limitation is automation: a local Python program should not treat a consumer chat subscription UI as an API. External dialogues are exported/imported manually.

Recommended pattern:

```text
Problem Graph in the active coding runtime
  ├─ blind project subagents
  ├─ fresh manual dialogue: independent route
  └─ fresh manual dialogue: adversarial critic
          ↓ manual import
     common evidence engine
```

External results are candidates, not authorities. They enter the same Claim–Evidence and audit process as native runtime outputs.
