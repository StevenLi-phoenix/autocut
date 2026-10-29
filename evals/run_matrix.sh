#!/usr/bin/env bash
# 跑整批：任务 × 2 个模型 × N 次，xargs -P 并发；单个 case 失败不影响其他。
# 用法: evals/run_matrix.sh <runset> [arm=baseline] [reps=2]
# 用 caffeinate 包住 xargs，批跑期间主机不睡眠（睡眠会让 timeout 和耗时失真）；CAFFEINATE="" 可关。
# 环境变量: P=并发数（默认 4）；TASKS_LIST="T1 T2 T3 T4"（默认第一批；第二批 "T5 T6 T7 T8 T9"）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runset=${1:?usage: run_matrix.sh <runset> [arm] [reps]}; arm=${2:-baseline}; reps=${3:-2}
# 顺序：第几次 → 模型 → 任务。xargs 并发窗口里装的是不同任务的 case，避免同一任务的 case 同时写 /tmp 下同名目录互相污染
for n in $(seq 1 "$reps"); do
  for model in opus haiku; do
    for task in ${TASKS_LIST:-T1 T2 T3 T4}; do
      echo "$runset $arm $model $task $n"
    done
  done
done | ${CAFFEINATE:-caffeinate -dims} xargs -P "${P:-4}" -L 1 "$ROOT/evals/run_headless.sh"
