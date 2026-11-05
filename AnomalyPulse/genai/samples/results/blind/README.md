# Blind actionability comparison (M6-2)

The five samples' `recommended_actions` from `global.anthropic.claude-sonnet-4-6/` (runbooks off) and
`global.anthropic.claude-sonnet-4-6+runbooks/` (on), both run 2026-11-03, set side by side as lists A and B in a
shuffled order (seed 20261103). Each list was scored 0 or 1 on four criteria: correct, specific, ordered, safe.
The sheet as scored, with the criteria and the stack facts the scorer was given, is
[`sheet_scored.md`](sheet_scored.md). Scored 2026-11-04.

## Answer key

| Incident | A | B |
|---|---|---|
| 1: lambda_degradation | with runbook | without runbook |
| 2: dependency_failure | with runbook | without runbook |
| 3: traffic_spike | with runbook | without runbook |
| 4: database_throttling | without runbook | with runbook |
| 5: cascading_failure | with runbook | without runbook |

## Result

| | Correct | Specific | Ordered | Safe | Total |
|---|---|---|---|---|---|
| With runbook | 5/5 | 5/5 | 5/5 | 4/5 | **19/20** |
| Without runbook | 4/5 | 5/5 | 5/5 | 5/5 | **19/20** |

**A tie.** Each side lost one point:

- **Without runbook, traffic spike, Correct 0.** The list watches the Cart and Orders tables "for signs of
  approaching provisioned limits"; both tables are on-demand. The runbook states this, and the with-runbook
  list watches only the Products table.
- **With runbook, Lambda degradation, Safe 0.** No reason was given. The likely reading is step 2, "roll back if a
  change is identified". By the sheet's wording ("as a first step") that list's first step is reading logs, so
  this is a stricter reading than the rubric; the score is recorded as given.

## What it shows, and what it doesn't

- **No measurable gain on this rubric.** Eighteen of the twenty scores match, and both sides score
  full marks on Specific and Ordered: four 0/1 criteria are too coarse to separate two lists that are both
  reasonable. M9-5's comparison needs finer scores (for example 0–2, or a pick of the better list per incident).
- **The one error the runbooks were written to prevent (capacity on an on-demand table) appeared only without
  them.** One case, so a hint, not a result.
- **The scorer was not fully blind.** They scoped M6-2 and know the stack well, and the with-runbook lists name
  stack facts (the 6 s read timeout, the Products table, reserved concurrency) more often, which can give them
  away. No one outside the GenAI work was available in time.
- **Each run also had one self-check finding**, both in the Lambda-degradation summary and neither from a
  runbook (off: "followed at roughly the same scale", an ordering word; on: "roughly 15x" for 15.6x). They were
  recorded rather than rerun.

The switch stays off by default; M9-5 decides whether it is on for the demo.
