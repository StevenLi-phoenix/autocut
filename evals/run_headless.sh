#!/usr/bin/env bash
# 跑一个 eval case：沙盒里 headless 跑 claude -p，再 resume 同一 session 做固定 debrief，最后用校验器判产物。
#
# 用法: evals/run_headless.sh <runset> <arm> <model> <task_id> <n>
#   runset  如 baseline-2026-10-28
#   arm     baseline = 什么都不给；其他 arm 读 evals/arms/<arm>.sh（可定义 arm_setup <sandbox> 和 ARM_CLAUDE_ARGS=(...)）
#   model   opus | haiku（传给 claude --model）
#   task_id T1..T9（见 evals/baseline/tasks.json；字段含义写在该文件的 _comment 里）
#   n       第几次重复
#
# 产物（都在 evals/runs/<runset>/ 下，<case> = <task>-<model>-<n>）:
#   <case>.jsonl / <case>-debrief.jsonl   原始 stream-json（先落盘，后分析）
#   <case>.stderr / <case>-debrief.stderr
#   <case>-verify.json                    校验结果（找不到产物时 fail=no_output）
#   <case>-meta.json                      退出码、耗时、沙盒新增文件清单
#   work/<case>/                          沙盒里新增的非 mp4/wav 小文件（导出的工程文件、agent 写的脚本等，gitignored）
# 沙盒（被测 agent 的 cwd）在 $EVAL_WORK_ROOT/<runset>/<case>/，只放任务素材的副本，不放 truth，不在仓库内。
# 被测 agent 在 macOS Seatbelt（sandbox-exec）里跑：读不到仓库、scratchpad 里别的 case 的沙盒/产物/calib、本项目的 claude 记忆和 team 目录，
# 只放行本 case 的沙盒（EVAL_ISOLATE=0 可关）。cwd 路径里仍带 "autocut" 字样，这一点隔离不掉。
# 单个 case 失败只记录、不让脚本以非零退出（不能中断 xargs 批跑）；只有用法错误退出 2。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
TASKS="$ROOT/evals/baseline/tasks.json"
MEDIA="$ROOT/fixtures/media"
WORK_ROOT="${EVAL_WORK_ROOT:-/private/tmp/claude-501/-Users-lishuyu-Codes-autocut/457cba72-f891-417a-bcd7-e9e176d74173/scratchpad/evalwork}"
DEFAULT_TIMEOUT="${TASK_TIMEOUT:-900}"
DEBRIEF_TIMEOUT="${DEBRIEF_TIMEOUT:-300}"

if [ "$#" -ne 5 ]; then
  echo "usage: $0 <runset> <arm> <model> <task_id> <n>" >&2
  exit 2
fi
runset=$1 arm=$2 model=$3 task=$4 n=$5
case_id="$task-$model-$n"

jq -e --arg t "$task" '.tasks[$t]' "$TASKS" > /dev/null || { echo "unknown task: $task" >&2; exit 2; }
field() { jq -r --arg t "$task" ".tasks[\$t].$1 // empty" "$TASKS"; }
prompt="$(jq -r --arg t "$task" '.tasks[$t].prompt + .suffix' "$TASKS")"
debrief="$(jq -r '.debrief' "$TASKS")"
vtool="$(field verify_tool)"; vtool="${vtool:-verify.py}"
vtask="$(field verify_task)"; truth_name="$(field truth)"
output_name="$(field output)"                       # 旧字段（T1–T4）；T4 为空 = 导出工程文件，自动发现
sources=()
while IFS= read -r s; do [ -n "$s" ] && sources+=("$s"); done < <(jq -r --arg t "$task" '.tasks[$t] | (.sources // [.source])[]' "$TASKS")
vsource="$(field verify_source)"; vsource="${vsource:-${sources[0]}}"
vout="$(field verify_output)"; vout="${vout:-$output_name}"
outputs=()
while IFS= read -r s; do [ -n "$s" ] && outputs+=("$s"); done < <(jq -r --arg t "$task" '.tasks[$t] | (.outputs // (if .output then [.output] else [] end))[]' "$TASKS")
max_turns="$(field max_turns)"; max_turns="${max_turns:-60}"
task_timeout="$(field timeout)"; task_timeout="${task_timeout:-$DEFAULT_TIMEOUT}"

ARM_CLAUDE_ARGS=()
arm_setup() { :; }
if [ "$arm" != "baseline" ]; then
  [ -f "$ROOT/evals/arms/$arm.sh" ] || { echo "unknown arm: $arm (no evals/arms/$arm.sh)" >&2; exit 2; }
  # shellcheck disable=SC1090
  source "$ROOT/evals/arms/$arm.sh"
fi

runs="$ROOT/evals/runs/$runset"
sandbox="$WORK_ROOT/$runset/$case_id"
jsonl="$runs/$case_id.jsonl"
mkdir -p "$runs"
case "$sandbox" in "$WORK_ROOT"/?*/?*) ;; *) echo "refusing sandbox path: $sandbox" >&2; exit 2;; esac
rm -rf "$sandbox"            # 仅清理自己上次建的沙盒
mkdir -p "$sandbox"
for rel in "${sources[@]}"; do
  mkdir -p "$sandbox/$(dirname "$rel")"
  rsync -a --exclude='.*' "$MEDIA/$rel" "$sandbox/$(dirname "$rel")/"   # 排除隐藏项：fixtures 里可能有 .autocut-cache（别的会话生成的 ASR/节拍/切点结果）
done
arm_setup "$sandbox"
inputs_list="$(mktemp)"; trap 'rm -f "$inputs_list"' EXIT
( cd "$sandbox" && find . -type f | sort ) > "$inputs_list"
log() { echo "[$(date +%H:%M:%S)] $case_id: $*" >&2; }

# 读隔离 + 信号隔离 profile（先 deny 再 allow 本 case 沙盒；已验证后一条规则优先）。信号：agent 的 pkill -f / kill 只能打到沙盒内的进程，
# 否则宽泛的 `pkill -f "ffmpeg …"` 会杀掉并行的其他 case 乃至本机其他会话的进程（已验证沙盒内 pkill 沙盒外 sleep 返回 Operation not permitted）。只禁 file-read-data（读文件内容/列目录），
# 保留 stat：否则 cwd 里的 `ls -la` 会因为 stat 不了被拒的 `..` 而以 Operation not permitted 失败，给每个 case 的首条命令制造人为摩擦。
profile="$(mktemp)"; trap 'rm -f "$inputs_list" "$profile"' EXIT
session_dir="$(dirname "$(dirname "$WORK_ROOT")")"
cat > "$profile" <<SB
(version 1)
(allow default)
(deny file-read-data (subpath "$ROOT") (subpath "/System/Volumes/Data$ROOT") (subpath "$session_dir")
  (subpath "$HOME/.claude/projects/-Users-lishuyu-Codes-autocut") (subpath "$HOME/.claude/teams"))
(allow file-read-data (subpath "$sandbox") (subpath "/System/Volumes/Data$sandbox"))
(deny signal)
(allow signal (target same-sandbox))
SB
sandbox_run=()
[ "${EVAL_ISOLATE:-1}" = "1" ] && sandbox_run=(sandbox-exec -f "$profile")

# 不继承父会话的身份：否则被测 agent 会连上当前会话的消息 socket / bridge，甚至当成同一个 team 的成员。
clean_env=(env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_MESSAGING_SOCKET -u CLAUDE_CODE_MESSAGING_TOKEN
           -u CLAUDE_CODE_BRIDGE_SESSION_ID -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ATTENDED -u CLAUDE_PID
           -u CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS -u CLAUDE_CODE_PLUGIN_DIRS -u AI_AGENT)

t0=$(date +%s)
log "start (sandbox $sandbox, max-turns $max_turns, timeout $task_timeout)"
rc=0
( cd "$sandbox" && "${clean_env[@]}" timeout -k 20 "$task_timeout" ${sandbox_run[@]+"${sandbox_run[@]}"} claude -p "$prompt" --model "$model" \
    --output-format stream-json --verbose --dangerously-skip-permissions --max-turns "$max_turns" ${ARM_CLAUDE_ARGS[@]+"${ARM_CLAUDE_ARGS[@]}"} \
    < /dev/null > "$jsonl" 2> "$runs/$case_id.stderr" ) || rc=$?
t1=$(date +%s)
log "task done rc=$rc in $((t1 - t0))s"

sid="$(jq -r 'select(.type=="system" and .subtype=="init") | .session_id' "$jsonl" 2>/dev/null | head -1 || true)"
[ -n "$sid" ] || sid="$(jq -r 'select(.type=="result") | .session_id' "$jsonl" 2>/dev/null | tail -1 || true)"

debrief_rc="skipped"
if [ -n "$sid" ]; then
  debrief_rc=0
  ( cd "$sandbox" && "${clean_env[@]}" timeout -k 20 "$DEBRIEF_TIMEOUT" ${sandbox_run[@]+"${sandbox_run[@]}"} claude -p "$debrief" --resume "$sid" \
      --output-format stream-json --verbose --dangerously-skip-permissions --max-turns 3 \
      < /dev/null > "$runs/$case_id-debrief.jsonl" 2> "$runs/$case_id-debrief.stderr" ) || debrief_rc=$?
  log "debrief rc=$debrief_rc"
else
  log "no session_id in $jsonl; debrief skipped"
fi

# ---- 找产物并校验 ----
verify_json="$runs/$case_id-verify.json"
new_files="$(cd "$sandbox" && find . -type f | sort | comm -13 "$inputs_list" - | sed 's|^\./||' | jq -R . | jq -s .)"

run_verify() {  # <output path> -> JSON on stdout（校验器失败退出码 1 属正常）
  "$PY" "$ROOT/evals/$vtool" --truth "$MEDIA/$truth_name" --source "$MEDIA/$vsource" \
        --output "$1" --task "$vtask" --json 2> /dev/null || true
}

candidates=()
if [ -n "$output_name" ] || [ "${#outputs[@]}" -gt 0 ]; then
  if [ "$vout" = "." ]; then   # 多文件产物（如 T6 的 clip_1..3）：校验器收目录；至少要有一个期望文件才算有产物
    for o in "${outputs[@]}"; do [ -e "$sandbox/$o" ] && { candidates+=("$sandbox"); break; }; done
  else
    if [ -e "$sandbox/$vout" ]; then candidates+=("$sandbox/$vout")
    else  # prompt 只说“输出 X.mp4”没说放哪：根目录没有就在前两层里找同名文件（agent 常把成片放进输入素材文件夹里）
      alt="$(find "$sandbox" -maxdepth 2 -name "$(basename "$vout")" -not -path '*/.*' -type f | head -1)"; [ -n "$alt" ] && candidates+=("$alt")
    fi
  fi
else  # export_nle：任何像工程/时间线的文件，按 FCPXML 包 > FCPXML > xmeml > EDL > OTIO 排序
  for ext in fcpxmld fcpxml xml edl otio; do
    while IFS= read -r f; do [ -n "$f" ] && candidates+=("$f"); done < <(find "$sandbox" -maxdepth 3 -name "*.$ext" -not -path "*/.claude/*" | sort)
  done
fi

if [ "${#candidates[@]}" -eq 0 ]; then
  jq -n --arg task "$vtask" --argjson others "$new_files" \
     '{task:$task, pass:false, fail:"no_output", checks:{exists:false}, reasons:["no_output: 沙盒里没有找到产物"], other_files:$others}' > "$verify_json"
else
  best=""
  for c in "${candidates[@]}"; do
    out="$(run_verify "$c")"
    if ! echo "$out" | jq -e . > /dev/null 2>&1; then
      out="$(jq -n --arg o "$c" '{pass:false, fail:"verify_error", output:$o, reasons:["校验器没有给出合法 JSON"]}')"
    fi
    if [ -z "$best" ] || { [ "$(echo "$best" | jq .pass)" != "true" ] && [ "$(echo "$out" | jq .pass)" = "true" ]; }; then best="$out"; fi
  done
  echo "$best" | jq --argjson others "$new_files" --argjson n "${#candidates[@]}" '. + {other_files:$others, candidates_checked:$n}' > "$verify_json"
fi
log "verify pass=$(jq .pass "$verify_json")"

# 沙盒新增的非 mp4/wav 小文件（导出的工程文件、字幕、agent 写的脚本）留一份在 runs/<runset>/work/<case>/ 方便读
mkdir -p "$runs/work/$case_id"
( cd "$sandbox" && echo "$new_files" | jq -r '.[]' | grep -v -E '\.(mp4|wav|mov|mkv)$|^\.claude/' | tr '\n' '\0' \
    | rsync -a --from0 --files-from=- . "$runs/work/$case_id/" ) 2> /dev/null || true

# /tmp 是并发 case 共用的（沙盒隔离不到）：把本 case 用过的 /tmp 顶层路径移到 evalwork/<runset>/_tmp_debris/<case>/，避免后面的 case 读到残留
"$PY" "$ROOT/evals/quarantine_tmp.py" --since "$t0" --dest "$WORK_ROOT/$runset/_tmp_debris/$case_id" "$jsonl" "$runs/$case_id-debrief.jsonl" 2>&1 \
  | while IFS= read -r l; do log "$l"; done || true

jq -n --arg runset "$runset" --arg arm "$arm" --arg model "$model" --arg task "$task" --argjson n "$n" \
      --arg sid "$sid" --argjson rc "$rc" --arg drc "$debrief_rc" --argjson wall "$((t1 - t0))" \
      --arg cv "$(claude --version 2>/dev/null | head -1)" --arg effort "${CLAUDE_EFFORT:-}" --arg sandbox "$sandbox" \
      --argjson files "$new_files" --argjson mt "$max_turns" --argjson to "$task_timeout" --arg iso "${EVAL_ISOLATE:-1}" \
      '{runset:$runset, arm:$arm, model:$model, task:$task, n:$n, session_id:$sid, task_rc:$rc, debrief_rc:$drc,
        wall_s:$wall, claude_version:$cv, claude_effort_env:$effort, sandbox:$sandbox, new_files:$files,
        max_turns:$mt, timeout_s:$to, isolated:($iso=="1"), signal_isolated:($iso=="1"), timed_out:($rc==124)}' > "$runs/$case_id-meta.json"
exit 0
