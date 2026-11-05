# Saved diagnoses

One folder per model and one file per sample, as `python -m genai.run_samples` writes them.

- **`global.anthropic.claude-sonnet-4-6/`**: the selected model on the final prompt (the M6-1 review's: the
  cascade rule, numbers from a bullet's own metrics, what `affected_services` means), runbooks off. First run
  2026-11-02, when all five passed the self-check; rerun 2026-11-03 beside the runbook run, so the two differ
  only in the runbook. Four of five pass; the Lambda-degradation summary says "followed at roughly the same
  scale", an ordering word.
- **`global.anthropic.claude-sonnet-4-6+runbooks/`** (M6-2): the same model and prompt with
  `--runbooks on`, run 2026-11-03. Four of five pass; the Lambda-degradation summary rounds 15.6x to "roughly
  15x". Neither finding comes from a runbook.
- **`blind/`** (M6-2): the blind actionability comparison of the two folders above, with the answer key and
  the result (a tie, 19/20 each).
- **`global.anthropic.claude-opus-4-6-v1/`**: run 2026-11-01 on the **first** prompt, before it was tightened
  against these very findings. It is the record of the model comparison in `Docs/DIAGNOSIS_CONTRACT.md`
  ("Why this model"), **not** a like-for-like comparison with the Sonnet folder: the prompt has changed since.
  Sonnet's answers to the first prompt were not kept.

Every file's `self_check` was recomputed on 2026-11-03 with the final `genai/self_check.py` (M6-2 added the
per-second form of a per-minute value and skips ordinal percentiles), so the findings in every folder come from
the same checker. The text Bedrock wrote is unchanged.
