# AnomalyPulse operations console (M7-1, M8-5)

The SRE console from PROJECT_PLAN §14: service health, the active incident, Bedrock's diagnosis, and the
model's prediction beside the injected ground truth. It renders only from the one JSON object the read API
returns (`GET /dashboard`, Docs/DASHBOARD_CONTRACT.md, M7-2), which serves live data since M8-5. `mock/` holds
snapshots of the API's replies for its six mock scenarios: the demo's offline fallback.

Static HTML, CSS and JavaScript modules: no framework, no build step, nothing to install. It lives apart from the
read API's code (`AnomalyPulse/dashboard/`), because that whole folder is packaged into the API's Lambda.

## Run it

```bash
cd AnomalyPulse/console
npm start                      # = python3 -m http.server 4173 --bind 127.0.0.1
```

Open <http://127.0.0.1:4173> or <http://localhost:4173>: the API's CORS allows only these two addresses, so a
page on any other port or host can't read it. Opening `index.html` as a file doesn't work: the page loads
modules and JSON over HTTP. For the demo, run it from the presenting laptop (agreed with Jake in #27); the
traffic for a live run still comes from the runner host, never the laptop (Docs/DETECTION.md).

## Test it

```bash
npm test                       # = node --test tests/*.test.js   (Node 20+)
```

The tests check the formatting, the prediction-vs-truth verdict, every mock against the contract's rules,
the console's reading of each scenario, and how failed requests are reported.

## Where it reads from

By default it reads the read API, `https://ffc8nyi6jl.execute-api.us-east-2.amazonaws.com/dashboard` (also in
SSM at `/anomalypulse/dashboard/url`), which serves live data (M8-5): the newest minute's health, the open incident
with its diagnosis, and the injected ground truth of the run that caused it. There is no Scenario picker.

| When | The API | In `js/config.js` |
|---|---|---|
| M8-5 (now): live data | deployed with `DashboardSource=live` (the default) | `scenarios: false` (no picker, no `?scenario`) |
| M7: the mock scenarios | deployed with `DashboardSource=mock` | `scenarios: true` |

Without editing the file, add to the page's address:

- `?source=mock` to read the snapshots in `mock/`. Needs no network, so it is the demo's fallback.
- `?api=<base URL>` to read another API. The source chip then shows that API's host.
- `?theme=light` for the light presentation theme (below).

The parameters combine, for example `?source=mock&theme=light&scenario=incident` (with `?source=mock`, the
Scenario picker is back).

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
it changes, capture them again from an API deployed with `DashboardSource=mock` (the live API ignores `?scenario=`):

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
- **Severity is the diagnosis's.** It is the severity rubric's, computed from the evidence Bedrock was given
  (`Docs/DIAGNOSIS_CONTRACT.md`), not Bedrock's own pick. Until the diagnosis arrives, the incident's AI severity
  is pending; the injected severity appears only on the ground-truth side.
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
- **Quiet polling.** A background poll changes nothing on screen until its reply arrives. Only a refresh the
  presenter asks for (Refresh, a new scenario, Resume) shows *Refreshing*, so nothing flickers every 15 s.

## Layout

Laid out for the demo's projector: on a 1280×720 screen the Overview (the incident's headline, the KPIs and all
four services) fits without scrolling, and no text is smaller than 13px. The header says the system's status once
(*System healthy*, *Incident active* or *No data*) beside the presenter's controls; the tab row carries the
minute the numbers describe and when the reply was made. Six sections, from top to bottom:

1. **System overview:** a headline naming the incident (*Dependency failure detected*) with its confidence,
   affected services and AI severity, then requests per minute, error rate, p95 latency and active incidents.
2. **Service health:** the four services with their status, a bar for the share of requests that succeeded
   (§14's bars: the bar's colour is the service's status from the API, so the console invents no threshold),
   error rate, requests and p95.
3. **Active incident:** the incident type, start and detection times, affected services, the model that
   answered, the incident id and model job, and a confidence bar.
4. **AI diagnosis:** Bedrock's summary, likely root cause, supporting evidence and prioritised actions.
5. **Prediction vs ground truth:** the verdict first, large, with an icon (✓ same classification, ≈ matches the
   active fault, ≠ different, — no ground truth) and the detection latency; then the prediction and what the
   simulator injected, side by side, with the injected faults and which are active this minute. This is the
   demo's ground-truth reveal (§15 part 3).
6. **Event timeline:** every timestamp in the reply in order (faults starting and ending, the incident
   starting, the model detecting it), with planned fault times marked. Useful for narrating a cascade (M10-5).

With no incident, 3 to 6 show an empty state. Times show as `05:41:04 UTC`: every time in a reply is from the
last few minutes.

## Colour

One colour per meaning, the same everywhere: **green** healthy (and a verdict that matches), **amber** a
degraded service or a medium severity, **red** the incident and its active faults, **grey** no data, idle or
nothing to compare, **cyan** links and things to look at. Two more colours say whose an item is: **teal** the
model's prediction, **violet** the injected faults. Every state also has words or an icon, never colour alone.

Every colour is a token at the top of `styles.css`, checked for contrast against its own theme's surfaces (text
at least 4.5:1). The card edges are 2:1 against the cards, so the layout holds on a projector, whose blacks are
grey.

**`?theme=light`** is a light presentation theme with the same tokens re-stepped for a white surface. Bright
rooms and projectors often show a light page better; try both on the room's projector before the demo and use
whichever reads better from the back.

## Pages

A tab bar under the header splits the console into five pages, one part of the story at a time:
**Overview** (1 and 2), **Incident** (3), **AI diagnosis** (4), **Ground truth** (5) and **Timeline** (6).
The address names the page (`#overview`, `#incident`, `#diagnosis`, `#verify`, `#timeline`), so a page can be
linked and the browser's back button works. Each tab has a dot: red for an incident, a cyan ring while the
diagnosis is pending, cyan when there is something to see, a grey ring when there is nothing (hover a tab for
the words). Previous / Next links at the bottom, or the ← and → keys, step through the pages in order, which is
handy when presenting.

Atkinson Hyperlegible Next (fonts/, SIL Open Font License) is
bundled so the demo doesn't depend on Wi-Fi.
