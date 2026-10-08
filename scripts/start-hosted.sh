#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PORT:-}" ]]; then
  echo "[commerce-ops-desk] PORT is required" >&2
  exit 64
fi

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || ((PORT < 1 || PORT > 65535)); then
  echo "[commerce-ops-desk] PORT must be an integer from 1 to 65535" >&2
  exit 64
fi

PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV_DIR="${COMMERCE_OPS_VENV_DIR:-/var/tmp/commerce-ops-desk-venv}"
PYTHON_BIN="$VENV_DIR/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[commerce-ops-desk] Python runtime is missing; run the workspace setup first" >&2
  exit 70
fi

if [[ ! -f "$PRODUCT_DIR/frontend/dist/index.html" ]]; then
  echo "[commerce-ops-desk] Frontend build is missing; run npm run build first" >&2
  exit 70
fi

DATA_DIR="$PRODUCT_DIR/data"
"$PYTHON_BIN" -c '
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])


def fail() -> None:
    print(
        "[commerce-ops-desk] data directory must be a real directory owned by the service user",
        file=sys.stderr,
    )
    raise SystemExit(78)


try:
    os.mkdir(path, 0o700)
except FileExistsError:
    pass
except OSError:
    fail()

descriptor = -1
try:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        fail()
    os.fchmod(descriptor, 0o700)
except OSError:
    fail()
finally:
    if descriptor >= 0:
        os.close(descriptor)
' "$DATA_DIR"
cd "$PRODUCT_DIR"

export COMMERCE_OPS_ENVIRONMENT="${COMMERCE_OPS_ENVIRONMENT:-demo}"

if [[ -z "${COMMERCE_OPS_DATABASE_URL:-}" ]]; then
  if [[ "$COMMERCE_OPS_ENVIRONMENT" == "production" ]]; then
    echo "[commerce-ops-desk] production requires an explicit database URL" >&2
    exit 78
  fi

  if [[ "$COMMERCE_OPS_ENVIRONMENT" == "demo" ]]; then
    LOCAL_DATABASE_DIR="/var/tmp/commerce-ops-desk"
    "$PYTHON_BIN" -c '
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])


def fail() -> None:
    print(
        "[commerce-ops-desk] local database directory must be a real directory owned by the service user",
        file=sys.stderr,
    )
    raise SystemExit(78)


try:
    os.mkdir(path, 0o700)
except FileExistsError:
    pass
except OSError:
    fail()

descriptor = -1
try:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        fail()
    os.fchmod(descriptor, 0o700)
except OSError:
    fail()
finally:
    if descriptor >= 0:
        os.close(descriptor)
' "$LOCAL_DATABASE_DIR"
    export COMMERCE_OPS_DATABASE_URL="sqlite+pysqlite:///$LOCAL_DATABASE_DIR/commerce_ops.db"
  fi
fi

if [[ -z "${COMMERCE_OPS_SESSION_SECRET:-}" ]]; then
  if [[ "$COMMERCE_OPS_ENVIRONMENT" == "production" ]] && \
    [[ -z "${COMMERCE_OPS_SESSION_SECRET_FILE:-}" ]]; then
    echo "[commerce-ops-desk] production requires an explicit session secret source" >&2
    exit 78
  fi

  if [[ -n "${COMMERCE_OPS_SESSION_SECRET_FILE:-}" ]]; then
    SESSION_SECRET_FILE="$COMMERCE_OPS_SESSION_SECRET_FILE"
    SESSION_SECRET_ALLOW_CREATE=0
  else
    SESSION_SECRET_FILE="$PRODUCT_DIR/data/.session-secret"
    SESSION_SECRET_ALLOW_CREATE=1
  fi

  COMMERCE_OPS_SESSION_SECRET="$("$PYTHON_BIN" -c '
import os
import secrets
import stat
import sys
import tempfile

path = os.path.abspath(sys.argv[1])
allow_create = sys.argv[2] == "1"


def fail(reason: str) -> None:
    print(f"[commerce-ops-desk] session secret {reason}", file=sys.stderr)
    raise SystemExit(78)


def validate() -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        fail("file is missing")
    except OSError:
        fail("file cannot be opened safely")

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            fail("path must be a regular file")
        if metadata.st_uid != os.geteuid():
            fail("file must be owned by the service user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            fail("file permissions must be 0600")

        secret_file = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        with secret_file:
            value = secret_file.read()
    except OSError:
        fail("file cannot be read")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(value) < 32:
        fail("must contain at least 32 bytes")
    if b"\x00" in value or b"\n" in value or b"\r" in value:
        fail("must contain exactly one line without control characters")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        fail("must contain valid UTF-8")
    return value


if not os.path.lexists(path):
    if not allow_create:
        fail("file is missing")
    parent = os.path.dirname(path)
    try:
        parent_metadata = os.lstat(parent)
    except OSError:
        fail("directory is unavailable")
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
        fail("directory must be a real directory")
    if parent_metadata.st_uid != os.geteuid():
        fail("directory must be owned by the service user")
    if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        fail("directory permissions must be 0700")

    descriptor = -1
    temporary_path = ""
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".session-secret.",
            dir=parent,
        )
        os.fchmod(descriptor, 0o600)
        secret_value = secrets.token_urlsafe(48).encode("ascii")
        os.write(descriptor, secret_value)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            pass
    except OSError:
        fail("could not be created atomically")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

os.write(sys.stdout.fileno(), validate())
' "$SESSION_SECRET_FILE" "$SESSION_SECRET_ALLOW_CREATE")"
  export COMMERCE_OPS_SESSION_SECRET
fi

if [[ -v COMMERCE_OPS_WEBHOOK_ENABLED ]]; then
  case "$COMMERCE_OPS_WEBHOOK_ENABLED" in
    true | false) ;;
    *)
      echo "[commerce-ops-desk] webhook enabled must be true or false" >&2
      exit 78
      ;;
  esac
elif [[ "$COMMERCE_OPS_ENVIRONMENT" == "demo" ]]; then
  export COMMERCE_OPS_WEBHOOK_ENABLED=true
else
  export COMMERCE_OPS_WEBHOOK_ENABLED=false
fi

if [[ "$COMMERCE_OPS_WEBHOOK_ENABLED" == "true" ]]; then
  if [[ -v COMMERCE_OPS_WEBHOOK_MASTER_SECRET ]]; then
    "$PYTHON_BIN" -c '
import os
import sys


def fail(reason: str) -> None:
    print(f"[commerce-ops-desk] webhook master secret {reason}", file=sys.stderr)
    raise SystemExit(78)


value = os.environb.get(b"COMMERCE_OPS_WEBHOOK_MASTER_SECRET")
if value is None:
    fail("is unavailable")
if len(value) < 32:
    fail("must contain at least 32 bytes")
try:
    value.decode("utf-8")
except UnicodeDecodeError:
    fail("must contain valid UTF-8")
'
  else
    if [[ -v COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE ]]; then
      WEBHOOK_SECRET_FILE="$COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE"
      WEBHOOK_SECRET_ALLOW_CREATE=0
    elif [[ "$COMMERCE_OPS_ENVIRONMENT" == "demo" ]]; then
      WEBHOOK_SECRET_FILE="$PRODUCT_DIR/data/.webhook-secret"
      WEBHOOK_SECRET_ALLOW_CREATE=1
    else
      echo \
        "[commerce-ops-desk] enabled webhook requires an explicit master secret source" \
        >&2
      exit 78
    fi

    COMMERCE_OPS_WEBHOOK_MASTER_SECRET="$("$PYTHON_BIN" -c '
import os
import secrets
import stat
import sys
import tempfile

path = os.path.abspath(sys.argv[1])
allow_create = sys.argv[2] == "1"
ascii_whitespace = b" \t\n\r\v\f"


def fail(reason: str) -> None:
    print(f"[commerce-ops-desk] webhook master secret {reason}", file=sys.stderr)
    raise SystemExit(78)


def validate() -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        fail("file is missing")
    except OSError:
        fail("file cannot be opened safely")

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            fail("path must be a regular file")
        if metadata.st_uid != os.geteuid():
            fail("file must be owned by the service user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            fail("file permissions must be 0600")

        secret_file = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        with secret_file:
            value = secret_file.read()
    except OSError:
        fail("file cannot be read")
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if value.endswith(b"\n"):
        value = value[:-1]
        if value.endswith(b"\r"):
            value = value[:-1]
    if b"\x00" in value:
        fail("must not contain NUL bytes")
    if value and (
        value[:1] in ascii_whitespace or value[-1:] in ascii_whitespace
    ):
        fail("must not contain leading or trailing ASCII whitespace")
    if len(value) < 32:
        fail("must contain at least 32 bytes")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        fail("must contain valid UTF-8")
    return value


if not os.path.lexists(path):
    if not allow_create:
        fail("file is missing")
    parent = os.path.dirname(path)
    try:
        parent_metadata = os.lstat(parent)
    except OSError:
        fail("directory is unavailable")
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(
        parent_metadata.st_mode
    ):
        fail("directory must be a real directory")
    if parent_metadata.st_uid != os.geteuid():
        fail("directory must be owned by the service user")
    if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        fail("directory permissions must be 0700")

    descriptor = -1
    temporary_path = ""
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".webhook-secret.",
            dir=parent,
        )
        os.fchmod(descriptor, 0o600)
        secret_value = secrets.token_urlsafe(48).encode("ascii")
        os.write(descriptor, secret_value)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            pass
    except OSError:
        fail("could not be created atomically")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

os.write(sys.stdout.fileno(), validate())
' "$WEBHOOK_SECRET_FILE" "$WEBHOOK_SECRET_ALLOW_CREATE")"
    export COMMERCE_OPS_WEBHOOK_MASTER_SECRET
  fi
fi

"$PYTHON_BIN" -m alembic \
  -c "$PRODUCT_DIR/backend/alembic.ini" \
  upgrade head

exec "$PYTHON_BIN" -m uvicorn app.main:create_app \
  --factory \
  --no-proxy-headers \
  --app-dir "$PRODUCT_DIR/backend" \
  --host 0.0.0.0 \
  --port "$PORT"
