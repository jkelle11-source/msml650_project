# M2-1 Handoff — Telemetry Schema Contract, Validator & S3 Landing Zone (in progress)

> **For the next Claude session (and Jake).** This is a *working-session* handoff, not a team handoff. It captures (1) the **way Jake wants to work**, (2) the M2-1 deliverables and their status, and (3) exactly where the in-progress files stand so you don't re-derive or re-litigate anything.

---

## 0. How to work with Jake on this — READ FIRST

**This is a Socratic, learning-by-doing session. Do not write to any project files. Jake writes all the code himself.**

The loop we agreed on, and that you must continue:

1. Take the ticket **one deliverable at a time** (see §2).
2. **Jake proposes** an approach or writes a draft.
3. **You give feedback** — point out errors, ask leading questions, explain *why* something will bite later, surface trade-offs. Lead him to the answer; don't hand him the finished answer.
4. Repeat until that deliverable is right, then move to the next.

Guardrails:
- **No `Write`/`Edit` on his deliverables.** Reading files, running read-only checks (`git status`, `sam build --help`, `make -n`, unit-test dry-runs), and validating *his* work are fine and encouraged.
- Keep feedback **tight and prioritized** — lead with the blocker, not a wall of every nit. (Jake got frustrated once when the feedback was too dense; rank issues, don't dump them.)
- When he asks "did I fix it?", **actually verify** against the files before answering.
- The one exception to the no-writes rule so far was *this handoff*, which he explicitly requested.

---

## 1. Context — the sources of truth

- **`Docs/PROJECT_PLAN.md`** — the whole AnomalyPulse plan. For M2-1 the load-bearing sections are **§3 (Telemetry, the Tier-1/Tier-2 Feature/Label Boundary, the millisecond-timestamp + `request_id` discipline)** and the Feature/Label Boundary rules.
- **`Milestones/Milestone_02_Observability.md`** — the milestone. **`M2-1` is Jake's issue** and it *blocks* M2-2..M2-5 (the four service owners' instrumentation). Read the `M2-1` issue block and its Definition of Done.
- **`Handoffs/M1-1_Handoff.md`** — background on the deployed stack, the tiered telemetry rule, and team conventions.

One-line project frame: services emit telemetry → ML detects/classifies incidents → Bedrock explains them. M2-1 makes the telemetry **structured, enforced, and landable in S3**.

---

## 2. M2-1 deliverables & status

| # | Deliverable | Status |
|---|---|---|
| 1 | Validating **record schema** (`telemetry_schema.json`) | ✅ **Done** |
| 1b | **Field catalog** with per-field tiers (all fields incl. Tier-2) | ✅ **Done** |
| — | **Lambda layer** packaging (delivery mechanism for the helper) | ✅ **Done** (build wiring verified via `make -n`; not yet `sam build`-proven) |
| 2 | Shared **validation helper** (`schema_validator.py`) | 🚧 **In progress — broken draft**, see §4 |
| 3 | **S3 raw landing zone** + partitioning (`raw/service=<svc>/dt=<date>/…`) | ⬜ Not started |
| 4 | **CloudWatch-logs → S3** delivery path | ⬜ Not started |
| 5 | One-page **telemetry-contract doc** | ⬜ Not started |

**Definition of Done (from the ticket) — remaining:**
- [ ] Schema validates a known-good record and rejects a record missing `timestamp`/`request_id` *(schema supports this; needs the validator + a test to prove it)*
- [x] Every Tier-2 field explicitly marked & documented as *never a feature* *(field catalog)*
- [ ] Records from ≥1 deployed service visible in S3 under documented partitions
- [ ] All four service owners have imported the shared validation helper

---

## 3. Decisions already made — do NOT relitigate

- **Standard JSON Schema (draft 2020-12) + a custom `tier` annotation.** Not a bespoke format.
- **Two separate artifacts, on purpose:**
  - **Record schema** validates *emitted telemetry lines* — correlation + Tier-1 fields only, `additionalProperties: false` so any stray **Tier-2 field is rejected** (this is the leak-catcher).
  - **Field catalog** is a *registry* (not a JSON Schema) listing **every** field with its tier; it's the only home for the Tier-2 field definitions, read by the M4 tier-guard and by M3/M4 authors.
- **`required` = all 14 observable fields.** Emission rule is "never omit, emit `null`"; `required` checks presence (a `null` still counts as present).
- **Timestamp** enforced with both `format: date-time` (advisory) and a `pattern` regex pinning millisecond precision (`^\d{4}-...T...:...:...\.\d{3}Z$`). The `pattern` does the real enforcement; `format` isn't enforced unless a format-checker is wired.
- **Validator contract:** a **pure** verdict-returner (no side effects; returns whether valid + the errors), plus a thin `validate_or_raise` wrapper. On the **live request path the handler logs and continues** (never crashes the user's request over bad telemetry — avoids the observer effect during incident experiments); **tests / CI / the M4 feature-guard call the raising variant.**
- **Sharing mechanism = a Lambda layer**, so all four services `import schema_validator` from one place. This satisfies the DoD "all four owners imported the shared helper" without four copies.
- **Single source of truth for the schema:** it now *lives inside the layer* (`layers/telemetry/python/telemetry_schema.json`), so there's exactly one copy. The field catalog stayed in `infra/shared/` (it's a build-time artifact; it does not need to ship to the Lambda runtime).

---

## 4. Current file locations & the broken validator

**Locations (as of this handoff):**
```
AnomalyPulse/
├─ infra/shared/
│  ├─ template.yaml          ← has the TelemetryLayer resource + Globals attach
│  ├─ field_catalog.json     ← DONE (all fields, tier-tagged)
│  └─ samconfig.toml
└─ layers/telemetry/
   ├─ Makefile               ← BuildMethod: makefile; copies python/ + pip-installs deps with Linux-arm64 wheels
   ├─ requirements.txt       ← jsonschema
   └─ python/
      ├─ telemetry_schema.json   ← DONE (record schema; canonical copy)
      └─ schema_validator.py     ← 🚧 BROKEN DRAFT
```

**The record schema and field catalog are finished and correct** (verified this session: types, `enum`+`null` on `dependency`, numeric `minimum`/`maximum`, boolean `additionalProperties: false`, `required` = all 14; catalog has `tier` on all 5 Tier-2 fields incl. `db_throttled`, and `severity`'s `enum` restored).

**Layer wiring is correct** (`BuildMethod: makefile`, real tabs, target `build-TelemetryLayer`, `--platform manylinux2014_aarch64 --python-version 3.13 --only-binary=:all:` so `rpds-py`/`jsonschema` get **Linux** wheels, not macOS — Jake is on Apple Silicon). Not yet proven with an actual `sam build`.

**`schema_validator.py` is the next thing to work through.** Its current draft has known defects (guide Jake to these Socratically — don't just paste the fix):
- Won't import: uses `String` (not a type — it's `str`), never imports `json`, and references `schema`/`Callable` that are undefined/unimported. (Annotations here are probably best dropped.)
- Schema loading is wrong two ways: the path is CWD-relative **and stale** (points at the old `infra/shared/...`; the file now sits next to the module, so use `Path(__file__).parent / "telemetry_schema.json"`), and `json.loads(path)` parses the *filename* rather than the file — it must read the file's contents first (`json.loads(path.read_text())` / `json.load(...)`).
- `validate_or_raise` **doesn't raise** — it returns `iter_errors(...)`. Decide what it raises, and drop the leftover `strict=` param (a function named "or_raise" has no non-raising mode).
- `validate` returns a bare `bool`, discarding the errors the handler needs to log. Reconsider returning the errors (empty == valid) per the agreed contract.
- Not yet added: `Draft202012Validator.check_schema(...)` at import (fails a *malformed schema* loudly at cold start) — cheap, and it's a DoD proof point.

**The three proofs to land once the module is right** (this is where item 2 stops being theory):
1. `check_schema(schema)` passes.
2. A real Product/Order record → valid.
3. A record with a stray `error_type`, and one missing `timestamp` → rejected, with a sensible error.

---

## 5. Git state

- Branch **`m2-1_jake`** (pushed to `origin/m2-1_jake`), based cleanly on **`main`** (`9f9f82e`).
- Convention was fixed this session: the old `m1-review` work was fast-forwarded into `main`, `m1-review` deleted (local + remote), and `m2-1_jake` re-rooted on `main`.
- One commit on the branch: *"Defining telemetry schema and making schema validator usable by Lambdas."* Working tree currently has uncommitted edits to `schema_validator.py`.
- **Convention:** branch-per-ticket → PR to `main`. Jake owns `infra/` and deploys the single shared stack; he never has teammates run `sam deploy`.

---

## 6. Suggested next steps (in order)

1. Finish **`schema_validator.py`** (§4) and land the three proofs — a small `test_schema_validator.py` is the natural home.
2. **Prove the layer** builds: `sam build` (add `--use-container` if not using the pip `--platform` flags), then confirm `.aws-sam/build/TelemetryLayer/python/` contains `schema_validator.py`, `telemetry_schema.json`, **and** the installed `jsonschema`/`rpds` packages.
3. **S3 raw landing zone** (deliverable 3): bucket + partition scheme `raw/service=<svc>/dt=<date>/…` in `template.yaml`.
4. **CloudWatch-logs → S3** path (deliverable 4).
5. **One-page contract doc** (deliverable 5) — and record there that the canonical schema now lives in the layer, so teammates aren't relying on word-of-mouth.

*Remember: guide, don't do. Jake writes it; you make him get it right.*
