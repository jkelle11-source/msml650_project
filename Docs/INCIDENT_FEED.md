# The incident feed (M8-4)

**Status:** built and checked offline (2026-11-08), and live on one traffic spike (2026-11-09). The hold takes
effect on the console when M8-5's live source builds the incident with `to_incident` ("For M8-5", below); today's
dashboard Lambda serves only the mock scenarios. **Owner:**
Josh · **Code:** `AnomalyPulse/incidents/record.py` (`to_incident`), `AnomalyPulse/incidents/feed.py` (the check) ·
**Reads:** [`INCIDENT_STORE.md`](INCIDENT_STORE.md) (M8-3's record) · **Read by:** M8-5 (the live source),
M9-5

How a diagnosis stored by M8-3 reaches the dashboard contract
([`DASHBOARD_CONTRACT.md`](DASHBOARD_CONTRACT.md)), and how the hand-off is checked.

## The mapping

M8-3 stores Bedrock's `diagnosis` already in the dashboard's shape, so nothing is renamed or rewritten.
`incidents.record.to_incident(record)` is the whole mapping, and the live source passes its result to
`build_dashboard`:

| Dashboard field | From the stored record |
|---|---|
| `incident.incident_id` | `incident_id` |
| `incident.prediction` | `prediction`, as stored |
| `incident.diagnosis.severity` | `diagnosis.severity` (the rubric's, #99) |
| `incident.diagnosis.summary` | `diagnosis.summary` |
| `incident.diagnosis.root_cause` | `diagnosis.root_cause` |
| `incident.diagnosis.evidence` | `diagnosis.evidence`, the bullets in order |
| `incident.diagnosis.recommended_actions` | `diagnosis.recommended_actions`, in the order to try them |
| `incident.diagnosis` = `null` | No diagnosis yet, the last call failed, or the diagnosis is **held back** (below) |

**Not shown:** `evidence_metrics`, `self_check`, `runbook`, `diagnosis_for_type` and `diagnosis_input` stay in
the record. The console shows each bullet's text, which already names its metric. Traceability is held when
Bedrock answers (the schema lets a bullet cite only the input's metrics) and checked again here, so the
dashboard contract stays at version 1 and the console needs no change. M9-5 reads them from the record.

## What is held back

A stored diagnosis whose self-check found **causal or ordering wording** (`kind` `wording`) or **a citation
outside the input** (`citation`) is not shown: `to_incident` gives `diagnosis: null`, and the console shows
"Severity pending" (`record.HOLD_KINDS`, `record.held_back`). Showing it would render an observed sequence as
causation, or a bullet that traces to no input metric. A citation finding can't come from Bedrock today: its
output schema lets a bullet cite only the input's metrics. It is held anyway, in case that ever changes.

- **Usually for about a minute.** M8-3 asks Bedrock again on the next counted minute, with the findings to fix
  (#102). A clean answer replaces the held one, and the feed shows it.
- **If the retry is flagged too** (one retry per type), the console stays on "Severity pending" until a call for
  a new type gives a clean answer, or the incident closes. On real Bedrock, three answers with 13 findings all
  came back clean from one revision (`DIAGNOSIS_CONTRACT.md`, "Decided in M8-3").
- **Every other finding is shown:** a number rounded too far, a claim with no metric of its kind, a severity
  disagreement. M9-5 scores them.
- The console's pending text says the diagnosis "hasn't arrived yet", which is not quite true for a held one.
  No change was asked for: the engineer sees no diagnosis either way.

## The check (Definition of Done)

`incidents.feed.check(record, shown)` takes a stored record and the dashboard's `incident` object for it
(after the read API's JSON round trip) and returns problems and notes:

1. **All sections intact:** the five sections exactly as stored (text, bullet order, action order), and
   nothing else in the diagnosis; the prediction unchanged; a held-back diagnosis never shown.
2. **No ordering claim rendered as causation:** the shown text, checked again by `genai.self_check` against
   the input it was made from (`diagnosis_input`), has no `wording` finding.
3. **Every evidence bullet traces to an input metric:** one `evidence_metrics` entry per bullet, none empty,
   each a metric of `diagnosis_input`, and no `citation` finding.

A record stored before #102 kept no input (no `diagnosis_input`, no `retried_for`; M8-3's two live runs). For
it, items 2 and 3 are checked only as far as they can be without the input (one citation entry per bullet, none
empty), and the rest is a note, not a failure, as `live_check.py` allows those records.

**Offline** (`python3 -m pytest incidents/test_feed.py`, 42 tests): all 15 saved Sonnet diagnoses
(`genai/samples/results/`, runbooks off, on, and the review's) stored with their inputs, through DynamoDB's
Decimal round trip, `to_incident`, `build_dashboard` and JSON. 14 reach the feed intact. One, the review run's
Lambda degradation (an ordering claim with no observed sequence), is held back. Each way the hand-off could
break fails a test: a section shortened, reordered, dropped or added; a changed prediction; causal or ordering
text the stored check missed; a bullet citing a metric outside the input, or nothing; citations out of step
with the bullets; a record without its input; a held diagnosis that reaches the dashboard. The same 15 replies
also passed the console's own `checkReply` and `viewModel` (`console/js/model.js`): every section was shown as
stored, and the held one as pending.

**Live**, read-only, on the newest stored incidents:

```bash
cd AnomalyPulse
python3 -m incidents.feed --profile <profile>            # the 10 newest; --limit N for more or fewer
```

The records of M8-3's two live runs (the traffic spike and the cascade, 2026-11-08) were stored before #102, so
they have no `diagnosis_input` and fail item 3's check by design.

## The live run (2026-11-09)

Run by Josh from the runner with `drive.sh`: `exp-20261109T025523Z-bbf1`, a traffic spike in the ×6-8 band
(intensity 0.7), 300 s of fault after `--baseline 360`, 02:55:23 to 03:11:23 UTC. `anomalypulse-explain` and
`anomalypulse-incidents` had been redeployed with #102 (00:09 and 00:10 UTC).

- **The feed check passed** on its record, `inc-20261109T030100Z` (`TRAFFIC_SPIKE`, 1.000, closed): all five
  sections reached the dashboard as stored, severity `LOW`. The shown text, checked again against the stored
  input, had no finding at all. Each of the 6 bullets cites 1 to 3 of the input's 10 metrics.
- **The first live retry.** The first answer was flagged for a claim with no metric of its kind (`metric`, not a
  hold-back kind, so the console showed it). On the next counted minute the store sent it back with its findings,
  and the clean answer replaced it (`incident_retry`: `"flagged": ["metric"], "kept": true`). 2 Bedrock calls in
  all.
- **`live_check.py` passed:** one incident of the run's type, opened on 03:01 and closed with `ended_at` 03:07, the
  `experiment_id` beside the prediction, 16 steps, 0 errors, the failure queue empty. Round trip: 172 s from the
  minute's end to its diagnosis, then 17 to 31 s to the stored diagnosis.
- **Noted for M9-5, not a feed problem:** the first action says to check "provisioned or auto-scaling capacity
  limits" on the Cart and Orders tables, which are on-demand. That's the same slip the M6-2 blind comparison
  scored, without runbooks (`genai/samples/results/blind/`).

## For M8-5

The live source should call `incidents.record.from_dynamodb`, then `to_incident`, on the newest `OPEN` record.
It shouldn't read `diagnosis` itself, or a held-back diagnosis reaches the console. `record.py` imports
`incidents.builder` and `genai.self_check`, so the dashboard Lambda is built by `dashboard/Makefile`
(2026-11-09), which bundles `incidents/record.py` and `builder.py` with `genai/contract.py`, `affected.py` and
`self_check.py`; `sam build` picks it up, and a redeploy of `anomalypulse-dashboard` changes nothing it serves.
`incidents/test_bundle.py` checks that each Lambda's Makefile copies every module its code imports; a new module
the live source runs is one more entry in its `BUILDS` row.
