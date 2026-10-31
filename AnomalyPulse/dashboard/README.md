# AnomalyPulse console (M7-1)

The SRE console from PROJECT_PLAN §14: service health, the active incident, Bedrock's diagnosis, and the
model's prediction beside the injected ground truth. It renders only from the one JSON object the read API
returns (`GET /dashboard`, Docs/DASHBOARD_CONTRACT.md, M7-2), and until the API is deployed it reads the same
shape from `mock/`.

Static HTML, CSS and JavaScript modules: no framework, no build step, nothing to install.

## Run it

```bash
cd AnomalyPulse/dashboard
npm start                      # = python3 -m http.server 4173 --bind 127.0.0.1
```

Open <http://127.0.0.1:4173>. Opening `index.html` as a file doesn't work: the page loads modules and JSON over HTTP.
Pick a state from **Scenario**, or open `?scenario=<name>` directly.

## Test it

```bash
npm test                       # = node --test tests/*.test.js   (Node 20+)
```

The tests check the formatting, the prediction-vs-truth verdict, every mock against the contract's rules,
the console's reading of each scenario, and how failed requests are reported.

## Point it at the read API

In `js/config.js`:

| When | Set |
|---|---|
| Now: no API yet | `source: "mock"` (reads `mock/<scenario>.json`) |
| Jake's mock API is up (M7) | `source: "api"`, `apiBaseUrl: "<base URL>"` (sends `?scenario=<name>`) |
| Live data (M8-5) | also `scenarios: false` (no picker, no `?scenario`) |

To try an API without editing the file, add `?api=<base URL>` to the page's address.

## The six states

| Scenario | What it shows |
|---|---|
| `healthy` | No incident; every service healthy |
| `diagnosing` | An incident is classified but Bedrock hasn't answered: severity and diagnosis are pending |
| `incident` | Jake's example from the contract, verbatim: a diagnosed payment failure |
| `cascade_early` | A cascade's first minutes: the model says traffic spike, the run is a cascade, only the spike is active |
| `no_ground_truth` | An incident with nothing injected, as in production |
| `no_data` | The pipeline has stopped: every number is a dash and the console never says healthy |

`incident.json` is the contract's example. The other five follow the contract's scenario descriptions;
compare them with the API's own once it is deployed.

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
  stays on screen, greyed out, the state reads "Out of date", and a line under the masthead gives the reason.

## Design

It reads as a live incident report, not a wall of tiles: a dateline, one headline sentence built from the
reply ("A dependency failure is affecting Orders and Payments."), a short deck, then three sections set off
by dot leaders: the system (a line of key figures and the services), what Bedrock says, and the prediction
beside the ground truth with the verdict written as a sentence. With no incident, the last two sections
are left out. White paper and dark ink; colour only where state lives (red, amber, green on the state
word) and indigo for what the simulator injected. Atkinson Hyperlegible Next (fonts/, SIL Open Font
License) was drawn for legibility, which suits a screen read from the back of a room, and is bundled so the
demo doesn't depend on Wi-Fi.
