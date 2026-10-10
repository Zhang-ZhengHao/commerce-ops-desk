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
- The live data directory must already exist under the application home,
  outside both `code/` and `deploy-state/`, as a canonical non-symlink directory
  owned by numeric `10001:10001` with mode `0700`. The candidate path must not
  exist; `prepare` atomically creates its exact direct-child directory with the
  same ownership/mode, binds its inode, and ensures neither data directory can
  contain the other.
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

The release decision and the workstation installation are two different trust
contexts. The operator supplies one explicitly approved lowercase full
`DEPLOY_SHA`; neither context derives approval from whichever branch happens to
be current.

### Trusted release-controller context

Run this block only in the trusted sandbox/release controller checkout where
`origin` is the platform repository and `github` is the public GitHub mirror.
It proves both published `main` refs agree, checks out that exact commit in a
detached and completely clean tree, and runs the selector from those committed
bytes. GitHub authentication is ambient to `gh`; a token and the enterprise
access code must never be placed in argv, shell tracing, logs, or the JSON
files.

```bash
set -euo pipefail
umask 077
: "${DEPLOY_SHA:?export DEPLOY_SHA as the explicitly approved full commit}"
: "${CONTROLLER_CODE_ROOT:?set the trusted release-controller checkout}"
: "${VERIFY_WORKFLOW_DATABASE_ID:?copy the Verify workflowDatabaseId from the trusted record}"
: "${CODEQL_WORKFLOW_DATABASE_ID:?copy the CodeQL workflowDatabaseId from the trusted record}"
[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "DEPLOY_SHA must be one full lowercase 40-character Git SHA" >&2
  exit 1
}
GITHUB_REPOSITORY="Zhang-ZhengHao/commerce-ops-desk"
cd -- "$CONTROLLER_CODE_ROOT"
git fetch --prune origin main
git fetch --prune github main
APPROVED_REMOTE_REF="refs/remotes/origin/main"
GITHUB_REMOTE_REF="refs/remotes/github/main"
ORIGIN_MAIN_SHA="$(git rev-parse --verify "${APPROVED_REMOTE_REF}^{commit}")"
GITHUB_MAIN_SHA="$(git rev-parse --verify "${GITHUB_REMOTE_REF}^{commit}")"
test "$ORIGIN_MAIN_SHA" = "$DEPLOY_SHA"
test "$GITHUB_MAIN_SHA" = "$DEPLOY_SHA"
git checkout --detach "$DEPLOY_SHA"
test "$(git rev-parse --verify HEAD)" = "$DEPLOY_SHA"
if git symbolic-ref --quiet HEAD >/dev/null; then
  echo "release-controller checkout must be detached" >&2
  exit 1
fi
test -z "$(git status --porcelain=v1 --untracked-files=all)"
test "$(git hash-object scripts/select_release_evidence.py)" = \
  "$(git rev-parse "${DEPLOY_SHA}:scripts/select_release_evidence.py")"

PRIVATE_TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/commerce-ops-release.XXXXXXXX")"
GITHUB_RUNS_TMP="$(mktemp "$PRIVATE_TMP_ROOT/github-runs.XXXXXXXX.json")"
RELEASE_EVIDENCE_TMP="$(mktemp "$PRIVATE_TMP_ROOT/release-evidence.XXXXXXXX.json")"
trap 'rm -rf -- "$PRIVATE_TMP_ROOT"' EXIT
chmod 0700 "$PRIVATE_TMP_ROOT"
chmod 0600 "$GITHUB_RUNS_TMP" "$RELEASE_EVIDENCE_TMP"
gh run list --repo "$GITHUB_REPOSITORY" --commit "$DEPLOY_SHA" --limit 1000 --json attempt,conclusion,databaseId,event,headBranch,headSha,status,url,workflowName,workflowDatabaseId > "$GITHUB_RUNS_TMP"
/usr/bin/python3 -I scripts/select_release_evidence.py \
  --repository Zhang-ZhengHao/commerce-ops-desk \
  --deploy-sha "$DEPLOY_SHA" \
  --require-workflow "Verify=${VERIFY_WORKFLOW_DATABASE_ID}@push" \
  --require-workflow "CodeQL=${CODEQL_WORKFLOW_DATABASE_ID}@dynamic" \
  --input "$GITHUB_RUNS_TMP" > "$RELEASE_EVIDENCE_TMP"
cat -- "$RELEASE_EVIDENCE_TMP"
```

The selector output records each exact run attempt and its immutable
`/attempts/<attempt>` URL. Preserve that non-secret result in the release
record. A missing, duplicate, wrong-event, wrong-branch, wrong-SHA, unsuccessful,
or non-canonical run fails closed. The controller requests 1,000 records while
the selector has a 999-run acceptance ceiling. Because filtered workflow-run
searches have a 1,000-result GitHub API cap, a full 1,000-record response is a
truncation sentinel and fails closed instead of making a uniqueness claim from
an incomplete list.

### Enterprise workstation context

The workstation uses only the public GitHub repository. It does not trust the
existing `code` path to be a Git checkout: first create a new staging clone,
verify its exact detached clean identity, recoverably quarantine whatever old
path exists, and only then place the verified clone at the canonical path.

```bash
set -euo pipefail
umask 077
: "${DEPLOY_SHA:?export the exact SHA approved by the release controller}"
[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "DEPLOY_SHA must be one full lowercase 40-character Git SHA" >&2
  exit 1
}
GITHUB_REPOSITORY_URL="https://github.com/Zhang-ZhengHao/commerce-ops-desk.git"
APP_ROOT="$HOME/apps/commerce-ops-desk"
CODE_ROOT="$APP_ROOT/code"
DEPLOY_STATE="$APP_ROOT/deploy-state"
test -d "$APP_ROOT"
test ! -L "$APP_ROOT"
test "$(realpath -e -- "$APP_ROOT")" = "$APP_ROOT"
test "$(/usr/bin/stat --format=%u -- "$APP_ROOT")" = "$(/usr/bin/id -u)"
test "$(/usr/bin/stat --format=%g -- "$APP_ROOT")" = "$(/usr/bin/id -g)"
test "$(/usr/bin/stat --format=%a -- "$APP_ROOT")" = "700"
if [[ ! -e "$DEPLOY_STATE" && ! -L "$DEPLOY_STATE" ]]; then
  mkdir -m 0700 -- "$DEPLOY_STATE"
fi
test -d "$DEPLOY_STATE"
test ! -L "$DEPLOY_STATE"
test "$(realpath -e -- "$DEPLOY_STATE")" = "$DEPLOY_STATE"
test "$(/usr/bin/stat --format=%u -- "$DEPLOY_STATE")" = "$(/usr/bin/id -u)"
test "$(/usr/bin/stat --format=%a -- "$DEPLOY_STATE")" = "700"
test "$(/usr/bin/stat --format=%d -- "$APP_ROOT")" = \
  "$(/usr/bin/stat --format=%d -- "$DEPLOY_STATE")"

STAGING_ROOT="$(mktemp -d -p "$APP_ROOT" ".code-staging-${DEPLOY_SHA:0:12}.XXXXXXXX")"
chmod 0700 "$STAGING_ROOT"
STAGING_CODE="$STAGING_ROOT/repository"
git clone --no-checkout "$GITHUB_REPOSITORY_URL" "$STAGING_CODE"
git -C "$STAGING_CODE" fetch --prune origin main
WORKSTATION_ORIGIN_MAIN_SHA="$(git -C "$STAGING_CODE" rev-parse --verify refs/remotes/origin/main^{commit})"
test "$WORKSTATION_ORIGIN_MAIN_SHA" = "$DEPLOY_SHA"
git -C "$STAGING_CODE" checkout --detach "$DEPLOY_SHA"
WORKSTATION_HEAD_SHA="$(git -C "$STAGING_CODE" rev-parse --verify HEAD)"
test "$WORKSTATION_HEAD_SHA" = "$DEPLOY_SHA"
if git -C "$STAGING_CODE" symbolic-ref --quiet HEAD >/dev/null; then
  echo "workstation staging checkout must be detached" >&2
  exit 1
fi
test -z "$(git -C "$STAGING_CODE" status --porcelain=v1 --untracked-files=all)"
for relative_tool in \
  scripts/select_release_evidence.py \
  deploy/workstation/build_verified_image.py \
  deploy/workstation/deploy.py
do
  test -f "$STAGING_CODE/$relative_tool"
  test ! -L "$STAGING_CODE/$relative_tool"
  test "$(git -C "$STAGING_CODE" hash-object -- "$relative_tool")" = \
    "$(git -C "$STAGING_CODE" rev-parse "${DEPLOY_SHA}:${relative_tool}")"
done

if [[ -e "$CODE_ROOT" || -L "$CODE_ROOT" ]]; then
  CODE_QUARANTINE="$(mktemp -d -p "$APP_ROOT" "code-quarantine-${DEPLOY_SHA:0:12}.XXXXXXXX")"
  chmod 0700 "$CODE_QUARANTINE"
  mv -T -- "$CODE_ROOT" "$CODE_QUARANTINE/code"
fi
mv -T -- "$STAGING_CODE" "$CODE_ROOT"
test -d "$CODE_ROOT"
test ! -L "$CODE_ROOT"
test "$(realpath -e -- "$CODE_ROOT")" = "$CODE_ROOT"
cd -- "$CODE_ROOT"

verify_exact_checkout() {
  local head_sha origin_main_sha relative_tool
  head_sha="$(git rev-parse --verify HEAD)"
  origin_main_sha="$(git rev-parse --verify refs/remotes/origin/main^{commit})"
  test "$head_sha" = "$DEPLOY_SHA"
  test "$origin_main_sha" = "$DEPLOY_SHA"
  if git symbolic-ref --quiet HEAD >/dev/null; then
    echo "canonical workstation checkout must be detached" >&2
    return 1
  fi
  test -z "$(git status --porcelain=v1 --untracked-files=all)"
  for relative_tool in \
    scripts/select_release_evidence.py \
    deploy/workstation/build_verified_image.py \
    deploy/workstation/deploy.py
  do
    test -f "$relative_tool"
    test ! -L "$relative_tool"
    test "$(git hash-object -- "$relative_tool")" = \
      "$(git rev-parse "${DEPLOY_SHA}:${relative_tool}")"
  done
}

verify_exact_checkout
SOURCE_SHA="$DEPLOY_SHA"
APPROVED_REMOTE_REF="refs/remotes/origin/main"
BUILD_MANIFEST="$DEPLOY_STATE/build-${DEPLOY_SHA}.json"
/usr/bin/python3 -I deploy/workstation/build_verified_image.py \
  --repository "$CODE_ROOT" \
  --source-sha "$DEPLOY_SHA" \
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
object. Do not continue unless the release controller's two fetched `main`
refs, the workstation's public GitHub `main`, the canonical detached checkout,
the manifest source, and the printed SHA are the same explicitly approved
`DEPLOY_SHA`. Preserve every old-code quarantine and failed staging tree for
inspection; neither is silently deleted or reused.

## 2. Prepare independent state

Choose the current live directory deliberately and derive the one allowed
candidate path from the approved SHA. Do not create the candidate directory by
hand: `deploy.py prepare` owns its atomic creation and binds the newly created
inode before any candidate-preparation Docker inspection or resource creation.

```bash
set -euo pipefail
verify_exact_checkout
LIVE_DATA_DIR="$APP_ROOT/data-v0.2.0-live"
CANDIDATE_DATA_DIR="$APP_ROOT/data-candidate-${SOURCE_SHA:0:12}"
CANDIDATE_STATE="$APP_ROOT/deploy-state/candidate-${SOURCE_SHA:0:12}.json"
if [[ -e "$CANDIDATE_DATA_DIR" || -L "$CANDIDATE_DATA_DIR" ]]; then
  echo "candidate data path already exists; stop for inspection" >&2
  exit 1
fi
if [[ -e "$CANDIDATE_STATE" || -L "$CANDIDATE_STATE" ]]; then
  echo "candidate state already exists; stop for inspection" >&2
  exit 1
fi
```

`10001:10001` are literal numeric IDs; do not replace them with account names.
The existing live directory must already have the same numeric ownership and
mode, but do not blindly change a live directory. The live directory and
candidate directory must be different. Check both paths before continuing; do
not create either directory through Docker or Compose. Under the per-SHA
prepare lock, the tool's fixed-argv isolated privileged helper atomically
creates a random hidden sibling, assigns numeric `10001:10001` and mode `0700`,
flushes its inode and parent, and only then publishes exactly the direct child
derived from the SHA with Linux `renameat2(RENAME_NOREPLACE)`—that final
no-clobber rename atomically creates exactly the direct child derived from the
SHA. An interruption
before publication can leave a hidden retained staging directory for
administrator inspection, but it cannot occupy the final candidate path. The
helper returns the published device/inode identity. Any
pre-existing object—an empty directory, regular file, hidden-content directory,
symlink, or broken symlink—fails closed. The identity-bound empty check is still
performed immediately before any candidate-preparation Docker inspection or
resource creation. This freshness statement does not cover the earlier
verified-image build.

The final candidate state is flushed to a hidden file and published through the
same dirfd-bound Linux `renameat2(RENAME_NOREPLACE)` atomic no-clobber operation. An
interruption leaves either the hidden pre-publication file or the single-link
final state, never a two-link intermediate state. A concurrent state insertion
is retained and causes refusal; it is never replaced. The same per-SHA lock
remains held from candidate validation through final state publication. These
controls bind the candidate data, state, deployment assets, and running
container to tools from the exact clean `DEPLOY_SHA` checkout.
`APP_ROOT` and `deploy-state` must remain on the same filesystem so each
candidate input can be quarantined with one atomic rename. Candidate data and
state are separate moves rather than one combined transaction, so interruption
between them is possible and is handled by the resumable recovery procedure
below. The preflight and recovery helper both fail closed before creating an
archive when the same-filesystem invariant does not hold.

### Recover interrupted candidate inputs

If an interrupted attempt left either path, first inspect the exact candidate
container, network, state, and data identity. If Docker resources remain, use
only the identity-checked cleanup procedure in section 3; never delete candidate
data. After an administrator confirms that the retained data is not live and no
container mounts it, use the versioned `quarantine` subcommand below. Its only
operator-controlled value is the full source SHA; all input names, archive
names, and move destinations are derived internally and passed through fixed
argv. The parent holds the same non-blocking per-SHA prepare lock through every
move and anchors the hierarchy to dirfd handles opened with `O_NOFOLLOW`.
Candidate data is owned by runtime identity `10001:10001`, so only that data
move is delegated through fixed argv to the narrow
`sudo /usr/bin/python3 -I` helper. The helper independently revalidates the
application root, archive, source, ownership, modes, and device/inode identities
before performing the no-clobber rename. Candidate state remains an
unprivileged move by the deployment user.
Each input uses its own Linux `renameat2(RENAME_NOREPLACE)`, so a racing archive
entry is retained and causes refusal instead of being overwritten; a kernel or
filesystem without that atomic operation also fails closed. A missing,
replaced, unsafe, or busy lock stops recovery.

The subcommand supports data-only, state-only, or both paths. The two inputs do
not move atomically together. If a prior run moved one item and stopped, rerun
it: the remaining item moves into another newly created quarantine archive,
while the earlier archive remains untouched. It never changes candidate
ownership, follows a symbolic link, deletes candidate data or state, or falls
back to a less constrained move. Every `prepare` invocation creates the
required lock before inspecting candidate inputs.

```bash
set -euo pipefail
: "${INTERRUPTED_SOURCE_SHA:?export the full SHA of the interrupted attempt}"
[[ "$INTERRUPTED_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "INTERRUPTED_SOURCE_SHA must be one full lowercase 40-character Git SHA" >&2
  exit 1
}
verify_exact_checkout
/usr/bin/python3 -I deploy/workstation/deploy.py quarantine \
  --source-sha "$INTERRUPTED_SOURCE_SHA"
```

`INTERRUPTED_SOURCE_SHA` identifies the retained input, not necessarily the
revision of the currently verified deployment tool. When a compatibility fix
has produced a new `DEPLOY_SHA`, use the old failed deployment SHA here so the
derived candidate path and per-SHA lock identify the old residual data.

If the command fails after the privileged helper may have committed, it does
not guess, restore, or delete anything and might not print the new archive
path. Inspect the two exact sources and every same-SHA direct-child archive
before deciding whether to rerun. This inspection follows no symlink and makes
no mutation:

```bash
set -euo pipefail
: "${INTERRUPTED_SOURCE_SHA:?export the full SHA of the interrupted attempt}"
[[ "$INTERRUPTED_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 1
SOURCE_PREFIX="${INTERRUPTED_SOURCE_SHA:0:12}"
DATA_SOURCE="$APP_ROOT/data-candidate-$SOURCE_PREFIX"
STATE_SOURCE="$APP_ROOT/deploy-state/candidate-$SOURCE_PREFIX.json"
for SOURCE_PATH in "$DATA_SOURCE" "$STATE_SOURCE"; do
  if [[ -e "$SOURCE_PATH" || -L "$SOURCE_PATH" ]]; then
    /usr/bin/stat --format='source|%n|%F|%d|%i|%u|%g|%a|%h' -- "$SOURCE_PATH"
  else
    printf 'source-absent|%s\n' "$SOURCE_PATH"
  fi
done

shopt -s nullglob
ARCHIVES=("$APP_ROOT"/quarantine-candidate-"$SOURCE_PREFIX"-*)
shopt -u nullglob
ARCHIVE_COUNT=0
for ARCHIVE in "${ARCHIVES[@]}"; do
  ARCHIVE_NAME="${ARCHIVE##*/}"
  [[ "$ARCHIVE_NAME" =~ ^quarantine-candidate-${SOURCE_PREFIX}-[0-9a-f]{16}$ ]] || continue
  [[ -d "$ARCHIVE" && ! -L "$ARCHIVE" ]] || {
    echo "unsafe quarantine archive path: $ARCHIVE" >&2
    exit 1
  }
  ((ARCHIVE_COUNT += 1))
  /usr/bin/stat --format='archive|%n|%F|%d|%i|%u|%g|%a|%h' -- "$ARCHIVE"
  for ENTRY in "$ARCHIVE/data" "$ARCHIVE/candidate-$SOURCE_PREFIX.json"; do
    if [[ -e "$ENTRY" || -L "$ENTRY" ]]; then
      /usr/bin/stat --format='entry|%n|%F|%d|%i|%u|%g|%a|%h' -- "$ENTRY"
    fi
  done
done
((ARCHIVE_COUNT > 0)) || {
  echo "no matching quarantine archive found; stop for diagnosis" >&2
  exit 1
}
```

Require each archive to remain a deployment-user-owned `0700` directory. An
archived `data` entry must be the retained `10001:10001`, `0700` directory with
the incident's original device/inode; an archived state entry must be the
deployment-user-owned `0600` one-link file. If one exact source remains, rerun
the versioned subcommand so it moves that input into a new archive. If both
sources are absent and the retained identities are accounted for in the
archives, do not rerun: preserve the archives and continue with the fail-closed
preflight. Any symlink, unexpected identity, unmatched source, or missing
archive is a stop condition for administrator diagnosis.

Preserve every quarantine archive for diagnosis. Then rerun the fail-closed
preflight and `prepare`; the tool creates a new candidate inode. Do not copy
anything back from an archive. This is an explicit operator recovery step, not
automatic cleanup, and it must never delete candidate data or state.

## 3. Prepare and verify the candidate

Select a canonical decimal loopback port. The tool refuses a listener, any Docker binding, or any Caddy reference to that port. It also refuses an existing candidate project/container so an interrupted attempt cannot be mistaken for a fresh deployment.

```bash
verify_exact_checkout
/usr/bin/python3 -I deploy/workstation/deploy.py prepare \
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
service, then supplies those in-memory bytes with `--file -`. The `create`
stage names that exact service and uses `--no-build` without `--no-deps`, which
is not accepted by the workstation's Compose `create` command and is redundant
after the exact-one-service gate. The final `up` stage names the same service
and retains `--no-deps`. Neither stage reopens the worktree Compose file. The
manifest source, immutable image, deployment assets, and current fixed local
Docker daemon ID must all match.

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
boundary, requires at most one project-labelled container and one network with
at least one of them present, and checks every present resource's exact name and
Compose labels before removing either one:

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
NETWORK_IDS="$("${DOCKER[@]}" network ls --quiet \
  --filter "label=com.docker.compose.project=$PROJECT_NAME")"
if [[ -z "$CONTAINER_IDS" && -z "$NETWORK_IDS" ]]; then
  echo "no candidate container or network remains to clean up" >&2
  exit 1
fi
if [[ -n "$CONTAINER_IDS" ]]; then
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
fi
if [[ -n "$NETWORK_IDS" ]]; then
  test "$(printf '%s\n' "$NETWORK_IDS" | /usr/bin/wc -l)" -eq 1
  NETWORK_ID="$NETWORK_IDS"
  test "$("${DOCKER[@]}" network inspect --format '{{.Name}}' "$NETWORK_ID")" = \
    "$NETWORK_NAME"
  test "$("${DOCKER[@]}" network inspect --format \
    '{{index .Labels "com.docker.compose.project"}}' "$NETWORK_ID")" = \
    "$PROJECT_NAME"
  test "$("${DOCKER[@]}" network inspect --format \
    '{{index .Labels "com.docker.compose.network"}}' "$NETWORK_ID")" = default
fi

if [[ -n "$CONTAINER_IDS" ]]; then
  "${DOCKER[@]}" container rm --force "$CONTAINER_ID"
fi
if [[ -n "$NETWORK_IDS" ]]; then
  "${DOCKER[@]}" network rm "$NETWORK_ID"
fi
)
```

The cleanup accepts the three interrupted Compose shapes: container only,
network only, or both. Every resource that exists must independently pass the
exact project label, expected name, uniqueness, and container service/network
label checks before either removal runs. No matching resource is also a refusal,
not a false success. The candidate data directory is intentionally retained.

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

A second read-only workstation attestation on 2026-10-10 reconstructed the
complete live upstream identity and produced fingerprint
`892810d70dc768db1db79f84276fd6df9b3e83085e34fe8089b604532db11e89`.
The complete non-secret payload is versioned in
`scripts/tests/test_workstation_upstream_verification.py`, where the production
fingerprint function must reproduce that value. The older recorded fingerprint
could not be reproduced from any versioned payload and is not accepted as an
alternative.

Host-port discovery means active ownership: it enumerates only running Docker
containers with full IDs, then requires exactly one matching healthy container
and revalidates its complete identity. Stopped evidence containers may retain a
static `HostConfig.PortBindings` declaration, but they do not own a listener
and are neither selected nor deleted. The frozen legacy container is a member
of Docker's shared default `bridge`; legacy validation therefore requires exact
agreement on that container's network ID, name, and endpoint ID while allowing
unrelated bridge peers. Hardened routes still require their recorded private
network to contain only the candidate container. Any drift in the selected
legacy endpoint or any additional hardened-network endpoint remains a hard
failure.

Use the state path printed by `prepare`:

```bash
STATE_FILE="$APP_ROOT/deploy-state/candidate-${SOURCE_SHA:0:12}.json"
verify_exact_checkout
/usr/bin/python3 -I deploy/workstation/deploy.py switch --state "$STATE_FILE"
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

### Access-code governance gate

Before release publication, a live-demo CTA, or sharing the URL or code, the
workstation administrator must explicitly confirm all four points in a
non-secret operator record:

1. The enterprise access code may be shared with external evaluators.
2. Its authorization scope does not unintentionally expose unrelated sites.
3. The record identifies who owns revocation/rotation.
4. The record explains how an access grant is withdrawn.

An observed gateway challenge is not a substitute for those scope and lifecycle
confirmations. Without all four, the deployed route may remain under private
operator evaluation, but no access code or live-demo CTA is shared. Public
source, exact-SHA CI evidence, and the existing public walkthrough remain the
only published evaluation paths.

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
verify_exact_checkout
/usr/bin/python3 -I deploy/workstation/deploy.py reconcile \
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

Record a non-secret pass/fail outcome for every item below. All nine are
required; a partial pass does not authorize publication:

1. An independent unauthorized browser context cannot reach application
   content through the HTTPS gateway.
2. The page shows `v0.2.1` and the exact full `DEPLOY_SHA`; its commit link
   resolves to the approved GitHub commit.
3. A fresh Manager workspace can deliver one synthetic event, replay it without
   a duplicate effect, and observe tamper rejection.
4. The Manager can assign the generated case to Demo Agent; after switching to
   Agent, the evaluator can add a clearly fictional note, resolve the case, and
   see safe provenance plus ordered audit history. The fictional-text warning
   remains visible, readable, and programmatically associated at both
   viewports.
5. Reset affects only the active synthetic tenant, and a clean browser receives
   an independent workspace.
6. The five-step guide, controls, case detail, and footer remain usable at both
   a desktop viewport and a 320-pixel mobile viewport with keyboard navigation
   and without horizontal page overflow.
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
   This verifies only the controlled run and does not claim that future
   free-text visitors are technically prevented from violating the displayed
   rule.

Keep the old container and data directory unchanged throughout the acceptance
window.

## 7. Roll back

The successful `switch` prints the exact root-owned backup path. Copy that path verbatim; do not copy, rename, edit, or recreate either the backup or its adjacent JSON ledger:

```bash
verify_exact_checkout
/usr/bin/python3 -I deploy/workstation/deploy.py rollback \
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

A successful rollback command is not the end of the failed acceptance path.
Run a second external smoke from clean browser contexts and verify all four
outcomes again: public HTTPS is reachable, an unauthorized context still meets
gateway protection, the authorized response carries the restored route marker,
and workstation inspection plus the public build identity prove the old
upstream identity is serving. Until all four pass, the failure path remains open
and no link, code, release, or live-demo CTA may be published.

If automatic restoration itself fails, stop all deployment activity and give
the administrator the printed trusted backup path plus the exact error. If the
helper observed a digest outside this transaction, preserve and inspect the
current fragment before any manual action. The root-owned ledger and linear
head protect against unprivileged application processes, accidental
writable-file replacement, stale helper operations, and replay through the
supported command flow. They are not a signature, a cross-file atomic
transaction, or a defense against the trusted deployment operator or a
compromised or uncoordinated root writer.
