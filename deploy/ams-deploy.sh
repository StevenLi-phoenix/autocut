#!/bin/bash
# Deploy this checkout to the AMS deploy copy and restart the AMS service autocut-worker. Run on the mini
# as the owner (lishuyu) or the service user (dsh-runtime) from the source checkout after `git pull`. Replaces deploy/install-mini.sh once the
# worker runs under AMS (as dsh-runtime, which cannot read the owner's home or uv pythons).
#
#   deploy/ams-deploy.sh [--no-restart]
#
# 1. rsync code to $AUTOCUT_DEPLOY_DIR — never worker.env, logs, tests, .git; the copy keeps its own .venv,
#    bin/ (vision_ocr) and ingest.env,
# 2. copy the compiled bin/vision_ocr if this checkout has one,
# 3. build/refresh the venv with python 3.14 from /Users/Shared/ams-apps/python and install the package
#    (non-editable: the copy must not point back into the owner's home),
# 4. `ams ctl restart autocut-worker` — restarting kills a pipeline in flight: check GET /v1/jobs first.
#   AUTOCUT_DEPLOY_DIR   deploy copy (default /Users/Shared/ams-apps/autocut)
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${AUTOCUT_DEPLOY_DIR:-/Users/Shared/ams-apps/autocut}"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
restart=1
for arg in "$@"; do
  case "$arg" in
    --no-restart) restart=0 ;;
    *) echo "usage: ams-deploy.sh [--no-restart]" >&2; exit 2 ;;
  esac
done
if [ "$(cd "$DEST" 2>/dev/null && pwd)" = "$SRC" ]; then
  echo "ams-deploy: deploy dir $DEST is the source checkout" >&2
  exit 2
fi
mkdir -p "$DEST/bin"

echo "ams-deploy: rsync $SRC -> $DEST"
rsync -a --delete \
  --exclude /.git --exclude /.venv --exclude /bin --exclude /ingest.env --exclude /worker.env --exclude '/.worker.env*' \
  --exclude /logs --exclude /tests --exclude /evals --exclude /fixtures --exclude __pycache__ --exclude '*.pyc' \
  --exclude '*.bak.*' --exclude .pytest_cache \
  "$SRC/" "$DEST/"
if [ -x "$SRC/bin/vision_ocr" ]; then cp -p "$SRC/bin/vision_ocr" "$DEST/bin/vision_ocr"; fi

echo "ams-deploy: venv in $DEST"
export UV_PYTHON_INSTALL_DIR=/Users/Shared/ams-apps/python UV_PYTHON_PREFERENCE=only-managed
if [ ! -x "$DEST/.venv/bin/python" ]; then
  "$UV" python install --no-bin 3.14
  "$UV" venv --python 3.14 "$DEST/.venv"
fi
"$UV" pip install --python "$DEST/.venv/bin/python" --reinstall-package autocut --compile-bytecode "$DEST"
chmod -R a+rX "$DEST"

if [ "$restart" = 1 ]; then
  # The owner goes through sudo; DSH already runs as the service user.
  ams() {
    if [ "$(id -un)" = dsh-runtime ]; then
      env AMS_STATE_DIR=/Users/dsh-runtime/.ams /Users/dsh-runtime/ams/.venv/bin/ams "$@"
    else
      sudo -n -u dsh-runtime env HOME=/Users/dsh-runtime AMS_STATE_DIR=/Users/dsh-runtime/.ams \
        /Users/dsh-runtime/ams/.venv/bin/ams "$@"
    fi
  }
  echo "ams-deploy: ams ctl restart autocut-worker"
  ( cd / && ams ctl restart autocut-worker )
fi
echo "ams-deploy: done"
