# M1-1 Handoff — Shared Infrastructure & API Gateway

> **The one-sentence version:** There is one live API Gateway, and every route is already wired to a Lambda that Jake deploys — your job is to write the *code inside your service's Lambda* (`AnomalyPulse/services/<service>/handler.py`), test it, and open a PR. You never deploy infrastructure or touch a CloudFormation template; Jake owns the single shared stack and redeploys it when your code merges. Everything is **live** in `us-east-2` (stack `anomalypulse-shared`).

---

## 1. Mental model (read this if you're new to AWS)

A few ideas make everything below click. Skip if you already know them.

**The shared API is a building with one street address.** That address is the base URL. Inside are rooms, and each room is a *route* — `/products`, `/cart`, `/orders`, `/payments`. **Jake has already built the whole building *and* wired every door** to a specific Lambda function. Right now each Lambda is a stub that just answers "200 — I'm wired, but I don't do anything yet." Your job is to replace the stub behind *your* door with real logic. You furnish the room; you don't build the building or hang the doors.

**A "stack" is one deployment.** In AWS, a *CloudFormation stack* is a bundle of resources created/updated/deleted together. There is exactly **one** stack in M1 — `anomalypulse-shared` — and Jake owns it. It contains the API, all the routes, the DynamoDB tables, the IAM roles, **and** all four of your service Lambdas. You do not have your own stack. This is deliberate: for four tiny services, one coordinated stack is simpler and less fragile than five stacks wired together. What's split across the team is not the infrastructure — it's the **application code**.

**Your code lives in a folder, not a template.** Jake's template points each Lambda at a code folder: `AnomalyPulse/services/product/`, `.../cart/`, and so on. You edit the `handler.py` in your folder. When you're happy, you open a PR; Jake reviews, merges, and redeploys the stack so your code goes live.

**Parameter Store is the shared address book.** The API's URL and the table names are published to **AWS Systems Manager Parameter Store** under `/anomalypulse/...`. Test scripts and tools look them up by a fixed name instead of hardcoding values that change on redeploy.

---

## 2. Repo layout — where your code goes

```
AnomalyPulse/
├─ infra/
│  └─ shared/            ← Jake owns this. Don't edit.
│     ├─ template.yaml       the single stack: API + routes + tables + roles + all 4 Lambdas
│     ├─ samconfig.toml
│     └─ telemetry_schema.json
└─ services/            ← everyone's application code
   ├─ product/handler.py    ← Josh   (M1-2)
   ├─ cart/handler.py       ← Linu   (M1-3)
   ├─ order/handler.py      ← Melisa (M1-4)
   └─ payment/handler.py    ← Rachel (M1-5)
```

**The split that matters:** `infra/` is Jake's job (one stack). `services/` is your job (your handler code, plus any seed script and tests you add in your folder). You only ever edit files under `AnomalyPulse/services/<your-service>/`.

Two hard rules the template depends on — **do not change these**:
- Your file is named **`handler.py`** and exposes a function **`handler(event, context)`**. The template declares `Handler: handler.handler`; a different filename or function name means Lambda can't find your code (runtime 502).
- Each service folder is packaged **independently**, so a handler can only import files inside its *own* folder. There is no shared helper module across services — that's why the stubs each carry their own small response helper. (Cross-service shared code becomes a Lambda layer later; not in M1.)

---

## 3. Getting started: branch and edit your handler

We use **branch-per-ticket**: each person works on their own branch and merges to `main` via PR.

```bash
git checkout main
git pull                             # get the merged shared stack + this doc + contracts
git checkout -b <TICKET>-<NAME>      # e.g. m1-2-josh
```

Your Lambda is already deployed as a stub, so open your handler and start replacing it:

```bash
$EDITOR AnomalyPulse/services/<SERVICE>/handler.py    # e.g. services/product/handler.py
```

You do **not** create a folder, a template, or a stack — they already exist.

---

## 4. What's live right now, and how to see it

The whole API is deployed. Every route already returns a stub `200`; only genuinely-undefined paths return `501`. **Don't hardcode the URL** — read it from Parameter Store, because it changes on redeploys.

```bash
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url --query Parameter.Value --output text --profile <your-profile>)
curl -i $API/products                 # 200 — {"data": {"service": "product", ...}, "error": null}  (stub)
curl -i $API/definitely-not-a-route   # 501 — the catch-all stub (only truly-unknown paths 501)
```

Your goal is to turn *your* route's stub `200` into a real `200` backed by DynamoDB. Every other route keeps working independently.

**See every shared parameter (API URL, table names):**

```bash
aws ssm get-parameters-by-path --path /anomalypulse --recursive --query "Parameters[].Name" --profile <your-profile>
```

(Region is **us-east-2**. If a command says "no credentials" or "invalid token," see §5.)

---

## 5. (Optional) AWS access — for testing and seeding, not deploying

You do **not** need AWS credentials to write and unit-test your handler, and you never deploy. You *do* need AWS access if you want to (a) curl the deployed API from a profile-scoped command, or (b) run a seed script that writes to your DynamoDB table. If so, do the one-time Identity Center setup below; otherwise skip it and rely on local tests (§6c) plus Jake's post-merge deploy.

**Before you start, you need from Jake:** an IAM Identity Center invitation email, and your permission set assigned to the account.

1. **Accept the invite.** Click **Accept invitation** in the email, set a password, enroll MFA. You land on your access portal — a URL like `https://d-xxxxxxxxxx.awsapps.com/start`. **Bookmark it.**
   > You sign in only at the `awsapps.com/start` portal — never at a `...signin.aws.amazon.com/console` page. Using a root or IAM-user password at the portal is the #1 login failure.
2. **Install the toolchain:** [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), **Python 3**, and — only if you want to run the API locally (§6c) — the [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) + Docker. Open a **new terminal** afterward so PATH updates take effect. Verify: `aws --version`, `python3 --version`.
3. **Connect the CLI:** run `aws configure sso`.

   | Prompt | What to enter |
   |---|---|
   | SSO session name | anything memorable, e.g. `AnomalyPulse` |
   | SSO start URL | your portal URL: `https://d-xxxxxxxxxx.awsapps.com/start` |
   | SSO region | `us-east-2` |
   | SSO registration scopes | press **Enter** (default `sso:account:access`) |

   A browser opens — log in with your **Identity Center** credentials and approve. Back in the terminal: select the account + permission set, set **default region** `us-east-2`, **output** `json`, and a short **profile name** (this doc calls it `<your-profile>`).
   > The start URL must end in `/start` — not a console URL. The CLI rejects console URLs with `Invalid start url provided`.
4. **Verify:** `aws sts get-caller-identity --profile <your-profile>` returns JSON whose `Arn` contains `AWSReservedSSO` and your username, and does **not** end in `:root`.
   > **Later:** when commands fail with a token/auth error, re-authenticate: `aws sso login --profile <your-profile>`.

---

## 6. How to build YOUR service (the important part)

Your Lambda receives the **full HTTP request** as `event` and must return the response envelope. Because one Lambda serves all of your service's routes, you branch on which route was called.

### 6a. The handler contract

```python
import json

def _resp(status, data=None, error=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": data, "error": error}),
    }

def handler(event, context):
    route = event["resource"]        # the ROUTE TEMPLATE, e.g. "/products/{id}"  (not "/products/42")
    method = event["httpMethod"]     # "GET", "POST", "DELETE", ...
    # Branch on (route, method) to serve each of your routes (see §7 for your list).
    if route == "/products" and method == "GET":
        ...
    return _resp(501, error={"code": "NOT_IMPLEMENTED", "message": "route not handled"})
```

Key fields on `event`:
- `event["resource"]` — the **route template** (`/products/{id}`). Branch on this, not `event["path"]` (which is the concrete `/products/42`).
- `event["pathParameters"]` — path variables, e.g. `{"id": "42"}` or `{"user": "u1", "item": "sku9"}`.
- `event["body"]` — the raw request body **as a string**; `json.loads(event["body"] or "{}")` to parse a POST payload.
- `event["queryStringParameters"]` — query params, or `None`.

**Always return the envelope** (`{"data": ..., "error": null}` on success; `{"data": null, "error": {...}}` on failure) — never a bare value and never a stack trace. See §7.

### 6b. Real DynamoDB

Your table already exists and your Lambda's IAM role is already scoped to it (read-only for Product; read/write for Cart and Order). The table name arrives in the `TABLE_NAME` environment variable — Jake wired it in the template, so just read it:

```python
import os, boto3
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
# e.g. GET /products:            items = table.scan().get("Items", [])
# e.g. GET /products/{id}:       item  = table.get_item(Key={"id": pid}).get("Item")
```

Because your role is scoped to just your table, code that touches another service's table is denied — that least-privilege guardrail is intentional. (Payment has **no** table and no `TABLE_NAME` — see §7.)

If your handler needs a third-party package, add a `requirements.txt` in your service folder; `sam build` installs it. `boto3` is already in the Lambda runtime, so M1 needs no requirements file.

### 6c. Test it

**Fast loop — local unit tests (no AWS needed).** Structure your handler so the request-handling logic is callable with a synthetic `event`, and assert on the returned envelope. Mock or stub the boto3 calls (e.g. with `unittest.mock`) so tests run offline. Put tests in your folder (`services/<service>/test_handler.py`).

**Optional — run the API locally (needs SAM CLI + Docker).** From `AnomalyPulse/infra/shared/`:
```bash
sam local start-api          # serves the whole API on http://127.0.0.1:3000
curl -i http://127.0.0.1:3000/products
```
This runs your real handler in a Lambda-like container without deploying. (DynamoDB calls will hit the real table if you pass credentials, or you can point at a local DynamoDB.)

**End-to-end — after your PR merges.** Jake redeploys the stack, then you confirm against the live URL:
```bash
curl -i $API/<YOUR_ROUTE>    # e.g. /products — now returns your real data
```

### 6d. Open a PR

When your handler meets its Definition of Done, push your branch and open a PR to `main`. Jake reviews, merges, and runs the single deploy (`sam build && sam deploy` from `infra/shared/`) that takes your code live. You never run `sam deploy` yourself — one owner deploying the shared stack keeps everyone's state consistent.

> **Need a route added or changed?** Routes live in Jake's template, so ping Jake — don't try to add one yourself. The routes in §7 are the agreed set; changing them is a coordination decision.

---

## 7. Contracts you must follow

These keep five people's code compatible. **Everything in this section is FIXED contract.**

### API response envelope

Every endpoint returns the same shape, so callers can rely on it:

```json
// success
{ "data": <YOUR_PAYLOAD>, "error": null }

// error
{ "data": null, "error": { "code": "<ERROR_CODE>", "message": "<HUMAN_READABLE>" } }
```

Return structured errors, **not** stack traces. This is what lets Melisa's Order service stub Rachel's Payment call before Payment is finished.

### Routes and tables (each service's slice)

| Service | Routes | Table (SSM name path) | Role scope (already wired) |
|---|---|---|---|
| Product | `GET /products`, `GET /products/{id}` | `/anomalypulse/tables/products/name` | read-only on Products |
| Cart | `POST /cart`, `GET /cart/{user}`, `DELETE /cart/{user}/{item}` | `/anomalypulse/tables/carts/name` | read/write on Carts |
| Order | `POST /orders`, `GET /orders/{id}` | `/anomalypulse/tables/orders/name` | read/write on Orders + invoke Payment |
| Payment | `POST /payments` | — (none) | logs only |

Your `TABLE_NAME` env var already holds the resolved name, so you rarely need the SSM path directly — it's here for seed scripts and tools.

**Table keys** (only key attributes are fixed; everything else is schemaless):
- **Products** — partition key `id` (string)
- **Carts** — partition key `user_id` + sort key `item_id`, so `DELETE /cart/{user}/{item}` is a single delete
- **Orders** — partition key `order_id` (string)

**Naming conventions (fixed):**
- **Lambda function names** are `anomalypulse-<service>` (set in Jake's template). You don't declare them, but the Order→Payment call relies on them.
- **Order → Payment:** Order invokes Payment by its function name `anomalypulse-payment` (`boto3 lambda.invoke`). Order's IAM role is already allowed to invoke exactly that name. Rachel: ship the hardcoded-success Payment handler first so Melisa is unblocked.

### Telemetry / logging

Every service logs a structured JSON line per request, with the **same fields everywhere**. The field list is frozen in [`AnomalyPulse/infra/shared/telemetry_schema.json`](../AnomalyPulse/infra/shared/telemetry_schema.json). Full instrumentation is **M2**, not M1 — but build to these field names now so services don't diverge. Two rules:

- Every log line carries a **millisecond timestamp** and a **`request_id`**, and Order passes its `request_id` down to Payment so a single request can be traced across services.
- **Two tiers of fields, and they never mix.** *Tier-1* = things a monitoring system can observe (latency, status codes, request rate…). *Tier-2* = things that reveal the *cause* of a failure (e.g. `error_type`, `db_throttled`). **Never emit a Tier-2 field from a service.** This protects the ML from data leakage; the schema file marks which is which.

### Special case — Payment is a simulator, not a real processor

Payment (Rachel, M1-5) is different, so read this before wiring it. There is **no real payment processor and no DynamoDB table** — Payment exists to *simulate* payment outcomes so the incident simulator (M3) can inject failures through it. Its configurable behavior **is** the deliverable.

- **No table.** Payment's handler has no `TABLE_NAME` and its role is logs-only.
- **Three fault-injection knobs, already declared as env vars defaulting to off:** `PAYMENT_LATENCY_MS` (artificial delay), `PAYMENT_FAILURE_RATE` (0.0–1.0 probability of a failure response), `PAYMENT_TIMEOUT` (hang long enough to trip the caller). Read them with `os.environ`. Ship a hardcoded-success version first to unblock Melisa, then implement the knobs; M3 turns them up to inject dependency-failure and Lambda-slowdown incidents.
- **Payment's telemetry stays Tier-1.** The injected delay is a Tier-2 fault-injection parameter, so it must **not** appear in Payment's log line — Payment's `latency_ms` already reflects any injected slowdown, which is exactly the signal the ML should see *without being told the cause*. The injected value is recorded separately by the simulator's manifest (M3/M4), never by the service.

---

## 8. Per-person quick start

Everyone: §3 (branch + open your handler) → §6 (write handler → test → PR). Then:

- **Josh — Product (M1-2).** Two GET routes, read-only against Products. Add a seed script (`services/product/seed.py`, 20–30 dummy products) and a test script. Highest-traffic service; clean structure here pays off (you own load generation later).
- **Linu — Cart (M1-3).** Three routes including a `DELETE`, read/write against Carts. Cover edge cases (empty cart, item not found) with proper error envelopes.
- **Melisa — Order (M1-4).** Two routes, the most connected service — it writes Orders **and** invokes Payment (`anomalypulse-payment`). You need Rachel's Payment stub to exist to finish your end-to-end test. Populate `dependency_*` fields from the Payment call — those become the raw signal for dependency-failure detection later.
- **Rachel — Payment (M1-5).** One route, no table, **plus** the three fault-injection knobs the simulator drives in M3. Ship a hardcoded-success handler first to unblock Melisa, then implement the knobs.

---

## 9. Troubleshooting cheat sheet

| Symptom | Cause | Fix |
|---|---|---|
| Your route returns **502/500** | Your `handler.py` has no `handler(event, context)`, or it raised | Confirm the function name/signature; check CloudWatch logs for the traceback (ask Jake for log access) |
| Your route returns **501** | The path isn't a defined route (hit the catch-all stub) | Check the exact path against §7; if a route is genuinely missing, ask Jake to add it to the template |
| Your change isn't live | It only goes live after Jake redeploys the merged PR | Open/merge the PR; ask Jake to deploy |
| `InvalidClientTokenId` / `security token is invalid` | SSO session expired | `aws sso login --profile <your-profile>` |
| `Unable to locate credentials` | No profile set in this terminal | Add `--profile <your-profile>`, or `export AWS_PROFILE=<your-profile>` |
| `AccessDeniedException` on DynamoDB | Code touched a table your role isn't scoped to | Use your own `TABLE_NAME`; least-privilege is intentional (§6b) |
| `sam local` won't start | Docker not running, or wrong folder | Start Docker; run from `AnomalyPulse/infra/shared/` |

**Golden rule:** if an AWS command fails, run `aws sts get-caller-identity --profile <your-profile>` first. If *that* fails, it's credentials (§5), not your code.

---

## 10. Your M1 checklist

- [ ] Pulled `main`; created your ticket branch `<TICKET>-<NAME>` (e.g. `m1-2-josh`)
- [ ] Edited `AnomalyPulse/services/<SERVICE>/handler.py` — kept the filename `handler.py` and function `handler(event, context)`
- [ ] Branch on `event["resource"]` / `event["httpMethod"]` to serve all your routes (§6a)
- [ ] Real DynamoDB via `os.environ["TABLE_NAME"]` (§6b) — Payment: real payment logic instead, using the env-var knobs
- [ ] Following the response-envelope and naming conventions (§7)
- [ ] Structured log line per request in the frozen schema shape (§7) — Tier-1 only, never Tier-2
- [ ] Local test passes (§6c); edge cases return structured error envelopes, not stack traces
- [ ] (Josh/Linu/Melisa) seed/test scripts added under your service folder
- [ ] (Rachel) hardcoded-success Payment handler shipped early; Melisa confirmed unblocked
- [ ] PR opened to `main`; confirmed your route returns real data after Jake's deploy

---

*Questions → ping Jake. If it's an `InvalidClientTokenId`, try §5 first — that one's almost always just an expired login.*
