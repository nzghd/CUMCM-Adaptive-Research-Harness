
#!/usr/bin/env python3
from __future__ import annotations
import importlib.util
import os
import sys

print("Python:", sys.version.split()[0])
spec = importlib.util.find_spec("llm_verifier")
print("llm_verifier installed:", bool(spec))
if spec:
    import llm_verifier
    print("llm_verifier version:", getattr(llm_verifier, "__version__", "unknown"))

for key in ("DEEPSEEK_API_KEY", "VERTEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
    value = os.getenv(key)
    print(f"{key}:", "set" if value else "not set")

print("\nFaithful mode requires token-level logprobs. A successful HTTP response alone is not enough.")
print("After a real run, reject flat scores, especially all values near 0.5.")
