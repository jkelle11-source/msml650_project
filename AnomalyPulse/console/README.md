# AnomalyPulse operations console (M7-1)

The SRE console from PROJECT_PLAN §14: service health, the active incident, Bedrock's diagnosis, and the
model's prediction beside the injected ground truth. It renders only from the one JSON object the read API
returns (`GET /dashboard`, Docs/DASHBOARD_CONTRACT.md, M7-2). `mock/` holds snapshots of the API's replies
for its six scenarios.

Static HTML, CSS and JavaScript modules: no framework, no build step, nothing to install. It lives apart from the
read API's code (`AnomalyPulse/dashboard/`), because that whole folder is packaged into the API's Lambda.

## Run it

```bash
cd AnomalyPulse/console
npm start                      # = python3 -m http.server 4173 --bind 127.0.0.1
```

Open <http://127.0.0.1:4173> (the address the API allows). Opening `index.html` as a file doesn't work: the
page loads modules and JSON over HTTP. Pick a state from **Demo Scenario**, or open `?scenario=<name>` directly.
For the demo, run it from the presenting laptop (agreed with Jake in #27).

## Test it

```bash
npm test                       # = node --test tests/*.test.js   (Node 20+)
```

The tests check the formatting, the prediction-vs-truth verdict, every mock against the contract's rules,
the console's reading of each scenario, and how failed requests are reported.

## Where it reads from

By default it reads M7-2's read API, `https://ffc8nyi6jl.execute-api.us-east-2.amazonaws.com/dashboard`
(also in SSM at `/anomalypulse/dashboard/url`), and the Demo Scenario picker sends `?scenario=<name>`. That API
serves mock data until M8-5, with every time placed at the moment of the request, so its timestamps differ from
the snapshots in `mock/`.

| When | In `js/config.js` |
|---|---|
| M7 (now): Jake's mock API | `source: "api"`, `scenarios: true` |
| M8-5: live data | `scenarios: false` (no picker, no `?scenario`) |

Without editing the file, add to the page's address:

- `?source=mock` to read the snapshots in `mock/`. Needs no network, so it is the demo's fallback.
- `?api=<base URL>` to read another API. The source chip then shows that API's host.

## The six states

| Scenario | What it shows |
|---|---|
| `healthy` | No incident; every service healthy |
| `diagnosing` | An incident is classified but Bedrock hasn't answered: severity and diagnosis are pending |
| `incident` | The contract's example: a diagnosed payment failure |
| `cascade_early` | A cascade's first minutes: the model says traffic spike, the run is a cascade, only the spike is active, and the payment failure is planned two minutes later |
| `no_ground_truth` | An incident with nothing injected, as in production |
| `no_data` | The pipeline has stopped: every number is a dash and the console never says healthy |

The files in `mock/` are the API's replies, captured on 2026-11-02 (UTC), so the offline fallback tells the same
story as the API. `mock_source.py` in `AnomalyPulse/dashboard/` is the one place the scenarios are defined; after
it changes, capture them again:

```bash
cd AnomalyPulse/console/mock
for s in healthy diagnosing incident cascade_early no_ground_truth no_data; do
  curl -sf "https://ffc8nyi6jl.execute-api.us-east-2.amazonaws.com/dashboard?scenario=$s" \
    | python3 -m json.tool --indent 2 > $s.json
done
```

The tests check some of their values (the times, ids and numbers), so update those after a capture.

## What the console promises

- **Nothing is invented.** Every value comes from the reply. A `null` number shows as a dash, never `0`.
- **Severity is Bedrock's.** Until the diagnosis arrives, the incident's AI severity is pending; the injected
  severity appears only on the ground-truth side.
- **Observed sequence, not cause.** Bedrock's text is shown as written (as text, never HTML), under a note
  that symptom ordering is an observed sequence from correlated timestamps, not proof of causation.
- **The comparison:** *Same classification* when the classes agree; *Matches active fault* when only one of a
  cascade's faults is active and the model names it, as in a cascade's first minutes (retry amplification never
  counts as a fault); a difference otherwise; nothing to compare when nothing was injected.
- **Planned isn't past.** While a run is going, its fault times are the planned ones. The timeline marks every
  time after the reply's `generated_at` as *planned*, so it never reports a fault before it starts.
- **Failures are visible.** Polls every 15 s while the tab is visible, with an 8 s timeout. If a request
  fails, the last good reply stays on screen, the connection chip says it is stale, and a notice gives the
  reason (including the API's own message for a 400 or 503). Until the first reply arrives, the console claims
  no state: the status reads *No reply* and the incident page *No reply yet*, never *No active incident*.

## Layout

A dark operations console, in six sections from top to bottom:

1. **System overview:** a headline naming the incident (*Dependency failure detected*) with its confidence,
   affected services and AI severity, then system health, requests per minute, error rate, p95 latency,
   active incidents.
2. **Service health:** the four services with their status, requests, error rate and p95; services the
   model names as affected are marked.
3. **Incident workspace:** the incident type, its id, start and detection times, affected services, the
   model that answered, and a confidence bar.
4. **AI diagnosis:** Bedrock's summary, likely root cause, supporting evidence and prioritised actions.
5. **Prediction vs ground truth:** the model's prediction and what the simulator injected, side by side,
   the injected faults and which are active this minute, and the comparison with detection latency.
6. **Event timeline:** every timestamp in the reply in order (faults starting and ending, the incident
   starting, the model detecting it), with planned fault times marked. Useful for narrating a cascade (M10-5).

With no incident, 3 to 6 show an empty state.

## Pages

A tab bar under the header splits the console into five pages, one part of the story at a time:
**Overview** (1 and 2), **Incident** (3), **AI diagnosis** (4), **Ground truth** (5) and **Timeline** (6).
The address names the page (`#overview`, `#incident`, `#diagnosis`, `#verify`, `#timeline`), so a page can be
linked and the browser's back button works. Each tab has a dot: red for an incident, amber while the diagnosis
is pending, cyan when there is something to see, a hollow ring when there is nothing. Previous / Next links at
the bottom, or the ← and → keys, step through the pages in order, which is handy when presenting.

Atkinson Hyperlegible Next (fonts/, SIL Open Font License) is
bundled so the demo doesn't depend on Wi-Fi.
