# Saved diagnoses

One folder per model and one file per sample, as `python -m genai.run_samples` writes them.

- **`global.anthropic.claude-sonnet-4-6/`**: the selected model, run 2026-11-02 on the final prompt (the M6-1
  review's: the cascade rule, numbers from a bullet's own metrics, what `affected_services` means). All five
  pass the self-check.
- **`global.anthropic.claude-opus-4-6-v1/`**: run 2026-11-01 on the **first** prompt, before it was tightened
  against these very findings. It is the record of the model comparison in `Docs/DIAGNOSIS_CONTRACT.md`
  ("Why this model"), **not** a like-for-like comparison with the Sonnet folder: the prompt has changed since.
  Sonnet's answers to the first prompt were not kept.

Every file's `self_check` was recomputed on 2026-11-02 with the final `genai/self_check.py`, so the findings in
both folders come from the same checker. The text Bedrock wrote is unchanged.
