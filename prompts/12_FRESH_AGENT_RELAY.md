# Fresh-Agent Relay

Use `$cumcm-relay` for a long bounded implementation or audit. Start with:

```text
python tools/relay.py init --goal "..." --task "..." --max-rounds 6
```

Each round starts a fresh `relay_worker`, verifies referenced artifacts, completes one bounded task, and fills the adjacent `HANDOFF.json` with `completed=true`. Advance with `python tools/relay.py advance --session <SESSION>`. Insert an independent audit every 2–3 rounds or after any high-severity failure.
