# M1-1 Handoff — Shared Infrastructure & API Gateway

> **The one-sentence version:** There is one live API Gateway that everybody's service plugs into. It answers `501 Not Implemented` on every route until *you* wire your service to *your* routes — and you do that from your own separate deployment without ever touching my code. The tables and IAM roles your service needs are already live too, so you can build all the way to real data in one pass. Everything the shared stack provides is **live** in `us-east-2` (stack `anomalypulse-shared`). 

---

## 1. Mental model (read this if you're new to AWS)

A few ideas make everything below click. Skip if you already know them.

**The shared API is a building with one street address.** That address is the base URL. Inside the building are rooms, and each room is a *route* — `/products` is a room, `/cart` is a room, `/orders` is a room. Each of you furnishes one or two rooms; that's your service. Right now the building is standing but every room is empty, so knocking on any door gets you a polite "501 — not built yet."

**A "stack" is one deployment you own.** In AWS, a *CloudFormation stack* is a bundle of resources that get created/updated/deleted together. **My** shared stack owns the building itself (plus the tables and roles). **Each of you** will have your **own** stack that owns just your rooms. The golden rule of AWS: *one resource is managed by exactly one stack.* Two people can't both own `/products`. That's why the building (mine) and the rooms (yours) are separate stacks.

**Parameter Store is the shared address book.** Instead of me emailing you the API's URL, table names, and role ARNs (which change every time I redeploy), I publish them to **AWS Systems Manager Parameter Store**. Your code looks them up by a fixed name. You never hardcode a URL, table name, or ARN — you look it up. This is how your stack "finds" my building.

**SAM is the tool that turns a YAML file into real AWS resources.** You write a `template.yaml` describing what you want; `sam deploy` makes it real.

---

## 3. FIRST: get your AWS CLI, SAM CLI, and AWS identity working ⚠️

This gets you from your IAM Identity Center invite to a working **AWS CLI + SAM** setup on your own machine, so you can deploy your service.

**Before you start, you need from Jake:**
- An email invitation to IAM Identity Center (subject: *"Invitation to join AWS IAM Identity Center"*).
- Your permission set assigned to the account. If the account picker in Step 3c comes up empty, that assignment is missing — ping Jake.

---

### 3a. Accept your Identity Center invite

1. Find the invitation email and click **Accept invitation**.
2. Set your password and enroll an **MFA device** when prompted (an authenticator app on your phone is fine).
3. You'll land on your **access portal** — a URL like `https://d-xxxxxxxxxx.awsapps.com/start`. **Bookmark it**; you need it in Step 3c.

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
| SSO start URL | your access portal URL from Step 3a: `https://d-xxxxxxxxxx.awsapps.com/start` |
| SSO region | `us-east-2` |
| SSO registration scopes | press **Enter** to accept the default (`sso:account:access`) |

A browser opens — log in with your **Identity Center** credentials from Step 3a and approve. Back in the terminal:
- Select the **account** and the **permission set** Jake assigned you.
- **CLI default client region:** `us-east-2`
- **CLI default output format:** `json`
- **CLI profile name:** something short, e.g. your first name — you'll pass this as `--profile` on every command. This doc calls it `<your-profile>`.

> **Pitfall — the start URL.** It must be the **AWS access portal URL** (`https://...awsapps.com/start`). Do **not** paste your browser's address bar or a `...signin.aws.amazon.com/console` URL — the CLI rejects those with `Invalid start url provided`. A valid one starts with `https://` and ends in `/start`, with nothing after it.

---

### 3d. Verify

```
aws sts get-caller-identity --profile <your-profile>
```
You should get back JSON with `UserId`, `Account`, and `Arn`. The **`Arn`** should contain `AWSReservedSSO` and your username — and must **not** end in `:root`.

> **Later:** SSO credentials expire. When commands start failing with a token/auth error, just re-authenticate: `aws sso login --profile <your-profile>`.

Once `get-caller-identity` shows your SSO identity, your machine can deploy into the shared account.

---

## 4. What's live right now, and how to see it

The shared API is deployed. **Don't hardcode its URL** — read it from Parameter Store, because it changes on redeploys.

**Grab the URL and knock on a couple of doors:**

```bash
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url --query Parameter.Value --output text --profile <your-profile>)
curl -i $API/products
curl -i $API/anything
```

Both return `HTTP/2 501` with this body:

```json
{"data": null, "error": {"code": "NOT_IMPLEMENTED", "message": "Route not yet wired"}}
```

That's correct and expected. Every path — real or made-up — hits a single catch-all stub until you attach your real routes. The moment you do, *your* specific route takes over and everything else keeps returning 501. (Region is **us-east-2**. If a command says "no credentials" or "invalid token," see §3.)

**See every shared parameter (URL, table names, role ARNs):**

```bash
aws ssm get-parameters-by-path --path /anomalypulse --recursive --query "Parameters[].Name" --profile <your-profile>
```

The full list, and what each value is for, is in the [README's shared-infrastructure reference](../README.md#shared-infrastructure-reference).

---

## 5. Getting started: branch and set up your service folder

> **IMPORTANT:** Anywhere below you see a placeholder in `<ALL_CAPS_ANGLE_BRACKETS>`, replace it with your own value — the `# e.g.` comment beside it shows a real example. Lines or values marked **FIXED** are shared contract: copy them **exactly**, don't change them. One gotcha: `AWS::SSM::Parameter::Value<String>` has angle brackets that are *real AWS syntax*, **not** a placeholder — leave that one exactly as written.

We're using **branch-per-ticket**: each of us works on our own ticket branch and merges back to `main` when the ticket is done. Once M1-1 is in `main`:

```bash
git checkout main
git pull                             # get the merged shared stack + this doc + contracts
git checkout -b <TICKET>-<NAME>      # e.g. m1-2-josh
```

Then create **your** service's folder under `AnomalyPulse/infra/` and do all your work there:

```bash
mkdir -p AnomalyPulse/infra/<SERVICE>     # e.g. AnomalyPulse/infra/product
cd AnomalyPulse/infra/<SERVICE>
```

Your folder sits **alongside** `infra/shared/` (my stack) — you're adding a new, separate stack next to mine, never editing mine. When your ticket meets its Definition of Done, open a PR and merge to `main`.

```
AnomalyPulse/infra/
├─ shared/     ← Jake    (M1-1, done)
├─ product/    ← Josh    (M1-2)
├─ cart/       ← Linu    (M1-3)
├─ order/      ← Melisa  (M1-4)
└─ payment/    ← Rachel  (M1-5)
```

---

## 6. How to build YOUR service (the important part)

Your job in M1 is: create a Lambda, attach your routes to the **shared** API by looking up its ID, and read/write your real table. You never edit my stack. The recommended path is to deploy a **stub** first (proves the wiring), then swap the handler body for real DynamoDB code (§6d).

### 6a. Fill-in template

Here's a complete `AnomalyPulse/infra/<SERVICE>/template.yaml`. It looks up the shared API's ID, your service's IAM role ARN, and your table name — all from Parameter Store. Replace every `<ALL_CAPS>` placeholder; leave the **FIXED** lines exactly as written.

> **Payment has no table.** Rachel: omit `ServiceTableName` and the `TABLE_NAME` env var — Payment stores nothing.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Description: <SERVICE> service (<TICKET>)              # e.g. Product service (M1-2)

Parameters:
  # FIXED — each looks a value up from Parameter Store. Copy exactly.
  SharedApiId:
    Type: AWS::SSM::Parameter::Value<String>          # <String> is real AWS syntax — leave it
    Default: /anomalypulse/api/rest-api-id            # FIXED
  ServiceRoleArn:
    Type: AWS::SSM::Parameter::Value<String>
    Default: /anomalypulse/iam/<SERVICE>-role-arn     # e.g. /anomalypulse/iam/product-role-arn
  ServiceTableName:                                   # (Payment: delete this block)
    Type: AWS::SSM::Parameter::Value<String>
    Default: /anomalypulse/tables/<TABLE>/name        # e.g. /anomalypulse/tables/products/name

Resources:
  <SERVICE>Function:                                  # e.g. ProductFunction
    Type: AWS::Serverless::Function
    Properties:
      FunctionName: anomalypulse-<SERVICE>            # e.g. anomalypulse-product  (FIXED prefix)
      Runtime: python3.12
      Handler: index.handler
      Timeout: 10
      Role: !Ref ServiceRoleArn                       # FIXED — your least-privilege role from Jake's stack
      Environment:
        Variables:
          TABLE_NAME: !Ref ServiceTableName           # (Payment: delete this) — your table's name
      InlineCode: |
        import json
        def handler(event, context):
            # STUB for now — return your own data in "data" below.
            # Swap for real DynamoDB code once the wiring is proven (see 6d).
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"data": [], "error": None}),
            }
      Events:
        <ROUTE_NAME>:                                 # e.g. ListProducts
          Type: Api
          Properties:
            RestApiId: !Ref SharedApiId               # FIXED — this is what attaches you to the shared API
            Path: <ROUTE_PATH>                        # your route from §7; e.g. /products
            Method: <METHOD>                          # e.g. GET
        # Add one event block per route you own (see §7). Example second route:
        #   <ROUTE_NAME_2>:            # e.g. GetProduct
        #     Type: Api
        #     Properties:
        #       RestApiId: !Ref SharedApiId           # FIXED
        #       Path: <ROUTE_PATH_2>                  # e.g. /products/{id}
        #       Method: <METHOD_2>                    # e.g. GET
```

**The three lines that matter:** `RestApiId: !Ref SharedApiId` (attaches your route to the shared building), `Role: !Ref ServiceRoleArn` (gives your Lambda exactly the permissions it needs, no more), and `TABLE_NAME: !Ref ServiceTableName` (tells your code which table to read/write).

### 6b. Deploy it as your own stack

```bash
cd AnomalyPulse/infra/<SERVICE>          # e.g. AnomalyPulse/infra/product
sam build
sam deploy --guided
```

At the prompts:
- **Stack name:** `anomalypulse-<SERVICE>` (e.g. `anomalypulse-product`) — your service, **not** `anomalypulse-shared`, that's mine
- **Region:** `us-east-2`
- **Allow SAM CLI IAM role creation:** `Y`
- **Disable rollback:** `N`
- **"…has no authentication. Is this okay?":** `Y`
- **Save arguments to config file:** `Y`
- The last two prompts (config file name, environment) — just press **Enter** to accept the defaults.

Future deploys are just `sam deploy` (no `--guided`).

### 6c. Confirm your route took over

```bash
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url --query Parameter.Value --output text --profile <your-profile>)
curl -i $API/<YOUR_ROUTE>     # e.g. /products — should now be 200 with your stub data
curl -i $API/<OTHER_ROUTE>    # e.g. /cart — a route you don't own, still 501 (the catch-all)
```

### 6d. Swap the stub for real DynamoDB

Your role and table are already live, so now replace the stub handler body with real boto3 code. The table name arrives in the `TABLE_NAME` env var you wired above:

```python
import json, os, boto3
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

def handler(event, context):
    # e.g. for GET /products:  items = table.scan().get("Items", [])
    # Return the response envelope from §7. Handle errors as structured bodies.
    ...
```

Redeploy with `sam deploy`, then curl your route again — you should get real data back. Because your IAM role is scoped to just your table, code that tries to touch another service's table will be denied; that's the least-privilege guardrail working as intended.

### 6e. ⚠️ If your new route still returns 501 after deploying

Sometimes the API needs a nudge to publish the change. Run this once:

```bash
APIID=$(aws ssm get-parameter --name /anomalypulse/api/rest-api-id --query Parameter.Value --output text --profile <your-profile>)
aws apigateway create-deployment --rest-api-id $APIID --stage-name dev --profile <your-profile>
```

Then curl again. This is a known quirk of sharing one API across separate stacks — it's not you doing anything wrong. If it keeps happening, tell Jake.

### Why this works (so you trust it)

Your `/products` is a **specific** route. My catch-all is `/{proxy+}`, a **greedy** wildcard. API Gateway always prefers the more specific match, so your route wins the moment it exists, and every un-wired path still falls through to the 501 stub. Different resources, different owners, no collision — as long as each service sticks to its own top-level path (`/products`, `/cart`, `/orders`, `/payments`).

---

## 7. Contracts you must follow

These keep five people's code compatible. **Everything in this section is FIXED contract** — the placeholders show *which* value is yours, but the shapes and names around them don't change.

### API response envelope

Every endpoint returns the same shape, so callers can rely on it:

```json
// success
{ "data": <YOUR_PAYLOAD>, "error": null }

// error
{ "data": null, "error": { "code": "<ERROR_CODE>", "message": "<HUMAN_READABLE>" } }
```

Return structured errors, **not** stack traces. This is what lets Melisa's Order service stub Rachel's Payment call before Payment is finished.

### Routes, tables, and roles (each service's slice)

| Service | Routes | Table (SSM name path) | Role (SSM ARN path) |
|---|---|---|---|
| Product | `GET /products`, `GET /products/{id}` | `/anomalypulse/tables/products/name` | `/anomalypulse/iam/product-role-arn` |
| Cart | `POST /cart`, `GET /cart/{user}`, `DELETE /cart/{user}/{item}` | `/anomalypulse/tables/carts/name` | `/anomalypulse/iam/cart-role-arn` |
| Order | `POST /orders`, `GET /orders/{id}` | `/anomalypulse/tables/orders/name` | `/anomalypulse/iam/order-role-arn` |
| Payment | `POST /payments` | — (none) | `/anomalypulse/iam/payment-role-arn` |

**Table keys** (only key attributes are fixed; everything else is schemaless):
- **Products** — partition key `id` (string)
- **Carts** — partition key `user_id` + sort key `item_id`, so `DELETE /cart/{user}/{item}` is a single delete
- **Orders** — partition key `order_id` (string)

**Naming conventions (follow exactly):**
- **Lambda function names:** `anomalypulse-<SERVICE>` → `anomalypulse-product`, `anomalypulse-cart`, `anomalypulse-order`, `anomalypulse-payment`. (The `anomalypulse-` prefix is FIXED.)
- **Special case — Order → Payment:** Order calls Payment by its function name `anomalypulse-payment`. Rachel: please ship a hardcoded-success Payment stub **early** so Melisa is unblocked, then build the real thing. Melisa's IAM role is already allowed to invoke `anomalypulse-payment` by that exact name, so don't rename it.

### Telemetry / logging

Every service logs a structured JSON line per request, with the **same fields everywhere**. The field list is frozen in [`AnomalyPulse/infra/shared/telemetry_schema.json`](../AnomalyPulse/infra/shared/telemetry_schema.json). Full instrumentation is **M2**, not M1 — but build to these field names now so services don't diverge. Two rules to internalize:

- Every log line carries a **millisecond timestamp** and a **`request_id`**, and Order passes its `request_id` down to Payment so a single request can be traced across services.
- **Two tiers of fields, and they never mix.** *Tier-1* = things a monitoring system can observe (latency, status codes, request rate…). *Tier-2* = things that reveal the *cause* of a failure (e.g. `error_type`, `db_throttled`). **Never emit a Tier-2 field from a service.** This protects the ML from data leakage and is *crucial* — the schema file marks exactly which is which.

### Special case — Payment is a simulator, not a real processor

Payment (Rachel, M1-5) is different from the other three services, so read this before wiring it. There is **no real payment processor and no DynamoDB table** anywhere in this project — Payment exists to *simulate* payment outcomes so the incident simulator (M3) can inject failures through it. Its configurable behavior **is** the deliverable, not a placeholder for something built later.

- **No table.** In the §6a template, delete the `ServiceTableName` parameter and the `TABLE_NAME` env var. Keep only the `SharedApiId` and `ServiceRoleArn` references. Payment's role is logs-only (it stores nothing).
- **The three fault-injection knobs are env vars that default to off/normal:** `PAYMENT_LATENCY_MS` (artificial delay), `PAYMENT_FAILURE_RATE` (0.0–1.0 probability of a failure response), `PAYMENT_TIMEOUT` (hang long enough to trip the caller). The default deploy simply approves every payment; M3 turns these up to inject dependency-failure and Lambda-slowdown incidents. Ship a hardcoded-success version first to unblock Melisa, then add the knobs.
- **Payment's telemetry stays Tier-1.** The injected delay is a Tier-2 fault-injection parameter, so it must **not** appear in Payment's log line. Payment emits only observable fields — and its `latency_ms` already reflects any injected slowdown, which is exactly the signal the ML should see *without being told the cause*. The injected value itself is recorded separately by the simulator's experiment manifest (M3/M4) for labeling, never by the service. (So "record the injected delay distinctly," which the M1-5 ticket mentions, is the *simulator's* job later — not something Payment writes into its telemetry.)

---

## 8. Per-person quick start

Everyone: do §3 (access) → §5 (branch + folder) → §6 (scaffold → attach → stub → real data). Then:

- **Josh — Product (M1-2).** Two GET routes, read-only against Products. Highest-traffic service. Use the §6a template with `<SERVICE>`=`product`, `<TABLE>`=`products`. You'll also own load generation later, so clean structure here pays off.
- **Linu — Cart (M1-3).** Three routes including a `DELETE`, read/write against Carts. Fully independent (Cart calls no one). Cover edge cases (empty cart, item not found) with proper error envelopes.
- **Melisa — Order (M1-4).** Two routes, the most connected service — it writes Orders **and** calls Payment. You need Rachel's Payment stub to exist to finish your end-to-end test. Populate the `dependency_*` fields from the Payment call; those become the raw signal for dependency-failure detection later.
- **Rachel — Payment (M1-5).** One route (no table) **plus** the fault-injection knobs (`PAYMENT_LATENCY_MS`, `PAYMENT_FAILURE_RATE`, `PAYMENT_TIMEOUT`) that the incident simulator drives in M3. Ship a hardcoded-success stub in the first few hours to unblock Melisa, then build the configurable version.

---

## 9. Troubleshooting cheat sheet

| Symptom | Cause | Fix |
|---|---|---|
| `InvalidClientTokenId` / `security token is invalid` | SSO session expired | `aws sso login --profile <your-profile>` |
| `Unable to locate credentials` | No profile set in this terminal | Add `--profile <your-profile>` to the command, or `export AWS_PROFILE=<your-profile>` |
| `Template file not found` | Running `sam` from the wrong folder | `cd` into the folder with `template.yaml`, or `sam build -t <PATH>/template.yaml` |
| Deploy fails with a profile-not-found | `samconfig.toml` references a profile you deleted/renamed | Edit `samconfig.toml` or re-run `sam deploy --guided` |
| Your new route still returns 501 | API stage needs a redeploy | Run the `create-deployment` command in §6e |
| `AccessDeniedException` on DynamoDB | Your code touched a table your role isn't scoped to | Use your own `TABLE_NAME`; least-privilege is intentional (§6d) |
| Deploy stuck / half-created after a failure | "Disable rollback" was set to yes | Delete the stack in the CloudFormation console, redeploy with rollback enabled (answer `N`) |
| A `Type`/`Serverless`/typo error at build | YAML typo | Run `sam validate` — it points at the line |

**Golden rule:** if a command touching AWS fails, run `aws sts get-caller-identity --profile <your-profile>` first. If *that* fails, it's credentials (§3), not your code.

---

## 10. Your M1 checklist

- [ ] `aws configure sso` done; `aws sts get-caller-identity --profile <your-profile>` returns your SSO identity (§3)
- [ ] M1-1 merged to `main`; pulled `main`, created your ticket branch `<TICKET>-<NAME>` (e.g. `m1-2-josh`)
- [ ] Service folder created under `AnomalyPulse/infra/<SERVICE>/` + `template.yaml` scaffolded (§5–§6)
- [ ] Your routes attach to the shared API via `RestApiId: !Ref SharedApiId` (FIXED — copied exactly)
- [ ] Deployed as your **own** stack (`anomalypulse-<SERVICE>`); your routes return 200 stub data, other routes still 501
- [ ] Stub swapped for real DynamoDB using your role + table name (§6d) — Payment: real payment logic instead
- [ ] Following the response-envelope and naming conventions (§7)
- [ ] (Rachel) hardcoded-success Payment stub shipped early; Melisa confirmed unblocked
- [ ] Structured log line emitted per request in the frozen schema shape (§7)

---

*Questions → ping Jake. If it's an `InvalidClientTokenId`, try §3 first — that one's almost always just an expired login.*
