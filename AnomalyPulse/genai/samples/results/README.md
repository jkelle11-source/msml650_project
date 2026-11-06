# Saved diagnoses

One folder per model and one file per sample, as `python -m genai.run_samples` writes them.

- **`global.anthropic.claude-sonnet-4-6/`**: the selected model on the final prompt (the M6-1 review's: the
  cascade rule, numbers from a bullet's own metrics, what `affected_services` means), runbooks off. First run
  2026-11-02, when all five passed the self-check (as merged in `45b9d24`); rerun 2026-11-03 beside the runbook
  run, so the two differ only in the runbook. All five pass. Until the 2026-11-06 self-check, the
  Lambda-degradation summary's "followed at roughly the same scale" was flagged as an ordering word.
- **`global.anthropic.claude-sonnet-4-6+runbooks/`** (M6-2): the same model and prompt with
  `--runbooks on`, run 2026-11-03 with the first runbooks and the first runbook intro. Four of five pass: the
  database-throttling root cause says "the provisioned table", a runbook fact (flagged since the M6-2 review
  added that check). The Lambda-degradation summary's "roughly 15x" for 15.6x was flagged until 2026-11-06.
  These are the lists the blind comparison scored; a new `--runbooks on` run writes to this folder, so move
  these first.
- **`global.anthropic.claude-sonnet-4-6+runbooks-review/`** (M6-2 review, 2026-11-05): the runbooks and intro
  as merged, written by the review's own script in this format rather than by `run_samples`. Four samples are
  from one run of all five; Lambda degradation is from a rerun after its rollback step was reworded (the first
  of two reruns, kept whichever way it came out). Four of five pass; the Lambda-degradation summary says
  "End-to-end latency has followed", which reads as "rose afterwards", and that input has no observed
  sequence. Its second rerun ("roughly 15x" for 15.6x) passes with the 2026-11-06 self-check.
- **`blind/`** (M6-2): the blind actionability comparison of the first two folders above, with the answer key
  and the result (a tie, 19/20 each).
- **`global.anthropic.claude-opus-4-6-v1/`**: run 2026-11-01 on the **first** prompt, before it was tightened
  against these very findings. It is the record of the model comparison in `Docs/DIAGNOSIS_CONTRACT.md`
  ("Why this model"), **not** a like-for-like comparison with the Sonnet folder: the prompt has changed since.
  Sonnet's answers to the first prompt were not kept.

Every file's `self_check` was recomputed on 2026-11-06 with the final `genai/self_check.py`, so the findings in
every folder come from the same checker. M6-2 added the per-second form of a per-minute value, skips ordinal
percentiles, and flags a claim about how a table's capacity is set; that last check added one finding each to
the first runbook run's and Opus's database-throttling files. On 2026-11-06 a hedged number ("roughly 15x",
"more than 15x") may be cut off instead of rounded, and "followed at roughly the same scale" orders nothing;
that cleared one finding each from the two Sonnet folders' Lambda-degradation files and added none. The text
Bedrock wrote is unchanged.
