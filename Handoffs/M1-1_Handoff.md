# M1-1 Handoff — Shared Infrastructure & API Gateway

**From:** Jake
**Milestone:** M1-1 (Shared infrastructure & API Gateway skeleton)
**Status:** ✅ Shared API is **live**. Tables, IAM roles, and the telemetry schema are landing next (see §9).
**Who this is for:** Josh (Product), Linu (Cart), Melisa (Order), Rachel (Payment) — and anyone new to AWS.

> **The one-sentence version:** There is now one live API Gateway that everybody's service plugs into. It answers `501 Not Implemented` on every route until *you* wire your service to *your* routes — and you can do that from your own separate deployment without ever touching my code.

---

## 1. Status at a glance

| Thing | Status | Where |
|---|---|---|
| Shared API Gateway (REST API) | ✅ **Live** | URL in Parameter Store (§4) |
| Catch-all `501` on every route | ✅ **Live** | Try it: §4 |
| API ID / URL / root-resource-id published | ✅ **Live** | Parameter Store (§4) |
| Branch + folder workflow | ✅ Ready | §5 |
| "Attach your service" pattern | ✅ Proven & documented | §6 |
| DynamoDB tables (Products / Carts / Orders) | 🚧 **Coming next from Jake** | §9 |
| Per-service IAM roles | 🚧 **Coming next from Jake** | §9 |
| `telemetry_schema.json` (Tier-1/Tier-2) | 🚧 **Coming next from Jake** | §9 |
| API response envelope contract | 🚧 Proposed — ratify at kickoff | §7 |

**What this means for you:** you are **unblocked to start today.** You can set up your AWS access, scaffold your service, wire your routes to the live API, and return placeholder (stub) data right now. You do **not** need to wait for the tables and IAM roles to start — you only need them for the *last* step (reading/writing real data). More on that in §6 and §9.

---

## 2. Mental model (read this if you're new to AWS)

A few ideas make everything below click. Skip if you already know them.

**The shared API is a building with one street address.** That address is the base URL. Inside the building are rooms, and each room is a *route* — `/products` is a room, `/cart` is a room, `/orders` is a room. Each of you furnishes one or two rooms; that's your service. Right now the building is standing but every room is empty, so knocking on any door gets you a polite "501 — not built yet."

**A "stack" is one deployment you own.** In AWS, a *CloudFormation stack* is a bundle of resources that get created/updated/deleted together. **My** shared stack owns the building itself. **Each of you** will have your **own** stack that owns just your rooms. The golden rule of AWS: *one resource is managed by exactly one stack.* Two people can't both own `/products`. That's why the building (mine) and the rooms (yours) are separate stacks.

**Parameter Store is the shared address book.** Instead of me emailing you the API's URL and ID (which change every time I redeploy), I publish them to **AWS Systems Manager Parameter Store**. Your code looks them up by a fixed name. You never hardcode a URL or ID — you look it up. This is how your stack "finds" my building.

**SAM is the tool that turns a YAML file into real AWS resources.** You write a `template.yaml` describing what you want; `sam deploy` makes it real. You already have SAM installed.

---

## 3. FIRST: get your AWS CLI, SAM CLI, and AWS Identity working ⚠️

This gets you from your IAM Identity Center invite to a working **AWS CLI + SAM** setup on your own machine, so you can deploy your service. 

**Before you start, you need from Jake:**
- An email invitation to IAM Identity Center (subject: *"Invitation to join AWS IAM Identity Center"*).
- Your permission set assigned to the account. If the account picker in Step 3 comes up empty, that assignment is missing — ping Jake.

---

### 3a. Accept your Identity Center invite

1. Find the invitation email and click **Accept invitation**.
2. Set your password and enroll an **MFA device** when prompted (an authenticator app on your phone is fine).
3. You'll land on your **access portal** — a URL like `https://d-xxxxxxxxxx.awsapps.com/start`. **Bookmark it**; you need it in Step 3.

> **Know your identities.** This Identity Center login is its *own* identity, separate from the AWS root account and from any IAM user. You sign in only at the `awsapps.com/start` portal — never at a `...signin.aws.amazon.com/console` page. Using a root or IAM-user password at the portal is the #1 login failure.

---

### 3b. Install the toolchain

Install on your machine:
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- **Python 3** (the project uses Python/boto3). Install Node.js only if you'll write your Lambda in Node.

> **Pitfall — two separate installs.** The SAM CLI installer does **not** bundle the AWS CLI. Install both.

> **Pitfall — PATH.** After installing, open a **brand-new terminal window** before running the checks below. Installers update your PATH, and an already-open terminal won't see the new commands yet.

Verify in the new terminal:
```
aws --version
sam --version
python3 --version
```
All three should print a version string. If `aws` or `sam` reports "command not found," the install didn't land — or you're still in an old terminal.

---

### 3c. Connect the CLI to your Identity Center login

Run:
```
aws configure sso
```
Answer the prompts:

| Prompt | What to enter |
|---|---|
| SSO session name | anything memorable, e.g. `AnomalyPulse` |
| SSO start URL | your access portal URL from Step 1: `https://d-xxxxxxxxxx.awsapps.com/start` |
| SSO region | `us-east-2` |
| SSO registration scopes | press **Enter** to accept the default (`sso:account:access`) |

A browser opens — log in with your **Identity Center** credentials from Step 1 and approve. Back in the terminal:
- Select the **account** and the **permission set** Jake assigned you.
- **CLI default client region:** `us-east-2`
- **CLI default output format:** `json`.
- **CLI profile name:** something short, e.g. your first name — you'll pass this as `--profile` on every command.

> **Pitfall — the start URL.** It must be the **AWS access portal URL** (`https://...awsapps.com/start`). Do **not** paste your browser's address bar or a `...signin.aws.amazon.com/console` URL — the CLI rejects those with `Invalid start url provided`. A valid one starts with `https://` and ends in `/start`, with nothing after it.

---

### 3d. Verify

```
aws sts get-caller-identity --profile <your-profile>
```
You should get back JSON with `UserId`, `Account`, and `Arn`. The **`Arn`** should contain `AWSReservedSSO` and your username — and must **not** end in `:root`.

> **Later:** SSO credentials expire. When commands start failing with a token/auth error, just re-authenticate: `aws sso login --profile <your-profile>`.

---

#### You're set

Once `get-caller-identity` shows your SSO identity, your machine can deploy into the shared account. 


## 4. What's live right now, and how to see it

The shared API is deployed. **Don't hardcode its URL** — read it from Parameter Store, because it changes on redeploys.

**Published parameters (the address book):**

| Parameter name | What it is |
|---|---|
| `/anomalypulse/api/base-url` | The base URL of the API (e.g. `https://xxxx.execute-api.us-east-2.amazonaws.com/dev`) |
| `/anomalypulse/api/rest-api-id` | The API's ID — **this is what your service stack needs to attach** |
| `/anomalypulse/api/root-resource-id` | The API's root resource ID (some setups need it) |

**See them all:**

```bash
aws ssm get-parameters-by-path --path /anomalypulse --recursive --query "Parameters[].Name"
```

**Grab the URL and knock on a couple of doors:**

```bash
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url --query Parameter.Value --output text)
curl -i $API/products
curl -i $API/anything
```

Both return `HTTP/2 501` with this body:

```json
{"data": null, "error": {"code": "NOT_IMPLEMENTED", "message": "Route not yet wired"}}
```

That's correct and expected. Every path — real or made-up — hits a single catch-all stub until you attach your real routes. The moment you do, *your* specific route takes over and everything else keeps returning 501.

---

## 5. Getting started: branch and set up your service folder

**Wait until M1-1 is merged into `main` before you start.** That merge is what puts the shared stack, this handoff, and the shared contracts into `main` for you to build against. (The API itself is already live and reachable now — but pull from `main` so you're working from the agreed contracts, not a moving target.)

We're using **branch-per-ticket**: each of you works on your own ticket branch and merges back to `main` when the ticket is done. Once M1-1 is in `main`:

```bash
git checkout main
git pull                      # get the merged shared stack + this doc + contracts
git checkout -b m1-2-<YOUR NAME>  # your ticket branch: <ticket>-<name>
```

Then create **your** service's folder under `infra/` and do all your work there:

```bash
mkdir -p msml650_project/AnomalyPulse/infra/<YOUR SERVICE>        # one folder per service
cd msml650_project/AnomalyPulse/infra/<YOUR SERVICE> 
```

Your folder sits **alongside** `infra/shared/` (my stack) — you're adding a new, separate stack next to mine, never editing mine. When your ticket meets its Definition of Done, open a PR and merge to `main`.

```
AnomalyPulse/infra/
├─ shared/     ← Jake    (M1-1, already merged)
├─ product/    ← Josh    (M1-2)
├─ cart/       ← Linu    (M1-3)
├─ order/      ← Melisa  (M1-4)
└─ payment/    ← Rachel  (M1-5)
```

---

## 6. How to build YOUR service (the important part)

Your job in M1 is: create a Lambda, and attach your routes to the **shared** API by looking up its ID. You never edit my stack.

### 6a. Minimal working example

Here's a complete `infra/product/template.yaml` that stands up the two Product routes returning stub data. Copy the shape; swap in your own service name and routes.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Description: Product service (M1-2)

Parameters:
  # This looks up the shared API's ID from Parameter Store automatically.
  SharedApiId:
    Type: AWS::SSM::Parameter::Value<String>
    Default: /anomalypulse/api/rest-api-id

Resources:
  ProductFunction:
    Type: AWS::Serverless::Function
    Properties:
      FunctionName: anomalypulse-product      # naming convention — see §7
      Runtime: python3.12
      Handler: index.handler
      Timeout: 10
      InlineCode: |
        import json
        def handler(event, context):
            # Stub for now. Real DynamoDB code comes once the table + role land (§9).
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"data": [], "error": None}),
            }
      Events:
        ListProducts:
          Type: Api
          Properties:
            RestApiId: !Ref SharedApiId       # <-- attaches to the SHARED api
            Path: /products
            Method: GET
        GetProduct:
          Type: Api
          Properties:
            RestApiId: !Ref SharedApiId
            Path: /products/{id}
            Method: GET
```

**The one line that matters:** `RestApiId: !Ref SharedApiId`. That's what plugs your route into the shared building instead of creating a new one.

### 6b. Deploy it as your own stack

```bash
cd infra/product
sam build
sam deploy --guided
```

At the prompts:
- **Stack name:** `anomalypulse-product` (your service — **not** `anomalypulse-shared`, that's mine)
- **Region:** `us-east-2`
- **Allow SAM CLI IAM role creation:** `Y`
- **Disable rollback:** `N` ← (say **no**; "yes" leaves broken half-deployments lying around)
- **"…has no authentication. Is this okay?":** `Y` (auth is a stretch goal, not now)
- **Save arguments to config file:** `Y`
- The last two prompts (config file name, environment) — just press **Enter** to accept the defaults. Don't type `y` — those aren't yes/no questions, they're asking for a filename.

Future deploys are just `sam deploy` (no `--guided`).

### 6c. Confirm your route took over

```bash
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url --query Parameter.Value --output text)
curl -i $API/products      # should now be 200 with your data
curl -i $API/cart          # still 501 — not your route, still the catch-all
```

### 6d. ⚠️ If your new route still returns 501 after deploying

Sometimes the API needs a nudge to publish the change. Run this once:

```bash
APIID=$(aws ssm get-parameter --name /anomalypulse/api/rest-api-id --query Parameter.Value --output text)
aws apigateway create-deployment --rest-api-id $APIID --stage-name dev
```

Then curl again. This is a known quirk of sharing one API across separate stacks — it's not you doing anything wrong. If it keeps happening, tell Jake.

### Why this works (so you trust it)

Your `/products` is a **specific** route. My catch-all is `/{proxy+}`, a **greedy** wildcard. API Gateway always prefers the more specific match, so your route wins the moment it exists, and every un-wired path still falls through to the 501 stub. Different resources, different owners, no collision — as long as each service sticks to its own top-level path (`/products`, `/cart`, `/orders`, `/payments`).

---

## 7. Contracts you must follow

These keep five people's code compatible. A couple are still being finalized (marked 🚧) — flag disagreements at kickoff **before** you write a Lambda.

### API response envelope (🚧 ratify at kickoff)

Every endpoint returns the same shape, so callers can rely on it:

```json
// success
{ "data": <your payload>, "error": null }

// error
{ "data": null, "error": { "code": "SOME_CODE", "message": "human readable" } }
```

Return structured errors, **not** stack traces. This is what lets Melisa's Order service stub Rachel's Payment call before Payment is finished.

### Naming conventions (please follow exactly)

- **Lambda function names:** `anomalypulse-<service>` → `anomalypulse-product`, `anomalypulse-cart`, `anomalypulse-order`, `anomalypulse-payment`.
- **Route paths (agree these at kickoff so nobody writes `/product` vs `/products`):**
  - Product: `GET /products`, `GET /products/{id}`
  - Cart: `POST /cart`, `GET /cart/{user}`, `DELETE /cart/{user}/{item}`
  - Order: `POST /orders`, `GET /orders/{id}`
  - Payment: `POST /payments`
- **Special case — Order → Payment:** Order calls Payment by its function name `anomalypulse-payment`. Rachel: please ship a hardcoded-success Payment stub **early** so Melisa is unblocked, then build the real thing. Melisa's IAM role will be allowed to invoke `anomalypulse-payment` by that exact name, so don't rename it.

### Telemetry / logging (🚧 schema coming from Jake — §9)

Every service logs a structured JSON line per request (same fields everywhere). Full instrumentation is **M2**, not M1 — but the field list is being frozen now so services don't diverge. Two rules to internalize now:

- Every log line carries a **millisecond timestamp** and a **`request_id`**, and Order passes its `request_id` down to Payment so a single request can be traced across services.
- **Two tiers of fields, and they never mix.** *Tier-1* = things a monitoring system can observe (latency, status codes, request rate…). *Tier-2* = things that reveal the *cause* of a failure (e.g. `error_type`, `db_throttled`). **Never put a Tier-2 field in a service's telemetry.** This protects the ML later. Jake's `telemetry_schema.json` will spell out exactly which is which.

---

## 8. Per-person quick start

Everyone: do §3 (access) → §5 (branch + folder) → §6 (scaffold + attach + stub data). Then:

- **Josh — Product (M1-2).** Two GET routes. Highest-traffic service. Depends only on the shared stack. Use the §6a example almost as-is. You'll also own load generation later, so clean structure here pays off.
- **Linu — Cart (M1-3).** Three routes including a `DELETE`. Fully independent (Cart calls no one). Cover edge cases (empty cart, item not found) with proper error envelopes.
- **Melisa — Order (M1-4).** Two routes, the most connected service — it calls Payment. You need Rachel's Payment stub to exist to finish your end-to-end test. Populate the `dependency_*` fields from the Payment call; those become the raw signal for dependency-failure detection later.
- **Rachel — Payment (M1-5).** One route **plus** the fault-injection knobs (`PAYMENT_LATENCY_MS`, `PAYMENT_FAILURE_RATE`, `PAYMENT_TIMEOUT`) that the incident simulator drives in M3. Ship a hardcoded-success stub in the first few hours to unblock Melisa, then build the configurable version.

---

## 9. What's still coming from Jake (and how it reaches you)

These are in progress and will be **added to the same shared stack**, then announced. None of them block you from scaffolding and wiring routes with stub data today — you only need them for the final "read/write real data" step.

- **DynamoDB tables.** Proposed designs (please confirm at kickoff):
  - **Products** — key: `id` (string)
  - **Carts** — keys: `user_id` (partition) + `item_id` (sort), so `DELETE /cart/{user}/{item}` is a single delete
  - **Orders** — key: `order_id` (string)
  - Table names will be published to Parameter Store, e.g. `/anomalypulse/tables/products/name`.
- **Per-service IAM roles** (least privilege). Each service gets a role scoped to just its table (Order also gets permission to invoke Payment). Role ARNs published to e.g. `/anomalypulse/iam/product-role-arn`.
- **`telemetry_schema.json`** — the frozen Tier-1/Tier-2 field list (§7).

**When your table + role are published, here's the change you make** (add a role and swap the stub for real DynamoDB code):

```yaml
Parameters:
  SharedApiId:
    Type: AWS::SSM::Parameter::Value<String>
    Default: /anomalypulse/api/rest-api-id
  ServiceRoleArn:
    Type: AWS::SSM::Parameter::Value<String>
    Default: /anomalypulse/iam/product-role-arn     # your service's role
  ProductsTableName:
    Type: AWS::SSM::Parameter::Value<String>
    Default: /anomalypulse/tables/products/name

Resources:
  ProductFunction:
    Type: AWS::Serverless::Function
    Properties:
      # ...everything from §6a, plus:
      Role: !Ref ServiceRoleArn
      Environment:
        Variables:
          TABLE_NAME: !Ref ProductsTableName
```

Until then: scaffold, wire routes, return stub data. You lose nothing by starting now.

---

## 10. Troubleshooting cheat sheet

| Symptom | Cause | Fix |
|---|---|---|
| `InvalidClientTokenId` / `security token is invalid` | SSO session expired, or wrong profile | `aws sso login --profile admin` then `export AWS_PROFILE=admin` (§3) |
| `Unable to locate credentials` | No profile set in this terminal | `export AWS_PROFILE=admin` |
| `Template file not found` | Running `sam` from the wrong folder | `cd` into the folder with `template.yaml`, or `sam build -t path/to/template.yaml` |
| Deploy fails with a profile-not-found | `samconfig.toml` references a profile you deleted/renamed | Edit `samconfig.toml` or re-run `sam deploy --guided` |
| Your new route still returns 501 | API stage needs a redeploy | Run the `create-deployment` command in §6d |
| Deploy stuck / half-created after a failure | "Disable rollback" was set to yes | Delete the stack in the CloudFormation console, redeploy with rollback enabled (answer `N`) |
| A `Type`/`Severless`/typo error at build | YAML typo | Run `sam validate` — it points at the line |

**Golden rule:** if a command touching AWS fails, run `aws sts get-caller-identity` first. If *that* fails, it's credentials (§3), not your code.

---

## 11. Your M1 checklist

- [ ] `~/.aws/config` has the `admin` + `dev` profiles (§3)
- [ ] `aws sso login --profile admin` + `export AWS_PROFILE=admin` works; `aws sts get-caller-identity` returns your identity
- [ ] M1-1 merged to `main`; pulled `main`, created your ticket branch (e.g. `m1-2-product`)
- [ ] Service folder created under `infra/<service>/` + `template.yaml` scaffolded (§5–§6)
- [ ] Your routes attach to the shared API via `RestApiId: !Ref SharedApiId`
- [ ] Deployed as your **own** stack (`anomalypulse-<service>`), your routes return 200 stub data, other routes still 501
- [ ] Following the response-envelope and naming conventions (§7)
- [ ] (Rachel) hardcoded-success Payment stub shipped early; Melisa confirmed unblocked
- [ ] (When published) swapped stub for real DynamoDB using your role + table name (§9)

---

*Questions → ping Jake. If it's an `InvalidClientTokenId`, try §3 first — that one's almost always just an expired login.*
