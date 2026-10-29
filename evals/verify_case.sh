#!/usr/bin/env bash
# 对已跑完的 case 重新校验（校验规则调整后不用重跑 agent）：读 meta.json 里的沙盒路径，按 tasks.json 找产物，重写 <case>-verify.json。
# 用法: evals/verify_case.sh <runset> <case_id> [<case_id> ...]      例: evals/verify_case.sh baseline2-2026-10-28 T5-haiku-1
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"; TASKS="$ROOT/evals/baseline/tasks.json"; MEDIA="$ROOT/fixtures/media"
runset=${1:?usage: verify_case.sh <runset> <case_id>...}; shift
runs="$ROOT/evals/runs/$runset"
for case_id in "$@"; do
  task="${case_id%%-*}"
  sandbox="$(jq -r .sandbox "$runs/$case_id-meta.json")"
  [ -d "$sandbox" ] || { echo "$case_id: 沙盒不在了 ($sandbox)，跳过" >&2; continue; }
  field() { jq -r --arg t "$task" ".tasks[\$t].$1 // empty" "$TASKS"; }
  vtool="$(field verify_tool)"; vtool="${vtool:-verify.py}"; vtask="$(field verify_task)"; truth_name="$(field truth)"
  output_name="$(field output)"
  sources=(); while IFS= read -r s; do [ -n "$s" ] && sources+=("$s"); done < <(jq -r --arg t "$task" '.tasks[$t] | (.sources // [.source])[]' "$TASKS")
  vsource="$(field verify_source)"; vsource="${vsource:-${sources[0]}}"; vout="$(field verify_output)"; vout="${vout:-$output_name}"
  outputs=(); while IFS= read -r s; do [ -n "$s" ] && outputs+=("$s"); done < <(jq -r --arg t "$task" '.tasks[$t] | (.outputs // (if .output then [.output] else [] end))[]' "$TASKS")
  # 输入文件清单 = 素材副本；新增文件 = 沙盒里不在其中的文件
  inputs="$(mktemp)"
  for rel in "${sources[@]}"; do ( cd "$MEDIA" && find "$rel" -type f -not -path '*/.*' ); done | sort > "$inputs"
  new_files="$(cd "$sandbox" && find . -type f | sed 's|^\./||' | sort | comm -13 "$inputs" - | jq -R . | jq -s .)"; rm -f "$inputs"
  candidates=()
  if [ -n "$output_name" ] || [ "${#outputs[@]}" -gt 0 ]; then
    if [ "$vout" = "." ]; then for o in "${outputs[@]}"; do [ -e "$sandbox/$o" ] && { candidates+=("$sandbox"); break; }; done
    else
      if [ -e "$sandbox/$vout" ]; then candidates+=("$sandbox/$vout")
      else alt="$(find "$sandbox" -maxdepth 2 -name "$(basename "$vout")" -not -path '*/.*' -type f | head -1)"; [ -n "$alt" ] && candidates+=("$alt"); fi
    fi
  else
    for ext in fcpxmld fcpxml xml edl otio; do
      while IFS= read -r f; do [ -n "$f" ] && candidates+=("$f"); done < <(find "$sandbox" -maxdepth 3 -name "*.$ext" -not -path "*/.claude/*" | sort)
    done
  fi
  if [ "${#candidates[@]}" -eq 0 ]; then
    jq -n --arg task "$vtask" --argjson others "$new_files" '{task:$task, pass:false, fail:"no_output", checks:{exists:false}, reasons:["no_output: 沙盒里没有找到产物"], other_files:$others}' > "$runs/$case_id-verify.json"
  else
    best=""
    for c in "${candidates[@]}"; do
      out="$("$PY" "$ROOT/evals/$vtool" --truth "$MEDIA/$truth_name" --source "$MEDIA/$vsource" --output "$c" --task "$vtask" --json 2> /dev/null || true)"
      echo "$out" | jq -e . > /dev/null 2>&1 || out="$(jq -n --arg o "$c" '{pass:false, fail:"verify_error", output:$o, reasons:["校验器没有给出合法 JSON"]}')"
      if [ -z "$best" ] || { [ "$(echo "$best" | jq .pass)" != "true" ] && [ "$(echo "$out" | jq .pass)" = "true" ]; }; then best="$out"; fi
    done
    echo "$best" | jq --argjson others "$new_files" --argjson n "${#candidates[@]}" '. + {other_files:$others, candidates_checked:$n}' > "$runs/$case_id-verify.json"
  fi
  echo "$case_id: pass=$(jq .pass "$runs/$case_id-verify.json")"
done
