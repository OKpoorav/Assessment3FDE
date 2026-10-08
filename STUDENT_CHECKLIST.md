# Student submission checklist

Before submitting, confirm that:

- [ ] `python verify_setup.py` passes in your project environment.
- [ ] The product can process a request end-to-end.
- [ ] Architecture A is a working single-agent baseline.
- [ ] Architecture B is a lightweight staged / 2-agent variant.
- [ ] At least 3 tools are used; at least 1 tool/check is deterministic.
- [ ] Recommendations are returned in the `ProcurementDecision` structure.
- [ ] Important evidence is visible to the user.
- [ ] Missing/conflicting/unavailable evidence is handled without fabrication.
- [ ] Human approval is preserved for sensitive decisions.
- [ ] Prompt injection inside business data does not override system behavior.
- [ ] Date-based checks use the policy's data snapshot / reference date.
- [ ] The same evaluation cases were run on both architectures.
- [ ] Latency and LLM/tool-call counts are reported.
- [ ] The decision memo is <= 500 words and supported by evaluation evidence.
- [ ] Setup instructions work from a clean environment.
- [ ] Any LLM/provider SDK you added is present in `requirements.txt`.
- [ ] `.env`, API keys, and other secrets are not committed.
