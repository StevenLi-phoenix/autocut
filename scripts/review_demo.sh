#!/usr/bin/env bash
# 审阅页演示：用 fixtures/media 里的素材手工构造一份方案，然后启动审阅页。
#
#   scripts/review_demo.sh              口播（talk_zh.mp4）：单条时间线模式
#   scripts/review_demo.sh highlights   直播高光（stream_vod.mp4）：候选清单模式
#
# 环境变量：PORT=8765 固定端口；NO_OPEN=1 不自动打开浏览器；BUILD_ONLY=1 只生成方案、打印路径。
# 方案写在 ${TMPDIR:-/tmp}/autocut-review-demo/，不改 fixtures。
# （等 autocut.recipes.cut 可用后，可以换成 `autocut cut fixtures/media/talk_zh.mp4` 生成的真方案。）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIND="${1:-cut}"
OUT="${TMPDIR:-/tmp}/autocut-review-demo"

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
mkdir -p "$OUT"

PLAN="$(python - "$ROOT" "$OUT" "$KIND" <<'PY'
import json, sys
from pathlib import Path

from autocut.plan import Captions, Output, Plan, Reframe, Segment, Source, new_segment_id

root, out, kind = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
media = root / "fixtures" / "media"


def mmss(t: float) -> str:
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def words_for(text: str, start: float, end: float) -> list[dict]:
    """按字数均匀分配时间——只是演示用，不是语音识别结果。标点单独成项（start == end）。"""
    chars = [c for c in text]
    body = [c for c in chars if c not in "，。！？、…"]
    step = (end - start) / max(len(body), 1)
    t, res = start, []
    for c in chars:
        if c in "，。！？、…":
            res.append({"text": c, "start": round(t, 3), "end": round(t, 3), "p": 0.97})
        else:
            res.append({"text": c, "start": round(t, 3), "end": round(t + step, 3), "p": 0.95})
            t += step
    return res


def demo_cut() -> Path:
    truth = json.loads((media / "talk_zh.truth.json").read_text())
    src_path = media / "talk_zh.mp4"
    segs: list[Segment] = []
    words: list[dict] = []
    n = 0
    sentences = [s for s in truth["segments"] if s["kind"] == "speech"]

    def add(**kw) -> None:
        nonlocal n
        n += 1
        segs.append(Segment(id=new_segment_id("d", n), source="main", **kw))

    for i, s in enumerate(truth["segments"]):
        a, b = round(s["start"], 3), round(s["end"], 3)
        if s["kind"] == "speech":
            words += words_for(s["text"], a, b)
            tag = s.get("tag")
            if tag == "filler":
                add(start=a, end=b, action="cut", reason="filler", confidence=0.71, text=s["text"], detail="语气词，前后各 0.5s 静音")
            elif tag == "retake":
                later = next(x for x in sentences[sentences.index(s) + 1:] if not x.get("tag") or x.get("tag") != "retake")
                if "不对" in s["text"]:
                    add(start=a, end=b, action="cut", reason="retake", confidence=0.93, text=s["text"], detail="重说标记（“不对，我重新说一遍”）")
                else:
                    add(start=a, end=b, action="cut", reason="retake", confidence=0.88, text=s["text"],
                        detail=f"被 {mmss(later['start'])} 那句取代", evidence={"similarity": 0.81})
            else:
                add(start=a, end=b, action="keep", reason="speech", text=s["text"])
        else:
            gap = b - a
            if gap < 0.5:
                add(start=a, end=b, action="keep", reason="pause", detail=f"停顿 {gap:.2f}s · 保留（自然停顿）")
            elif gap > 3.0 and i == len(truth["segments"]) - 1:
                add(start=a, end=b, action="cut", reason="silence", confidence=0.99, detail=f"片尾静音 {gap:.1f}s")
            else:
                keep = 0.4
                add(start=a, end=round(a + keep, 3), action="keep", reason="pause", detail=f"停顿 {gap:.1f}s → 保留 {keep}s")
                conf = 0.62 if abs(gap - 3.4) < 0.01 else 0.96  # 31–34s 那个长停顿可能是故意留给观众看代码
                add(start=round(a + keep, 3), end=b, action="cut", reason="silence", confidence=conf,
                    detail=f"长停顿 {gap:.1f}s → 保留 {keep}s" + ("；停顿前后在讲代码，可能是故意留白" if conf < 0.75 else ""),
                    evidence={"gap": round(gap, 2)})
    (out / "talk_zh.words.json").write_text(json.dumps(words, ensure_ascii=False))
    plan = Plan(
        recipe="cut",
        sources={"main": Source("main", str(src_path), duration=truth["duration"], width=640, height=360, fps=30.0, words="talk_zh.words.json")},
        outputs=[Output("main", segs, captions=Captions(enabled=True))],
        options={"pace": "natural"},
        notes=["演示方案：逐词时间是按字数均匀分配的，不是语音识别结果。"],
    )
    plan.path = out / "talk_zh.autocut.json"
    plan.save()
    return plan.path


def demo_highlights() -> Path:
    truth = json.loads((media / "stream_vod.truth.json").read_text())
    src_path = media / "stream_vod.mp4"
    clips = [
        # (起点, 高光起点, 终点, 分数, 标题, 理由, 选中, 拿不准的说明)
        (30.0, 33.0, 38.0, 92, "我去！这也能秒？！", ["弹幕 6.1× 爆发", "音量峰 +14dB", "关键词“我去”"], True, None),
        (79.5, 83.0, 89.5, 88, "不是吧！一打三！", ["弹幕 5.2× 爆发", "音量峰 +11dB", "关键词“不是吧”"], True, None),
        (126.0, 131.0, 137.0, 81, "完了完了完了，诶，赢了？！", ["无解说 · 游戏声爆发 + 画面剧烈变化", "音量峰 +9dB"], True,
         "这里一直有人说话，找不到不打断句子的位置，只能在电平最低处硬切"),
        (56.0, 59.5, 70.0, 40, "（抽奖刷屏）扣一参与", ["弹幕高峰，但都是重复的“1”（疑似抽奖刷屏，没选）", "原始分 83，因刷屏扣到 40"], False, None),
        (44.0, 48.0, 52.5, 55, "弹幕说要我开麦", ["弹幕 2.4× 上升", "排名第 5，只取前 3 条，没选"], False, None),
    ]
    outs: list[Output] = []
    for rank, (a, t0, b, score, title, reasons, selected, unc) in enumerate(clips, 1):
        outs.append(Output(
            id=f"clip_{rank}", width=1080, height=1920, selected=selected, score=score, title=title, reasons=reasons,
            captions=Captions(enabled=True, burn=True), reframe=Reframe(mode="center"),
            segments=[
                Segment(f"c001", "main", a, t0, "keep", "lead_in", 0.85, detail=f"铺垫 {t0 - a:.1f}s，让观众知道发生了什么；起点落在句子开头"),
                Segment(f"c002", "main", t0, b, "keep", "highlight", 0.55 if unc else 0.9,
                        detail=reasons[0] + "；结尾落在笑声结束后 0.6s" + (f"；{unc}" if unc else ""), text=title),
            ],
        ))
    plan = Plan(
        recipe="highlights",
        sources={"main": Source("main", str(src_path), duration=truth["duration"], width=640, height=360, fps=30.0)},
        outputs=outs,
        options={"n": 3, "length": [15, 30], "aspect": "9:16"},
        notes=["演示方案：分数和理由是手写的，用来演示候选清单界面。", "成片画幅 1080×1920（9:16），裁切框默认居中；主体不在中间时在审阅页调整"],
    )
    plan.path = out / "stream_vod.autocut.json"
    plan.save()
    return plan.path


print(demo_cut() if kind == "cut" else demo_highlights() if kind == "highlights" else sys.exit(f"未知演示类型 {kind!r}，可选：cut | highlights"))
PY
)"

echo "演示方案：$PLAN" >&2
[[ "${BUILD_ONLY:-}" == "1" ]] && { echo "$PLAN"; exit 0; }

OPEN="True"
[[ "${NO_OPEN:-}" == "1" ]] && OPEN="False"
exec python - "$PLAN" "${PORT:-0}" "$OPEN" <<'PY'
import logging, sys
from autocut.review.server import serve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
serve(sys.argv[1], port=int(sys.argv[2]), open_browser=sys.argv[3] == "True")
PY
