#!/bin/bash
# 在 mini 上安装/更新 autocut-worker（以 lishuyu 身份，经 tcc-host 拉起，LaunchAgent）。可重复运行。
# 用法：deploy/install-mini.sh [--restart]
#   - 拉取代码并同步依赖；登记 ~/.config/tcc-host/allow.json；写 plist；--restart 才 bootout/bootstrap。
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/bin:/bin
APP=$HOME/services/autocut
LABEL=app.lishuyu.autocut.worker
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
ALLOW=$HOME/.config/tcc-host/allow.json
cd "$APP"
git pull -q --ff-only
uv pip install -q -e .
mkdir -p "$APP/logs"

# allow.json：备份后原子改写，只动 autocut-worker 一项
cp -p "$ALLOW" "$ALLOW.bak.$(date +%Y%m%d%H%M%S)"
python3 - "$ALLOW" "$APP" <<'PY'
import json, os, sys
p, app = sys.argv[1:3]
d = json.load(open(p))
d.setdefault("services", {})["autocut-worker"] = {
    "exe": f"{app}/.venv/bin/python", "args": ["-m", "autocut.worker"], "cwd": app}
tmp = p + ".tmp"
json.dump(d, open(tmp, "w"), indent=2, ensure_ascii=False); open(tmp, "a").write("\n")
os.chmod(tmp, 0o600); os.replace(tmp, p)
PY
~/.local/libexec/tcc-host/tcc-host check autocut-worker

[ -f "$PLIST" ] && cp -p "$PLIST" "$PLIST.bak.$(date +%Y%m%d%H%M%S)"
cp "$APP/deploy/$LABEL.plist" "$PLIST"
plutil -lint "$PLIST"
if [ "${1:-}" = "--restart" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  sleep 3
  launchctl print "gui/$(id -u)/$LABEL" | grep -E "state|pid|last exit" || true
  curl -s --max-time 5 http://127.0.0.1:8102/health || echo "health 未响应，看 $APP/logs/worker.err.log"
fi
