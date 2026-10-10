# CommerceOps Desk v0.2.1 evaluator release design

Status: proposed for written-design approval

Target release: `v0.2.1`

Target demo: `https://commerce-ops-desk.srrsh.aig.rest`

## 1. Context and customer-trust problem

CommerceOps Desk already has strong engineering evidence: a complete synthetic
Manager/Agent workflow, signed webhook behavior, PostgreSQL 17 verification,
browser tests, a hardened container, and a reviewable workstation deployment
contract. A prospective client still has to trust screenshots, a video, or a
local setup guide because the repository has no live product entry point.

The next release will close that verification gap. A visitor with the
enterprise access code should be able to open one stable HTTPS URL, follow a
short script, and verify the same full-stack workflow represented by the source
and CI. The demo remains a synthetic portfolio environment, not a production
commerce service.

Success means that a new evaluator can:

1. identify the exact release and full source commit running in the demo;
2. complete the guided Manager-to-Agent journey without local setup;
3. understand what is synthetic and what is deliberately out of scope;
4. return to the tagged source, public CI evidence, or public walkthrough; and
5. reach those entry points from the repository and GitHub profile without
   finding a dead or premature link.

## 2. Chosen approach

Publish one access-code-protected evaluator deployment from an immutable,
green commit on `main`. Add a compact five-step evaluation guide and
server-reported build identity to the existing application, then publish the
URL only after desktop and mobile acceptance succeeds.

This approach reuses the product's real API, database, authorization, webhook,
and audit paths. It does not create a scripted mock, a separate showcase
frontend, or a second deployment mechanism. The existing verified-image,
isolated-candidate, blue/green route-switch, immutable transaction-ledger, and
current-head rollback controls remain the only workstation deployment path.

### Rejected alternatives

- **Add Stripe, Shopify, a queue, or more infrastructure first.** These would
  expand technical scope without fixing the immediate inability to evaluate
  the product.
- **Publish only another video or screenshot.** These are useful fallbacks but
  do not let a client independently exercise state, permissions, errors, and
  audit history.
- **Expose an unrestricted public sandbox.** The current workstation uses the
  enterprise access-code gateway. Removing that control would create a wider
  abuse and availability obligation without increasing the quality of the
  engineering proof.
- **Build a separate marketing demo.** A second implementation could drift from
  the tested repository and weaken, rather than strengthen, the provenance
  claim.
- **Publish links before acceptance.** A visible link to an unverified or
  unavailable deployment is a larger trust failure than having no link.

## 3. Release and branch sequence

The sequence is intentionally fail-closed. No public URL or release claim is
updated before the deployment has passed external acceptance.

1. Approve this written design.
2. Reconfirm that PR #9 still points to
   `215fb4088fa3db7a66d812507fb49a4190f08a07`, is mergeable, and has all required
   GitHub checks green. Before merging, remove or qualify any PR-description
   claim of `independent` review that is not backed by a public GitHub review;
   internal read-only audits are not represented as third-party approval. Merge
   it without adding unrelated changes, then wait for the resulting `main`
   checks to pass.
3. Fetch and verify the merged remote `main`, create a fresh
   `feat/evaluator-release-implementation` branch from that ref, and cherry-pick
   only this approved design commit. Do not rebase or replay PR #9's commits;
   this stays correct whether GitHub used a merge, squash, or rebase strategy.
   Implement the evaluator UI and build-identity contract test-first in a
   focused pull request. Do not combine profile or repository-link publication
   with this application change.
4. Merge the evaluator pull request only after repository verification,
   PostgreSQL 17, CodeQL, browser, container, and public-history checks pass.
   Fetch the resulting remote `main`, require `refs/remotes/origin/main` and
   GitHub's `refs/heads/main` to equal the same full commit, and record it as
   `DEPLOY_SHA`. Because a merge or squash creates a new commit, wait again for
   the `push`-to-`main` Verify and CodeQL runs whose `head_sha` equals
   `DEPLOY_SHA`; require them green and record their immutable run URLs.
5. Build and deploy only `DEPLOY_SHA` through the versioned workstation tools.
   Keep the previous route, container, and data directory unchanged during the
   acceptance window.
6. Run local candidate checks, switch the single CommerceOps route, and run
   clean external desktop and mobile deployment acceptance. If any deployment
   criterion fails, follow the exact failure or reconciliation branch in this
   design and publish no link.
7. After acceptance, tag exactly `DEPLOY_SHA` as the non-prerelease `v0.2.1`
   release. Release notes identify the deployed SHA, access-controlled demo,
   exact-SHA GitHub Actions evidence, synthetic-data boundary, and known
   exclusions. Verify the remote tag, published release target, live footer,
   image identity, and CI run SHAs as a separate provenance gate. The existing
   no-access-code v0.2.0 video remains the fallback.
8. Only then update the CommerceOps README, repository homepage, profile README,
   and GitHub profile Website field. These publication-only commits may be newer
   than `DEPLOY_SHA`; they must describe the deployed tag and SHA accurately.

Before steps 7 or 8, the workstation administrator must explicitly confirm
that the enterprise access code may be shared with external evaluators and
that its authorization scope does not unintentionally expose unrelated sites.
The confirmation must identify who owns revocation/rotation and how an access
grant is withdrawn. An observed gateway challenge is not a substitute for this
scope confirmation. Without it, the deployed route may remain under private
operator evaluation, but no access code or live-demo CTA is shared; public
source, CI, release evidence, and video remain the only published paths.

If any GitHub merge, tag, release, metadata, or profile update fails, stop at
the last verified state and report the mismatch. Never claim that a local
commit was published until its remote ref resolves to the same SHA.

## 4. Evaluator experience

### Entry screen

The existing no-registration entry remains. `Enter as Manager` is the primary
evaluation path; `Enter as Agent` remains available as a secondary role-boundary
check. The current four-item I03 card is replaced with a release-accurate
five-step guide:

1. **Create an event** — enter as Manager, open the synthetic provider panel,
   and deliver a fresh `payment.failed` event.
2. **Test the boundary** — replay the exact event, then tamper with one signed
   byte and observe idempotent success versus authentication rejection.
3. **Assign the case** — open the generated case and assign it to Demo Agent.
4. **Work as Agent** — switch role, add a fictional internal note, and resolve
   with an allowed reason.
5. **Verify the trail** — inspect safe provenance and the ordered audit history.

The guide describes this as a short five-step journey, uses fictional data,
needs no account, and cannot affect a store or payment. It makes no unmeasured
completion-time promise. It is an instructional guide, not analytics and not a
claim that the browser can infer every completed step.

The entry screen and internal-note control both state: `Use fictional text
only. Do not enter personal, customer, credential, or confidential data.` The
note helper is programmatically associated with its textarea through
`aria-describedby`; the warning remains visible and readable on mobile.

### In-workspace guidance

A compact, accessible guide remains available inside the workspace so the
instructions are not lost after entry. It may collapse on narrow screens but
must be reachable by keyboard and screen reader. It does not persist tracking
state, create another server write, or bypass the existing workflow.

The guide points to existing controls by their visible labels. It must not
automatically send, replay, tamper with, assign, note, resolve, or reset any
case. The evaluator remains in control of every material action.

### Build identity and evidence links

The footer displays:

- `v0.2.1`;
- the complete lowercase 40-character source SHA; and
- a link to that exact commit in the public GitHub repository.

The version and SHA come from the running server, not a client-authored query
parameter or mutable page text. Failure to load build metadata must not block
the operational UI, but the workstation release cannot pass acceptance while
the identity is absent, abbreviated, malformed, or different from
`DEPLOY_SHA`.

The existing source and engineering-case-study links remain. A public
walkthrough link is presented as a fallback and explicitly labelled `v0.2.0`
until a matching new video exists; it is not represented as footage of the
v0.2.1 deployment.

## 5. Build-identity contract

One backend version constant is the source of the FastAPI version and the
public build response. `Settings` gains an optional source revision whose only
accepted non-null form is a full lowercase Git SHA. Local source runs may
identify themselves as an unverified local build, but the evaluator container
must expose a valid revision.

The Docker build uses the existing `SOURCE_SHA` argument for both the OCI
revision label and the runtime `COMMERCE_OPS_SOURCE_SHA` value. The verified
builder already binds `SOURCE_SHA` to the approved remote ref and confirms the
immutable image label; tests will extend that contract so the image runtime
value cannot silently disagree.

An unauthenticated, read-only JSON endpoint returns only the fixed service
identifier, semantic version, and source SHA. It returns no environment dump,
filesystem path, image ID, route ledger, secret, integration identifier, or
user/session data. The response carries `Cache-Control: no-store`, and the
frontend requests it with `cache: "no-store"`, so a route switch or rollback
cannot leave stale provenance in a browser or intermediary cache. The frontend
validates the response shape before rendering it and constructs the commit link
from a fixed repository origin.

The exact endpoint name and response schema will be fixed in the implementation
plan and covered by backend, frontend, container, and workstation acceptance
tests. Hosted evaluator acceptance requires:

```text
version == "0.2.1"
source_sha == DEPLOY_SHA
```

## 6. Access-code and synthetic-data boundaries

The enterprise gateway, not CommerceOps, owns the access-code challenge. The
application will not add an access-code field, cookie, environment variable,
API route, or repository setting. The code must never appear in Git, GitHub
metadata, application configuration, shell arguments, shell history, logs,
screenshots, test artifacts, release notes, or this design.

Public copy will say `Access-controlled live demo`; it will not claim an open
public sandbox. The access code is shared separately through the freelance
platform or other private conversation in which the evaluator was invited. A
visitor without a code is always offered the public release, tagged source,
exact-SHA CI evidence, and v0.2.0 video instead of being sent only to a gate.

All application-provided identities, orders, webhook events, and outcomes are
synthetic. Internal notes are bounded free text, so the application cannot
guarantee that a visitor will not paste real information. The entry and note
field therefore make the evaluator responsible for using fictional text and
prohibit personal, customer, credential, or confidential data; public copy
does not claim technical content detection. The deployment uses a fresh
candidate data directory and never copies the previous live SQLite database.
Workspace expiry, reset scope, capacity controls, role permissions, note
limits, and webhook limits remain unchanged. No real merchant account,
provider credential, customer record, or payment action is introduced by the
repository or controlled acceptance flow.

## 7. Verified deployment and rollback flow

The authoritative operational procedure remains
`deploy/workstation/RUNBOOK.md`; implementation must update it only where the
new build-identity acceptance check requires precision. The deployment uses
only these scoped locations:

- `~/apps/commerce-ops-desk/code`
- `~/apps/commerce-ops-desk/deploy-state`
- a new `~/apps/commerce-ops-desk/data-candidate-<12-char-sha>` directory
- `/etc/caddy/sites/commerce-ops-desk.conf`
- the root-owned CommerceOps transaction directory and active head

The deployment stages are:

1. Fetch `origin/main` in the canonical checkout and require it to equal the
   approved full `DEPLOY_SHA`.
2. Run `build_verified_image.py` against that approved remote ref. Retain the
   private schema-2 build manifest and require its source, immutable image,
   deployment-asset digests, and Docker daemon identity to validate.
3. Create a fresh mode-`0700`, numeric `10001:10001` candidate data directory.
   Do not reuse or copy live state.
4. Run `deploy.py prepare` with the private build manifest, an unused canonical
   loopback port, the unchanged live data directory, and the fresh candidate
   directory. `prepare` must leave Caddy untouched.
5. Verify candidate health, readiness, version, full SHA, and candidate logs
   through loopback with the required Host header. Do not weaken Secure cookies,
   alter Caddy, or start a second publication path to run the browser journey
   before the switch; the already-tested core journey is exercised externally
   after the verified route change.
6. Run `deploy.py switch --state <candidate-state>`. Preserve the exact flushed
   transaction-ledger path and the printed current-head rollback authority.
7. Verify the installed Caddy route revision, application build identity,
   target container identity, security headers, gateway boundary, and external
   workflow before publishing any link.

Deployment outcomes follow four branches; the operator does not infer where an
interrupted process happened:

1. **Normal success:** proceed to external deployment acceptance.
2. **Normal failure with proven automatic restoration:** stop publication and
   verify that the old route and old upstream remain active.
3. **Indeterminate result after the ledger path was flushed:** after SIGKILL,
   host restart, power loss, sudo timeout, unknown commit outcome, or any other
   ambiguous return, run `deploy.py reconcile --transaction
   <exact-flushed-ledger>`. Do not retry `switch`, invoke `rollback`, or choose a
   different ledger first.
4. **Unrecoverable or externally changed state:** if no exact ledger is
   available, reconciliation refuses, automatic restoration fails, or external
   drift is observed, stop all deployment actions and preserve the site,
   ledger, `active.json`, and relevant non-secret logs for administrator
   inspection.

If deployment acceptance fails after a proven successful switch, invoke
`deploy.py rollback` only with the immutable backup authorized by the current
root-owned head. Then repeat public HTTPS, gateway, route marker, and old
upstream smoke checks before closing the failure path. Never hand-edit Caddy,
the ledger, or `active.json`; never delete the previous container or data during
the acceptance window.

## 8. Public entry-point updates

These changes occur only after deployment acceptance and release publication:

- **CommerceOps README:** place `Open access-controlled live demo`, `Watch the
  public walkthrough`, and `Review source and CI` immediately below the title
  and badges. State the deployed tag/full SHA and place `Live evaluator:
  single-node SQLite; PostgreSQL 17: CI-verified path only` beside those CTAs.
- **CommerceOps repository metadata:** set the homepage to the HTTPS demo URL;
  preserve a concise description that does not claim production readiness.
- **GitHub profile README:** make CommerceOps the first featured proof and put
  the public v0.2.1 release/evidence link first, followed by the clearly marked
  access-controlled demo, video, and engineering links. Repeat the SQLite-live
  versus PostgreSQL-CI boundary beside the links.
- **GitHub profile Website field:** replace the ERP concept URL with the public
  v0.2.1 CommerceOps release page, not the access-code gate. This gives an
  unknown visitor a working evidence page while the protected demo remains the
  next CTA for prospects who received a code privately.

No X handle, email address, testimonial, customer name, uptime claim, star,
review, or external endorsement is invented. The existing GitHub inquiry form
and platform-origin contact language remain until the owner selects a single
public contact identity.

## 9. Test strategy

Implementation follows red-green-refactor. Tests are added before behavior and
must demonstrate the intended failure before the smallest implementation.

At minimum, automated coverage includes:

- backend settings validation for missing, malformed, uppercase, abbreviated,
  and valid source SHAs;
- build endpoint response and absence of unrelated settings or secrets;
- `Cache-Control: no-store` on build metadata and `cache: "no-store"` in the
  frontend request;
- FastAPI version and public version constant agreement;
- Docker image runtime revision and OCI label agreement;
- frontend metadata parsing, full-SHA rendering, fixed commit link, unavailable
  state, keyboard access, and mobile layout;
- exact five-step guide content on entry and inside a workspace;
- fictional-text warnings at entry and beside the note textarea, including the
  `aria-describedby` association and mobile browser coverage;
- unchanged Manager, webhook replay/tamper, assignment, Agent, note,
  resolution, reset, and recovery journeys;
- deployment helper/runtime-contract refusal when runtime source identity does
  not equal the verified image source;
- release-evidence selection that accepts only GitHub Actions runs whose
  `head_sha` equals `DEPLOY_SHA`, never a mutable latest-run link; and
- public-history scanning of every new commit.

The final application pull request must pass `make verify`, the PostgreSQL 17
job, production-container tests, CodeQL, and the complete GitHub checks. Local
success is not substituted for a CI-only PostgreSQL, container, or security
claim.

## 10. External acceptance criteria

### Deployment acceptance

Use a clean browser context. Enter the gateway code interactively without
recording it, then verify both a desktop viewport and a 320-pixel mobile
viewport against the stable HTTPS URL.

Deployment acceptance requires all of the following:

1. An independent unauthorized browser context cannot reach application
   content through the HTTPS gateway.
2. The page shows `v0.2.1` and the exact full `DEPLOY_SHA`; its commit link
   resolves to the approved GitHub commit.
3. A fresh Manager workspace can deliver one synthetic event, replay it without
   a duplicate effect, and observe tamper rejection.
4. The Manager can assign the generated case to Demo Agent; after switching to
   Agent, the evaluator can add a clearly fictional note, resolve the case, and
   see safe provenance plus ordered audit history. The note warning remains
   visible, readable, and programmatically associated at both viewports.
5. Reset affects only the active synthetic tenant, and a clean browser receives
   an independent workspace.
6. The five-step guide, controls, case detail, and footer remain usable at both
   viewports with keyboard navigation and without horizontal page overflow.
7. In a fresh authorized browser context, `/docs`, `/redoc`, and
   `/openapi.json` reach the application and return real `404` responses;
   expected security headers occur once; build metadata is `no-store` and
   exposes only its allowlisted fields. Separately, a no-code, incorrect-Host
   probe at the scoped Caddy boundary is rejected rather than routed to the
   application.
8. Candidate/container health and readiness remain green after a controlled
   restart, and the workspace persists through that restart.
9. The controlled acceptance flow uses only fictional note text. Application,
   Caddy, and container logs contain no access code, session or CSRF value,
   webhook signature, master secret, raw event body, or personal/customer data.
   This verifies the controlled run and does not claim that future free-text
   visitors are technically prevented from violating the displayed rule.

If any post-switch item fails and rollback is required, acceptance is not
closed until a second external smoke confirms public HTTPS, gateway protection,
the restored route marker, and the old upstream identity.

### Post-release provenance gate

After deployment acceptance and release publication, but before updating any
public entry point:

1. the remote `v0.2.1` tag and GitHub release target equal `DEPLOY_SHA`;
2. the live footer and running image identify the same `DEPLOY_SHA`;
3. every linked GitHub Actions run reports `head_sha == DEPLOY_SHA`; and
4. the non-prerelease release page is reachable without an access code and
   accurately labels the protected demo, SQLite runtime, PostgreSQL CI-only
   evidence, public fallback video, and explicit non-goals.

Record only non-secret outcomes, UTC time, tag, and full SHA. Do not capture the
gateway challenge or authenticated cookies in screenshots or artifacts.

## 11. Explicit non-goals

This release does not add or claim:

- Stripe, Shopify, or another real commerce-provider adapter;
- real store, customer, order, refund, fulfillment, or payment access;
- an asynchronous queue, outbox, worker, automatic delivery retry, or DLQ;
- exactly-once processing, multi-node operation, high availability, disaster
  recovery, production observability, production readiness, or an SLA;
- open anonymous public access or independent per-visitor edge rate limiting;
- performance or capacity results;
- a generated SDK, public production OpenAPI contract, or integration guide;
- a new contact identity, customer testimonial, third-party review, social
  following, or other external endorsement; or
- unrelated feature work in the other portfolio repositories.

The release is successful when a prospective client can quickly inspect and
exercise the existing full-stack evidence with accurate provenance and honest
boundaries. It is not a substitute for production discovery, provider-specific
integration work, or an independent security review.
