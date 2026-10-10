# CommerceOps workstation deployment

This runbook deploys one synthetic CommerceOps candidate on the shared workstation. It never modifies another application, `/etc/caddy/Caddyfile`, or another file under `/etc/caddy/sites/`.

## Boundaries

- Application home: `~/apps/commerce-ops-desk`
- Exact canonical code checkout: `~/apps/commerce-ops-desk/code`; it must not
  be a symlink, and every deployment helper is run from this checkout.
- Private deployment state: `~/apps/commerce-ops-desk/deploy-state`; it is a
  sibling of `code/`, never part of the repository.
- Managed site: `/etc/caddy/sites/commerce-ops-desk.conf`
- Fixed advisory lock for cooperating invocations: `/run/lock/commerce-ops-desk-caddy.lock`
- Root-owned backup ledger: `/var/lib/commerce-ops-desk/caddy-transactions/`
- Root-owned linear route head:
  `/var/lib/commerce-ops-desk/caddy-transactions/active.json`
- Runtime UID/GID: numeric `10001:10001`.
- Live and candidate data directories must already exist under the application
  home, outside both `code/` and `deploy-state/`. They must be canonical,
  non-symlink directories owned by numeric `10001:10001` with mode `0700`, and
  neither may contain the other.
- A candidate gets a unique Compose project, container, network, image tag, and
  the exact direct-child `data-candidate-<12-char-sha>` directory derived from
  its full source SHA.
- The tool accepts no trusted-proxy argument. It first creates the isolated Compose network without starting the app, inspects its single IPv4 gateway, and then recreates the candidate trusting only that single `/32`.
- The candidate uses fresh synthetic state. Never copy a live SQLite database while its container is running and never mount one SQLite directory into two application processes.
- The enterprise access code remains outside all commands, repository data,
  application/Caddy/container logs, screenshots, and artifacts. It must also
  never enter Compose, Caddy, shell arguments or history, this runbook, Git
  metadata, test output, HAR exports, or release notes. Do not put the access
  code in any of those locations.

The Caddy template overwrites `X-Forwarded-For` with the source address Caddy actually observes. When the enterprise access-code gateway uses one upstream address, all viewers safely use a shared source bucket. This deployment must not claim independent visitor rate limiting unless the administrator supplies and approves a different authenticated forwarding contract.

## 1. Stage the exact source and image

Stage the candidate checkout at the exact canonical
`~/apps/commerce-ops-desk/code` path. Fetch the approved remote-tracking ref,
review its full lowercase 40-character commit, and build through the verified
entry point:

```bash
APP_ROOT="$HOME/apps/commerce-ops-desk"
CODE_ROOT="$APP_ROOT/code"
cd -- "$CODE_ROOT"
git fetch --prune origin
APPROVED_REMOTE_REF="refs/remotes/origin/main"
SOURCE_SHA="$(git rev-parse "$APPROVED_REMOTE_REF")"
BUILD_MANIFEST="$APP_ROOT/deploy-state/build-${SOURCE_SHA}.json"
/usr/bin/python3 deploy/workstation/build_verified_image.py \
  --repository "$CODE_ROOT" \
  --source-sha "$SOURCE_SHA" \
  --approved-remote-ref "$APPROVED_REMOTE_REF" \
  --build-manifest "$BUILD_MANIFEST"
```

The builder refuses abbreviated or uppercase SHAs and any ref outside
`refs/remotes/`. It clears Git environment overrides, disables replacement
objects, requires the repository argument to be the exact worktree top level,
and requires the selected ref to equal `SOURCE_SHA`. It exports a temporary
context with `git archive "$SOURCE_SHA"` through a temporary bare object view
and a clean Git config and attributes namespace. Mutable
`.git/info/attributes` and global/system attributes cannot apply
`export-ignore` or `export-subst`. Versioned attributes committed in
`SOURCE_SHA` remain approved archive policy. The builder then builds
`commerce-ops-desk:$SOURCE_SHA` and binds the OCI revision label and immutable
image ID in one JSON inspection.
Before building, it hashes the exact approved-archive bytes of
`deploy/workstation/compose.yaml` and both managed Caddy templates. The
schema-2 manifest records that exact path-to-SHA-256 set and the fixed local
Docker daemon ID, in addition to the immutable image identity. The builder
verifies the same daemon again after the build. A later deployment therefore
cannot silently substitute a writable-checkout asset or another Docker daemon.
The resulting strict private manifest is written atomically with mode `0600`.
Dirty or untracked checkout files never enter the context, and temporary content
is removed on success or failure.

This is a local provenance control, not a cryptographic signature, transparency
record, or remote supply-chain attestation. It proves what local Git object was
used as the Docker context; it does not prove who authored or approved that
object. Do not continue unless the printed SHA is the reviewed remote commit.

## 2. Prepare independent state

Choose the current live directory deliberately. Create a fresh candidate directory whose name is derived from the same SHA:

```bash
LIVE_DATA_DIR="$APP_ROOT/data-v0.2.0-live"
CANDIDATE_DATA_DIR="$APP_ROOT/data-candidate-${SOURCE_SHA:0:12}"
mkdir -- "$CANDIDATE_DATA_DIR"
sudo chown 10001:10001 "$CANDIDATE_DATA_DIR"
sudo chmod 0700 "$CANDIDATE_DATA_DIR"
```

`10001:10001` are literal numeric IDs; do not replace them with account names.
The existing live directory must already have the same numeric ownership and
mode, but do not blindly change a live directory. The live directory and
candidate directory must be different. Check both paths before continuing; do
not create either directory through Docker or Compose. A pre-existing
candidate path makes `mkdir` fail closed and must be inspected rather than
reused.

## 3. Prepare and verify the candidate

Select a canonical decimal loopback port. The tool refuses a listener, any Docker binding, or any Caddy reference to that port. It also refuses an existing candidate project/container so an interrupted attempt cannot be mistaken for a fresh deployment.

```bash
/usr/bin/python3 deploy/workstation/deploy.py prepare \
  --build-manifest "$BUILD_MANIFEST" \
  --candidate-port 18088 \
  --live-data-dir "$LIVE_DATA_DIR" \
  --candidate-data-dir "$CANDIDATE_DATA_DIR"
```

`prepare` accepts no standalone source SHA. It requires the private build
manifest, confirms the current image tag still resolves to that manifest's
immutable ID, inspects the image configuration by immutable ID, and verifies
the created container against that baseline. It then performs the two-stage
network creation, derives the single /32, waits for Docker health, and records a
private candidate state file under `~/apps/commerce-ops-desk/deploy-state/`. It
does not change Caddy.

During `prepare` and `switch`, the helper accepts only the exact three-path
deployment asset set recorded in the manifest or state. It opens each canonical
regular file once, bounds its size, verifies its SHA-256 digest and UTF-8
encoding, and retains the verified bytes in memory. `prepare` checks that the
verified Compose document contains exactly the single `commerce-ops-desk`
service, then supplies those in-memory bytes with `--file -`; both create and
up name that service explicitly with `--no-deps`. It never reopens the worktree
Compose file during the operation. The manifest source, immutable image,
deployment assets, and current fixed local Docker daemon ID must all match.

Before switching, inspect only this candidate's logs and exercise its loopback endpoint with the required public Host:

```bash
curl --fail --silent --show-error \
  --header 'Host: commerce-ops-desk.srrsh.aig.rest' \
  http://127.0.0.1:18088/ready
```

Require the candidate's exact build identity from
`http://127.0.0.1:18088/api/build` before switching. This local probe requires
the fixed service, version `0.2.1`, the full `$SOURCE_SHA`, and the
`Cache-Control` header value exactly `no-store`; it does not print the response
or add credentials:

```bash
SOURCE_SHA="$SOURCE_SHA" /usr/bin/python3 - <<'PY'
import http.client
import json
import os

source_sha = os.environ["SOURCE_SHA"]
connection = http.client.HTTPConnection("127.0.0.1", 18088, timeout=10)
try:
    connection.request(
        "GET",
        "/api/build",
        headers={"Host": "commerce-ops-desk.srrsh.aig.rest"},
    )
    response = connection.getresponse()
    body = response.read()
    if response.status != 200:
        raise SystemExit("candidate build identity did not return HTTP 200")
    expected = {
        "service": "commerce-ops-desk",
        "version": "0.2.1",
        "source_sha": source_sha,
    }
    if json.loads(body) != expected:
        raise SystemExit("candidate build identity does not match SOURCE_SHA")
    if response.headers.get_all("Cache-Control", []) != ["no-store"]:
        raise SystemExit("candidate Cache-Control is not exactly no-store")
finally:
    connection.close()
PY
```

Do not weaken Secure cookies, alter Caddy, or create a second publication path
to run the browser journey before `switch`. The full journey runs through the
existing HTTPS route only after the verified route change.

If preparation fails after resource creation, inspect the exact derived
container and network first. Do not run Compose against
`deploy/workstation/compose.yaml`: a mutable worktree file is not a trusted
cleanup input. The following Bash recipe uses the same fixed local Docker
boundary, requires exactly one project-labelled container and network, and
checks their exact name and Compose labels before removing them:

```bash
(
set -euo pipefail
PROJECT_NAME="commerce-ops-candidate-${SOURCE_SHA:0:12}"
CONTAINER_NAME="app-commerce-ops-desk-candidate-${SOURCE_SHA:0:12}"
NETWORK_NAME="${PROJECT_NAME}_default"
DOCKER=(
  /usr/bin/env -i
  PATH=/usr/bin:/bin
  DOCKER_HOST=unix:///var/run/docker.sock
  DOCKER_CONFIG=/etc/docker
  /usr/bin/docker
)

CONTAINER_IDS="$("${DOCKER[@]}" ps --all --quiet \
  --filter "label=com.docker.compose.project=$PROJECT_NAME")"
test -n "$CONTAINER_IDS"
test "$(printf '%s\n' "$CONTAINER_IDS" | /usr/bin/wc -l)" -eq 1
CONTAINER_ID="$CONTAINER_IDS"
test "$("${DOCKER[@]}" inspect --format '{{.Name}}' "$CONTAINER_ID")" = \
  "/$CONTAINER_NAME"
test "$("${DOCKER[@]}" inspect --format \
  '{{index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER_ID")" = \
  "$PROJECT_NAME"
test "$("${DOCKER[@]}" inspect --format \
  '{{index .Config.Labels "com.docker.compose.service"}}' "$CONTAINER_ID")" = \
  "commerce-ops-desk"

NETWORK_IDS="$("${DOCKER[@]}" network ls --quiet \
  --filter "label=com.docker.compose.project=$PROJECT_NAME")"
test -n "$NETWORK_IDS"
test "$(printf '%s\n' "$NETWORK_IDS" | /usr/bin/wc -l)" -eq 1
NETWORK_ID="$NETWORK_IDS"
test "$("${DOCKER[@]}" network inspect --format '{{.Name}}' "$NETWORK_ID")" = \
  "$NETWORK_NAME"
test "$("${DOCKER[@]}" network inspect --format \
  '{{index .Labels "com.docker.compose.project"}}' "$NETWORK_ID")" = \
  "$PROJECT_NAME"
test "$("${DOCKER[@]}" network inspect --format \
  '{{index .Labels "com.docker.compose.network"}}' "$NETWORK_ID")" = default

"${DOCKER[@]}" container rm --force "$CONTAINER_ID"
"${DOCKER[@]}" network rm "$NETWORK_ID"
)
```

The candidate data directory is intentionally retained.

## 4. Switch only the CommerceOps site

### Frozen legacy compatibility evidence

The initial hardened cutover has one deliberately narrow migration input. A
read-only workstation check on 2026-10-09 reported Caddy 2.6.2 and this complete
live site fragment:

```caddyfile
http://commerce-ops-desk.srrsh.aig.rest {
	reverse_proxy 127.0.0.1:18087
}
```

Rendering
`deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template` at port `18087`
produces that exact byte sequence, SHA-256
`740ab123464b07d8e6460c974abc901c4025fc08f34a955402ba5db574994e3a`.
Caddy 2.6.2 adaptation produced one `:80` server, one route for only
`commerce-ops-desk.srrsh.aig.rest`, and one reverse-proxy upstream at
`127.0.0.1:18087`. The check did not modify the workstation. This frozen
profile is accepted only as an existing site, backup, automatic-restoration
target, or trusted rollback target. It can never be installed by `switch` as a
new candidate. The first bootstrap requires the exact recorded fragment bytes,
port, adapted active route, readiness behavior without a revision header, and
the frozen live container/network/data/runtime fingerprint. A
whitespace-equivalent fragment, an extra site, address, directive, different
upstream, or different runtime identity is not a bootstrap input.

Use the state path printed by `prepare`:

```bash
STATE_FILE="$APP_ROOT/deploy-state/candidate-${SOURCE_SHA:0:12}.json"
/usr/bin/python3 deploy/workstation/deploy.py switch --state "$STATE_FILE"
```

`switch` holds the fixed root-owned advisory lock from candidate revalidation
through publication of the final route head. The lock serializes cooperating
invocations of this deployment helper; it does not exclude a root writer or
another privileged tool that ignores the lock. The helper requires exactly one
Caddy port placeholder, renders the hardened canonical profile, and compares
Caddy 2.6.2's adapted JSON with the frozen single-site templates. Extra
addresses, sites, directives, or a different upstream are refused.

The human deployment operator is part of the trusted computing base (TCB).
Every privileged Python invocation uses isolated mode and a fixed minimal
environment, but the embedded privileged code still comes from the operator's
checkout. This runbook therefore does not claim protection from a malicious
operator who can edit the helper or invoke equivalent root commands. Such a
boundary would require a separately installed, root-owned helper plus a
restricted sudoers policy.

Before installation it creates an immutable schema-3 transaction: a root-owned
backup plus canonical JSON binding the bootstrap ID, exact parent head, site,
backup `RouteState`, and installed `RouteState`. Files are published with a
no-clobber hard-link step and explicitly fsynced before use, so an existing
transaction ID is never overwritten and a published head does not depend on an
unflushed ledger. Before any site replacement, the command verifies that the
target container is healthy and still matches its complete recorded upstream
identity, then flushes the exact reconcile-ledger path to stdout.
The verified fragment bytes and their SHA-256 are passed directly to isolated
Python under `sudo`; no separate deploy-user-writable fragment file is reopened
during replacement. The tool then validates the complete Caddy configuration,
reloads Caddy, compares the active managed route, verifies the response route
revision, and revalidates the full Docker upstream identity. Only after all of
those checks does it atomically replace `active.json` with the schema-1 head.

Under that lock, `switch` revalidates the candidate state's exact deployment
asset digest set, reads each asset once, and uses the verified hardened-template
bytes retained in memory for rendering and structural validation. It does not
reopen the template path during the switch.

If validation, reload, active-route comparison, revision probe, upstream
revalidation, or pre-publication head commit fails, automatic restoration first
rereads the managed fragment. Restoration repeats the complete validation,
reload, active-route, marker, and upstream checks for the old route. An
unexpected fragment or head is treated as external drift and restoration is
refused. If a commit command reports failure but the exact proposed head is
already readable, the operation is treated as committed and is not restored.

The site replacement and `active.json` replacement are two separate filesystem
operations. A process killed between them can leave the site ahead of the
recorded head; this cannot be made cross-file atomic by this helper. Use the
reconciliation procedure below with the exact ledger path that was printed and
flushed before the site write. An immutable transaction may also remain
orphaned after a failed attempt. Such a ledger is retained for diagnosis but is
not rollback-authorized unless `active.json` names it. Privileged writers must
coordinate on the advisory lock; the helper does not claim to exclude a root
writer that ignores it.

Do not send the URL or access code to a prospect until the administrator confirms that the access-code gate is appropriate for external viewers and does not expose unrelated sites.

### Interpret every switch result using exactly four branches

1. **Normal success:** proceed to external acceptance.
2. **Normal failure with proven automatic restoration:** stop publication and
   verify that the old route and old upstream remain active.
3. **Indeterminate result after the ledger path was flushed:** after SIGKILL,
   host restart, power loss, sudo timeout, an unknown commit outcome, or any
   other ambiguous return, use `reconcile` with the exact flushed transaction
   path. Do not retry `switch`, invoke `rollback`, or choose a different ledger
   first.
4. **Unrecoverable or externally changed state:** if the exact ledger is not
   available, reconciliation refuses, automatic restoration fails, or external
   drift is present, stop all deployment actions. Preserve the site, ledger,
   `active.json`, and relevant non-secret logs for administrator inspection.

If external acceptance fails after a proven successful switch, run rollback
only with the immutable backup authorized by the current root-owned
`active.json` head. Never hand-edit Caddy, the ledger, or `active.json`, and do
not delete the previous container or data during the acceptance window.

## 5. Reconcile an interrupted route transaction

Use this only when `switch` or `rollback` was terminated without returning a
normal result, such as by SIGKILL, host restart, or power loss. Copy the exact
ledger path from the flushed `Caddy transaction prepared` line; do not choose a
different orphan or edit the ledger:

```bash
/usr/bin/python3 deploy/workstation/deploy.py reconcile \
  --transaction "/var/lib/commerce-ops-desk/caddy-transactions/<printed-transaction>.json"
```

`reconcile` takes the same lock and accepts only a canonical, root-owned
transaction whose parent is exactly the current head, including the initial
no-head bootstrap case. If the site contains the transaction's installed
route, it verifies target health and identity, validates and reloads Caddy,
checks the active route and response marker, revalidates the upstream, and only
then advances the head. If that orphan route cannot be verified while the head
and site still match the interrupted transaction, reconcile preflights the
trusted backup, restores it, repeats all route checks, and leaves the parent
head unchanged. If the site already contains the transaction backup, it
performs the same verification for that route and also leaves the parent head
unchanged. Re-running it for the exact already-committed head is idempotent and
still repeats all route checks; it does not auto-restore behind a committed
head. Any other site, head, transaction, or unrecoverable upstream state is
refused; stop and preserve the files for administrator inspection.

## 6. External acceptance

Use a clean browser session so the access code is entered only into the gateway
UI and is never placed in a command. Do not record the gateway challenge,
authenticated cookies, or access code in screenshots, HAR files, logs, or test
artifacts.

Repeat the build-identity check after the route switch through the stable HTTPS
route. In the authorized browser, open `/api/build` and inspect its
Network response without exporting it. Require HTTP 200, exactly the three
allowlisted JSON fields, service `commerce-ops-desk`, version `0.2.1`, the full
`SOURCE_SHA`, and exactly `Cache-Control: no-store`. A mismatch stops
publication and follows the proven-success rollback branch above.

Verify HTTPS, Host rejection, docs `404`, security headers, Manager/Agent
workflow, signed webhook cases, mobile layout, restart persistence, and the
absence of secrets in logs. Keep the old container and data directory unchanged
during the acceptance window.

## 7. Roll back

The successful `switch` prints the exact root-owned backup path. Copy that path verbatim; do not copy, rename, edit, or recreate either the backup or its adjacent JSON ledger:

```bash
/usr/bin/python3 deploy/workstation/deploy.py rollback \
  --backup "/var/lib/commerce-ops-desk/caddy-transactions/<printed-commerce-ops-backup>.conf"
```

Rollback uses the same advisory lock and therefore serializes only cooperating
invocations. It accepts only the backup named by the current `active.json`
head, after verifying the active bytes, ledger digest, canonical schema, exact
transaction paths, backup digest, both route states, and root ownership. It
accepts only the frozen legacy v0.2.0 or hardened canonical structure. Rollback
creates a new schema-3 transaction and advances `active.json`; it never moves
the head backward. Consequently, after rolling back T1 through a new T2, T1's
backup cannot be replayed. The command validates the complete Caddy
configuration, reloads, compares the active route, verifies the response marker
contract, and revalidates the restored upstream. Before changing Caddy, it also
requires the target container to be healthy and to match the recorded complete
upstream identity, so a stopped container's released loopback port cannot be
substituted by another local listener. It does not stop or delete either
container and never writes another Caddy site.

If automatic restoration itself fails, stop all deployment activity and give
the administrator the printed trusted backup path plus the exact error. If the
helper observed a digest outside this transaction, preserve and inspect the
current fragment before any manual action. The root-owned ledger and linear
head protect against unprivileged application processes, accidental
writable-file replacement, stale helper operations, and replay through the
supported command flow. They are not a signature, a cross-file atomic
transaction, or a defense against the trusted deployment operator or a
compromised or uncoordinated root writer.
