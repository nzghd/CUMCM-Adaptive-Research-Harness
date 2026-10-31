# Optional OpenAI API burst

API usage is separately billed and must be enabled in the active run's `BUDGET.json`. First run a dry preview with explicit budget reservation and billing acknowledgement:

```text
python tools/api_burst_openai.py --prompt-file <FILE> --reserve-usd <AMOUNT> --ack-separate-billing --dry-run
```

Only after team approval remove `--dry-run`. Use the burst for one bounded uncertainty, not generic parallel brainstorming. Reconcile the recorded reservation with the API dashboard and human-verify all adopted output.
