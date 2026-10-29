#!/usr/bin/env python
"""汇总一个 runset 的 headless 运行：从 stream-json 抽指标，合并 verify 结果，写 metrics.md。

用法：
    .venv/bin/python evals/summarize.py <runset>                 # 写 evals/runs/<runset>/metrics.md 并打印汇总
    .venv/bin/python evals/summarize.py <runset> --show T2-haiku-1 [--max-result 800]
                                                                  # 把一个 case 的完整 transcript（含 debrief）渲染成可读文本
    .venv/bin/python evals/summarize.py <runset> --calls T2-haiku-1   # 只列工具调用序列

transcript 原文在 <case>.jsonl / <case>-debrief.jsonl；本脚本只读不写它们。
可选 evals/runs/<runset>/self_verify.json：{"<case>": "none|exists|probe|content"}，人工读 transcript 后覆盖启发式的「自验」列（表里加 * 标记）。
可选 evals/runs/<runset>/judgements.json：{"<case>": "评审者一句话判断"}，会作为「评审者判断(非实测)」列写进 metrics.md。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("summarize")
RUNS = Path(__file__).resolve().parent / "runs"

INSTALL_RE = re.compile(r"\b(pip3?|uv pip|uv add|uvx?|pipx|brew|npm|npx|pnpm|yarn|apt(-get)?|cargo|gem)\s+(install|add|i)\b"
                        r"|\b(curl|wget)\b|git clone|hf download|\bpip install\b", re.I)
ASR_RE = re.compile(r"whisper|ggml|huggingface|vosk|silero|webrtcvad|auto-editor|funasr|parakeet", re.I)
PROBE_RE = re.compile(r"\bffprobe\b|\bmediainfo\b|\bsoxi\b")
CONTENT_RE = re.compile(r"silencedetect|astats|volumedetect|ebur128|-f\s+null|freezedetect|blackdetect|showinfo|"
                        r"loudnorm|whisper|librosa|numpy|scipy|wave\.open|soundfile|xmllint|ElementTree|etree|xml\.")
EXISTS_RE = re.compile(r"\b(ls|stat|du|wc|file|cat|head|tail|grep|open)\b")


@dataclass
class Call:
    idx: int
    id: str
    name: str
    input: dict
    summary: str
    is_error: bool = False
    result_chars: int = 0
    result_text: str = ""


@dataclass
class Case:
    case_id: str
    task: str
    model: str
    n: int
    calls: list[Call] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    final_text: str = ""
    ctx_peak: int = 0
    init: dict = field(default_factory=dict)
    verify: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    debrief_text: str = ""
    debrief_cost: float = 0.0
    self_verify: str = "none"
    judgement: str = ""
    net_attempts: list[str] = field(default_factory=list)
    asr_probes: list[str] = field(default_factory=list)


def read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for i, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("%s:%d 不是合法 JSON，跳过", path.name, i)
    return out


def block_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(parts)


def one_line(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s.replace("\n", " ↵ ")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def call_summary(name: str, inp: dict) -> str:
    if name == "Bash":
        return "Bash: " + one_line(inp.get("command", ""), 120)
    for key in ("file_path", "path", "query", "skill", "pattern", "url", "prompt", "description"):
        if key in inp:
            return f"{name}: {one_line(str(inp[key]), 120)}"
    return f"{name}: {one_line(json.dumps(inp, ensure_ascii=False), 120)}"


def parse_events(events: list[dict], case: Case) -> None:
    by_id: dict[str, Call] = {}
    for ev in events:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            case.init = ev
        elif t == "assistant":
            msg = ev.get("message", {})
            u = msg.get("usage") or {}
            ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            case.ctx_peak = max(case.ctx_peak, ctx)
            for b in msg.get("content") or []:
                if b.get("type") == "tool_use" and b["id"] not in by_id:
                    c = Call(len(case.calls), b["id"], b["name"], b.get("input") or {}, call_summary(b["name"], b.get("input") or {}))
                    case.calls.append(c)
                    by_id[c.id] = c
                elif b.get("type") == "text" and b.get("text", "").strip():
                    case.final_text = b["text"]  # 最后一段文本；有 result 事件时会被覆盖
        elif t == "user":
            for b in (ev.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    c = by_id[b["tool_use_id"]]
                    c.is_error = bool(b.get("is_error"))
                    c.result_text = block_text(b.get("content"))
                    c.result_chars = len(c.result_text)
        elif t == "result":
            case.result = ev
    if case.result.get("result"):
        case.final_text = case.result["result"]


def classify_self_verify(case: Case, output_hints: list[str]) -> None:
    """启发式：最后一次写产物之后，agent 有没有再看过它。none < exists < probe < content。
    只是表格里的粗标记，失败/非常规 case 以读 transcript 全文为准。"""
    cmds = [(c.idx, c.input.get("command", "")) for c in case.calls if c.name == "Bash"]
    hints = [h for h in (*output_hints, "fcpxml", ".edl", ".xml", "_cut", "_clean") if h]
    last_write = -1
    for c in case.calls:
        text = c.input.get("command", "") if c.name == "Bash" else json.dumps(c.input, ensure_ascii=False)
        unquoted = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', '""', text)  # echo "(>=0.3s)" 之类的引号内容不算重定向
        wrote = (c.name in ("Write", "Edit")) or re.search(r"\bffmpeg\b(?!.*-f\s+null)|(?<![0-9&])>>?\s*[^&\s]|\btee\b|cat\s*<<", unquoted) \
            or re.search(r"open\([^)]*['\"]w|write_text", text)
        if wrote and any(h in text for h in hints):
            last_write = c.idx
    level = 0
    for idx, cmd in cmds:
        if idx <= last_write or not any(h in cmd for h in hints):
            continue
        if CONTENT_RE.search(cmd):
            level = max(level, 3)
        elif PROBE_RE.search(cmd):
            level = max(level, 2)
        elif EXISTS_RE.search(cmd):
            level = max(level, 1)
    case.self_verify = ["none", "exists", "probe", "content"][level]
    case.net_attempts = [one_line(c.input.get("command", ""), 100) for c in case.calls
                         if c.name == "Bash" and INSTALL_RE.search(c.input.get("command", ""))]
    case.net_attempts += [f"{c.name}: {one_line(json.dumps(c.input, ensure_ascii=False), 80)}" for c in case.calls
                          if c.name in ("WebFetch", "WebSearch")]
    case.asr_probes = [one_line(c.input.get("command", ""), 100) for c in case.calls
                       if c.name == "Bash" and ASR_RE.search(c.input.get("command", ""))]


def load_case(runset_dir: Path, case_id: str, tasks: dict) -> Case:
    task, model, n = case_id.rsplit("-", 2)
    case = Case(case_id, task, model, int(n))
    case.events = read_jsonl(runset_dir / f"{case_id}.jsonl")
    parse_events(case.events, case)
    vf = runset_dir / f"{case_id}-verify.json"
    case.verify = json.loads(vf.read_text()) if vf.exists() else {}
    mf = runset_dir / f"{case_id}-meta.json"
    case.meta = json.loads(mf.read_text()) if mf.exists() else {}
    deb = read_jsonl(runset_dir / f"{case_id}-debrief.jsonl")
    for ev in deb:
        if ev.get("type") == "result":
            case.debrief_text = ev.get("result", "")
            case.debrief_cost = ev.get("total_cost_usd", 0.0) or 0.0
    if not case.debrief_text:  # debrief 没有 result 事件时退回最后一段 assistant 文本
        for ev in deb:
            if ev.get("type") == "assistant":
                t = block_text(ev.get("message", {}).get("content"))
                if t.strip():
                    case.debrief_text = t
    t = tasks.get(task) or {}
    classify_self_verify(case, t.get("outputs") or ([t["output"]] if t.get("output") else []))
    return case


# ---------------------------------------------------------------- 指标


def usage(case: Case) -> dict:
    u = case.result.get("usage") or {}
    in_all = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    return {"in_all": in_all, "out": u.get("output_tokens") or 0}


def claimed_removed(case: Case) -> str:
    """T2 要求汇报剪掉的时间：抽取最终回复里“剪掉/去掉/removed”附近的数字（粗提取，供对照 verify 实测）。"""
    t = case.final_text
    m = re.findall(r"(?:剪掉|去掉|删除|删掉|移除|减少|缩短|removed|cut|trimmed|saved)[^\d]{0,30}(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec)", t, re.I)
    m += re.findall(r"(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec\w*)[^\n。]{0,12}(?:被剪|被去|被删|剪掉|去掉)", t, re.I)
    return "/".join(dict.fromkeys(m)) or "-"


def actual_text(case: Case) -> str:
    v = case.verify
    if not v:
        return "(无 verify)"
    if v.get("fail") == "no_output":
        return "no_output"
    parts = []
    if "durations" in v:
        parts.append(f"剪掉 {v['durations']['removed']}s")
    if "timeline_duration" in v:
        parts.append(f"时间线 {v['timeline_duration']}s")
    if "output_info" in v:
        oi = v["output_info"]
        parts.append(f"{oi['width']}x{oi['height']} {oi['duration']}s")
    if v.get("loudness", {}).get("lufs") is not None:
        parts.append(f"{v['loudness']['lufs']} LUFS")
    parts += [one_line(r, 60) for r in v.get("reasons", [])[:3]]
    return "; ".join(parts) or "ok"


def env_of(case: Case) -> str:
    """运行环境：meta.json 里 isolated=true → iso（Seatbelt 读隔离）；缺字段（早期未隔离版）或 false → noniso。"""
    return "iso" if case.meta.get("isolated") is True else "noniso"


def case_row(case: Case) -> dict:
    u = usage(case)
    res = case.result
    secs = (res.get("duration_ms") or 0) / 1000 or case.meta.get("wall_s", 0)
    bash = sum(1 for c in case.calls if c.name == "Bash")
    return {
        "case": case.case_id, "env": env_of(case), "task": case.task, "model": case.model, "n": case.n,
        "pass": bool(case.verify.get("pass")), "calls": len(case.calls), "bash": bash,
        "errors": sum(1 for c in case.calls if c.is_error), "cost": res.get("total_cost_usd") or 0.0,
        "in_all": u["in_all"], "out": u["out"], "ctx_peak": case.ctx_peak, "secs": secs,
        "turns": res.get("num_turns") or 0, "self_verify": case.self_verify,
        "max_out_chars": max((c.result_chars for c in case.calls), default=0),
        "net": len(case.net_attempts), "asr": len(case.asr_probes), "end": res.get("subtype") or ("timeout" if case.meta.get("timed_out") else "no_result"),
        "claim": one_line(case.final_text, 90), "claimed_removed": claimed_removed(case) if case.task == "T2" else "",
        "actual": actual_text(case),
    }


def fmt_k(x: int) -> str:
    return f"{x / 1000:.0f}k"


def render_metrics(runset: str, cases: list[Case]) -> str:
    rows = [case_row(c) for c in cases]
    has_judge = any(c.judgement for c in cases)
    judge = {c.case_id: c.judgement for c in cases}
    L = [f"# Baseline metrics — {runset}", "",
         f"共 {len(rows)} 个 case。pass = verify.py 判定；调用数含所有工具；错误数 = tool_result.is_error；"
         "自验 = 最后一次写产物之后是否再看过它（none/exists/probe/content，启发式）；"
         "in/out = 全部输入(含 cache)/输出 token；ctx峰值 = 单轮最大上下文；最大输出 = 单次工具返回的最大字符数。", "",
         "## 逐 case", "",
         "| case | 环境 | pass | 调用 | Bash | 错误 | $ | in/out tok | ctx峰值 | 秒 | turns | 结束 | 自验 | 联网/安装 | ASR探测 | 最大输出 | 声称 | 实测 |"
         + (" 评审者判断(非实测) |" if has_judge else ""),
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|" + ("---|" if has_judge else "")]
    for r in rows:
        claim = r["claim"] + (f" 〔声称剪掉 {r['claimed_removed']}s〕" if r["claimed_removed"] else "")
        L.append(f"| {r['case']} | {r['env']} | {'PASS' if r['pass'] else 'FAIL'} | {r['calls']} | {r['bash']} | {r['errors']} | {r['cost']:.2f} | "
                 f"{fmt_k(r['in_all'])}/{fmt_k(r['out'])} | {fmt_k(r['ctx_peak'])} | {r['secs']:.0f} | {r['turns']} | {r['end']} | "
                 f"{r['self_verify']} | {r['net']} | {r['asr']} | {r['max_out_chars']} | {claim.replace('|', '/')} | {r['actual'].replace('|', '/')} |"
                 + (f" {judge[r['case']].replace('|', '/')} |" if has_judge else ""))
    L += ["", "## 按 task × model 汇总", "",
          "| task | model | pass | 平均调用 | 平均 Bash | 平均错误 | 平均 $ | 平均秒 | 平均 ctx峰值 | 自验(none/exists/probe/content) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["task"], r["model"]), []).append(r)
    for (task, model), rs in sorted(groups.items()):
        mean = lambda k: statistics.mean(x[k] for x in rs)  # noqa: E731
        sv = "/".join(str(sum(1 for x in rs if x["self_verify"].rstrip("*") == k)) for k in ("none", "exists", "probe", "content"))
        L.append(f"| {task} | {model} | {sum(x['pass'] for x in rs)}/{len(rs)} | {mean('calls'):.1f} | {mean('bash'):.1f} | "
                 f"{mean('errors'):.1f} | {mean('cost'):.2f} | {mean('secs'):.0f} | {fmt_k(int(mean('ctx_peak')))} | {sv} |")
    tot = {m: [r for r in rows if r["model"] == m] for m in sorted({r["model"] for r in rows})}
    L += ["", "## 按 model 汇总", "", "| model | pass | 平均调用 | 平均 $ | 总 $ |", "|---|---|---|---|---|"]
    for m, rs in tot.items():
        L.append(f"| {m} | {sum(x['pass'] for x in rs)}/{len(rs)} | {statistics.mean(x['calls'] for x in rs):.1f} | "
                 f"{statistics.mean(x['cost'] for x in rs):.2f} | {sum(x['cost'] for x in rs):.2f} |")
    L += ["", "## 工具调用序列（每个 case）", ""]
    for c in cases:
        L.append(f"### {c.case_id}")
        for call in c.calls:
            L.append(f"{call.idx + 1:>3}. {'✗ ' if call.is_error else ''}{call.summary}")
        if c.net_attempts:
            L.append("联网/安装尝试: " + " | ".join(c.net_attempts))
        if c.asr_probes:
            L.append(f"ASR/现成剪辑工具探测: {len(c.asr_probes)} 条，首条 {c.asr_probes[0]}")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- transcript 渲染


def render_transcript(case: Case, runset_dir: Path, max_result: int, brief: bool = False) -> str:
    L = [f"===== {case.case_id}  pass={case.verify.get('pass')}  $ {case.result.get('total_cost_usd')}  "
         f"turns={case.result.get('num_turns')}  end={case.result.get('subtype')} ====="]
    i = case.init
    if i:
        L.append(f"[init] model={i.get('model')} tools={len(i.get('tools') or [])} skills={len(i.get('skills') or [])} "
                 f"mcp={len(i.get('mcp_servers') or [])} cwd={i.get('cwd')}")

    def render(events: list[dict]) -> None:
        last_text = ""
        for ev in events:
            t = ev.get("type")
            if t == "assistant":
                for b in ev.get("message", {}).get("content") or []:
                    if b.get("type") == "text" and b["text"].strip():
                        last_text = b["text"].strip()
                        L.append(f"\n[A] {last_text if not brief or len(last_text) <= 700 else last_text[:700] + ' …〔截断〕'}")
                    elif b.get("type") == "thinking":
                        th = (b.get("thinking") or "").strip()
                        if th:  # 空 thinking（内容不可见）不输出
                            L.append(f"\n[thinking] {th[:1500]}")
                    elif b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        if b["name"] == "Bash":
                            cmd = inp.get("command", "")
                            L.append(f"\n[CALL Bash] {cmd if not brief or len(cmd) <= 600 else cmd[:600] + ' …〔截断 ' + str(len(cmd) - 600) + ' 字符〕'}")
                        else:
                            L.append(f"\n[CALL {b['name']}] {json.dumps(inp, ensure_ascii=False)[:300 if brief else 1500]}")
            elif t == "user":
                for b in ev.get("message", {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        txt = block_text(b.get("content"))
                        head = txt if len(txt) <= max_result else txt[: max_result * 2 // 3] + f"\n…〔省略 {len(txt) - max_result} 字符〕…\n" + txt[-max_result // 3:]
                        L.append(f"[RESULT{' ERROR' if b.get('is_error') else ''} {len(txt)} chars]\n{head}")
            elif t == "result":
                res_text = ev.get("result", "")
                L.append(f"\n[RESULT-EVENT] subtype={ev.get('subtype')} is_error={ev.get('is_error')} turns={ev.get('num_turns')} "
                         f"cost={ev.get('total_cost_usd')} dur_ms={ev.get('duration_ms')}"
                         + ("" if res_text.strip() == last_text else f"\n{res_text}"))

    render(case.events)
    deb = read_jsonl(runset_dir / f"{case.case_id}-debrief.jsonl")
    if deb:
        L.append("\n----- DEBRIEF -----")
        render(deb)
    v = case.verify
    L.append(f"\n----- VERIFY: pass={v.get('pass')} reasons={v.get('reasons')} durations={v.get('durations')} -----")
    return "\n".join(L)


def render_digest(case: Case) -> str:
    """调用序列（✗=is_error）穿插 agent 每步前写的说明（截断），再接最终回复开头、debrief、verify。"""
    L = [f"===== {case.case_id} pass={case.verify.get('pass')} ${case.result.get('total_cost_usd', 0):.2f} "
         f"turns={case.result.get('num_turns')} end={case.result.get('subtype')} tools={len(case.init.get('tools') or [])} ====="]
    pending: dict[str, str] = {}
    last = ""
    for ev in case.events:
        if ev.get("type") == "assistant":
            for b in ev.get("message", {}).get("content") or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    last = one_line(b["text"], 260)
                elif b.get("type") == "thinking" and (b.get("thinking") or "").strip():
                    last = "(思考) " + one_line(b["thinking"], 240)
                elif b.get("type") == "tool_use":
                    pending[b["id"]] = last
                    last = ""
    for c in case.calls:
        note = pending.get(c.id, "")
        L.append(f"{c.idx + 1:>3}. {'✗ ' if c.is_error else ''}{c.summary}  [{c.result_chars}c]")
        if note:
            L.append(f"       ↳ {note}")
    L.append("--- 最终回复（开头 600 字）---")
    L.append(case.final_text[:600])
    L.append("--- debrief ---")
    L.append(case.debrief_text)
    v = case.verify
    L.append(f"--- verify: pass={v.get('pass')} reasons={v.get('reasons')} durations={v.get('durations')} "
             f"timeline={v.get('timeline_duration')} net={case.net_attempts[:6]}")
    return "\n".join(L)


def render_contaminated(d: Path) -> str:
    """<runset>/contaminated/（素材被污染）和 <runset>/interrupted/（被外部进程打断）里是被剔除并已重跑的 case：
    列出原因和原结果，不进上面的统计。每个目录里的 reasons.json 是 {case: 原因}。"""
    out = ""
    for bucket, title in (("contaminated", "被剔除的污染 case"), ("interrupted", "被剔除的被打断 case（interrupted）")):
        rf = d / bucket / "reasons.json"
        if not rf.exists():
            continue
        reasons = json.loads(rf.read_text())
        L = ["", f"## {title}（已重跑；原件在 {bucket}/，不计入上面的统计）", "", "| case | 原 pass | 原因 |", "|---|---|---|"]
        for cid, why in sorted(reasons.items()):
            vf = d / bucket / f"{cid}-verify.json"
            orig = ("PASS" if json.loads(vf.read_text()).get("pass") else "FAIL") if vf.exists() else "(原 verify 被覆盖，transcript 在 *.session.jsonl)"
            L.append(f"| {cid} | {orig} | {why.replace('|', '/')} |")
        out += "\n".join(L) + "\n"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runset")
    ap.add_argument("--show", metavar="CASE")
    ap.add_argument("--calls", metavar="CASE")
    ap.add_argument("--digest", metavar="CASE", help="紧凑视图：调用序列 + 每步前的说明 + 最终回复开头 + debrief + verify")
    ap.add_argument("--max-result", type=int, default=800)
    ap.add_argument("--brief", action="store_true", help="--show 时截断长命令/长回复（debrief 与 verify 全文保留）")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    d = RUNS / a.runset
    if not d.is_dir():
        print(f"no such runset: {d}", file=sys.stderr)
        return 2
    tasks = json.loads((Path(__file__).parent / "baseline" / "tasks.json").read_text())["tasks"]
    ids = sorted(p.stem for p in d.glob("*.jsonl") if not p.stem.endswith("-debrief"))
    if a.digest:
        if a.digest not in ids:
            print(f"no such case: {a.digest}", file=sys.stderr)
            return 2
        print(render_digest(load_case(d, a.digest, tasks)))
        return 0
    if a.show or a.calls:
        cid = a.show or a.calls
        if cid not in ids:
            print(f"no such case: {cid}（有: {', '.join(ids)}）", file=sys.stderr)
            return 2
        case = load_case(d, cid, tasks)
        if a.show:
            print(render_transcript(case, d, 250 if a.brief else a.max_result, a.brief))
        else:
            for c in case.calls:
                print(f"{c.idx + 1:>3}. {'ERR ' if c.is_error else ''}{c.summary}   [{c.result_chars} chars]")
        return 0
    cases = [load_case(d, i, tasks) for i in ids]
    sf = d / "self_verify.json"
    if sf.exists():
        sv = json.loads(sf.read_text())
        for c in cases:
            if c.case_id in sv:
                c.self_verify = sv[c.case_id] + "*"
    jf = d / "judgements.json"
    if jf.exists():
        jd = json.loads(jf.read_text())
        for c in cases:
            c.judgement = jd.get(c.case_id, "")
    text = render_metrics(a.runset, cases)
    text += render_contaminated(d)
    (d / "metrics.md").write_text(text)
    print(text.split("## 工具调用序列")[0])
    print(f"-> {d / 'metrics.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
