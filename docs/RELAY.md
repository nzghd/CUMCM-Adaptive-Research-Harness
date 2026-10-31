# Fresh-Agent Relay

Each relay round starts with a new context. Persistent state is limited to:

- approved project files;
- the current bounded task;
- the latest structured handoff;
- explicit evidence and open issues.

A handoff must contain:

- completed items and exact artifact paths;
- commands run and results;
- assumptions introduced;
- known failures and uncertainty;
- next task with a stopping condition;
- files the next Agent must not modify.

Every 2–3 rounds, insert an independent auditor that does not trust the previous summary. Otherwise a false assumption can propagate cleanly through every new context.
