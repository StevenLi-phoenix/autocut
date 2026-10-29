#!/usr/bin/env bash
# 把一个 case 的全部产物归档到 <runset>/<bucket>/（并把沙盒目录挪到 evalwork/<runset>-<bucket>/），不删除任何东西。
# 用于被污染 / 被打断的 case：重跑前先归档，否则 run_headless.sh 会覆盖同名 jsonl 和清空沙盒。
#
# 用法: evals/archive_case.sh <runset> <bucket> <case_id>...      bucket 例: contaminated | interrupted
# 之后在 <runset>/<bucket>/reasons.json 里写 {"<case>": "原因"}，summarize.py 会把它们列在 metrics.md 的“被剔除”一节。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_ROOT="${EVAL_WORK_ROOT:-/private/tmp/claude-501/-Users-lishuyu-Codes-autocut/457cba72-f891-417a-bcd7-e9e176d74173/scratchpad/evalwork}"
runset=${1:?usage: archive_case.sh <runset> <bucket> <case_id>...}; bucket=${2:?}; shift 2
runs="$ROOT/evals/runs/$runset"
mkdir -p "$runs/$bucket" "$WORK_ROOT/$runset-$bucket"
for c in "$@"; do
  moved=0
  for f in "$c.jsonl" "$c.stderr" "$c-debrief.jsonl" "$c-debrief.stderr" "$c-verify.json" "$c-meta.json"; do
    if [ -e "$runs/$f" ]; then mv "$runs/$f" "$runs/$bucket/"; moved=$((moved + 1)); fi
  done
  [ -d "$runs/work/$c" ] && mv "$runs/work/$c" "$runs/$bucket/work-$c"
  [ -d "$WORK_ROOT/$runset/$c" ] && mv "$WORK_ROOT/$runset/$c" "$WORK_ROOT/$runset-$bucket/"
  echo "$runset/$c: archived $moved files into $bucket/"
done
