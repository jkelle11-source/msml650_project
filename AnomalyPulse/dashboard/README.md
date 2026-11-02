# AnomalyPulse status board (M7-1)

The SRE console from PROJECT_PLAN §14: service health, the active incident, Bedrock's diagnosis, and the
model's prediction beside the injected ground truth. It renders only from the one JSON object the read API
returns (`GET /dashboard`, Docs/DASHBOARD_CONTRACT.md, M7-2). `mock/` holds local copies of the same six
states.

Static HTML, CSS and JavaScript modules: no framework, no build step, nothing to install.

## Run it

```bash
cd AnomalyPulse/dashboard
npm start                      # = python3 -m http.server 4173 --bind 127.0.0.1
```

Open <http://127.0.0.1:4173> (the address the API allows). Opening `index.html` as a file doesn't work: the
page loads modules and JSON over HTTP. Pick a state from **Scenario**, or open `?scenario=<name>` directly.
For the demo, run it from the presenting laptop (agreed with Jake in #27).

## Test it

```bash
npm test                       # = node --test tests/*.test.js   (Node 20+)
```

The tests check the formatting, the prediction-vs-truth verdict, every mock against the contract's rules,
the console's reading of each scenario, and how failed requests are reported.

## Where it reads from

By default it reads M7-2's read API, `https://ffc8nyi6jl.execute-api.us-east-2.amazonaws.com/dashboard`
(also in SSM at `/anomalypulse/dashboard/url`), and the Scenario picker sends `?scenario=<name>`. That API
serves mock data until M8-5, with every time placed at the moment of the request, so its timestamps and ids
differ from the copies in `mock/`.

| When | In `js/config.js` |
|---|---|
| M7 (now): Jake's mock API | `source: "api"`, `scenarios: true` |
| M8-5: live data | `scenarios: false` (no picker, no `?scenario`) |

Without editing the file, add to the page's address:

- `?source=mock` to read the local copies in `mock/`. Needs no network, so it is the demo's fallback.
- `?api=<base URL>` to read another API.

## The six states

| Scenario | What it shows |
|---|---|
| `healthy` | No incident; every service healthy |
| `diagnosing` | An incident is classified but Bedrock hasn't answered: severity and diagnosis are pending |
| `incident` | Jake's example from the contract, verbatim: a diagnosed payment failure |
| `cascade_early` | A cascade's first minutes: the model says traffic spike, the run is a cascade, only the spike is active |
| `no_ground_truth` | An incident with nothing injected, as in production |
| `no_data` | The pipeline has stopped: every number is a dash and the console never says healthy |

The API serves these six. In `mock/`, `incident.json` is the contract's example; the other five follow the
contract's scenario descriptions.

## What the console promises

- **Nothing is invented.** Every value comes from the reply. A `null` number shows as a dash, never `0`.
- **Severity is Bedrock's.** Until the diagnosis arrives the incident shows "Severity pending"; the injected
  severity appears only on the ground-truth side.
- **Observed sequence, not cause.** Bedrock's text is shown as written (as text, never HTML), under a note
  that any ordering of symptoms is an observed sequence from correlated timestamps.
- **The verdict:** *Match* when the classes agree; *Matches the active fault* in a cascade's first minutes,
  when only one fault is active and the model names it (the Window Labeling Policy labels those windows the
  same way; retry amplification never counts as a fault); *Mismatch* otherwise; *No ground truth* when nothing
  was injected.
- **Failures are visible.** Polls every 15 s with an 8 s timeout. If a request fails, the last good reply
  stays on screen, greyed out, the banner turns grey and says "Data out of date", and a line under the top bar
  gives the reason.

## Design

A status board, read top to bottom, so it can be explained in that order:

1. **The banner** answers "is anything wrong?" in one line: red with the incident and where it is
   ("Dependency failure in Orders and Payments", then confidence, severity, start), green for "All systems
   normal", grey when there is no data or the data is out of date.
2. **Key figures** for the latest minute: requests, error rate, p95 latency, active incidents.
3. **Service health:** one light per service, green, amber or grey, with its own numbers.
4. **Incident details** (type, a confidence meter, severity, affected services, times) beside **AI diagnosis**
   (summary, root cause, evidence, recommended actions).
5. **Did the model get it right?** The model's prediction and what the simulator injected, side by side, with
   a tick or a cross between them, then the injected faults and which are active this minute.

With no incident, 4 and 5 are replaced by one line. Colour only ever means state (green, amber, red, grey),
plus indigo for what the simulator injected. Atkinson Hyperlegible Next (fonts/, SIL Open Font License) was
drawn for legibility, which suits a projector, and is bundled so the demo doesn't depend on Wi-Fi.
