"""LLM 网关客户端（registry 里的 llmgateway，OpenAI 兼容）。

- 原始响应先落盘（<job>/work/llm/NNN-<purpose>.json）再解析——付费 API 的规矩。
- 每个任务一个现金上限（job.config.budget_cny，默认 ¥2）：调用前检查已花费，超了抛 BudgetExceeded，不再调用。
- 花费 = 网关 /api/me 的 spend_usd 差值（精确，含缓存折扣）；查不到时按 usage 的 token 数保守估算。
- 视觉：messages 里放 image_url（data URI）。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from autocut.worker.config import WorkerConfig, load_secrets
from autocut.worker.store import atomic_write_json, read_json

log = logging.getLogger(__name__)
# 美元/百万 token（输入, 输出）：按 token 数的保守估价。网关 /api/me 的 spend 结算有延迟（实测大多数调用的差值是 0），
# 所以每次花费取 max(网关差值, 估价)，预算宁可高估也不低估。
PRICE_USD_PER_MTOK = {"claude-haiku-5-5": (1.0, 5.0), "claude-sonnet-5-5": (3.0, 15.0), "deepseek-flash": (0.3, 1.2), "deepseek-v4-pro": (0.6, 2.4)}
DEFAULT_PRICE = (3.0, 15.0)
THINKING_OFF = {"claude-sonnet-5-5", "claude-opus-5-5"}


def estimate_usd(model: str, usage: dict[str, Any]) -> float:
    pin, pout = PRICE_USD_PER_MTOK.get(model, DEFAULT_PRICE)
    ti = usage.get("prompt_tokens") or 0
    to = usage.get("completion_tokens") or 0
    if not ti and not to:
        ti = usage.get("total_tokens") or 0
    return (ti * pin + to * pout) / 1e6


class LLMError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


class BudgetExceeded(LLMError):
    pass


class Gateway:
    def __init__(self, cfg: WorkerConfig, job_dir: Path, budget_cny: float) -> None:
        self.cfg, self.job_dir, self.budget_cny = cfg, job_dir, budget_cny
        self.key = load_secrets(cfg.secrets_file).get("AUTOCUT_LLM_KEY") or ""
        self.dir = job_dir / "work" / "llm"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = job_dir / "work" / "budget.json"

    # ---- 预算
    def spent_usd(self) -> float:
        return float(read_json(self.state_path, {"spent_usd": 0.0}).get("spent_usd", 0.0))

    def spent_cny(self) -> float:
        return self.spent_usd() * self.cfg.cny_per_usd

    def _add_spend(self, usd: float, purpose: str, model: str) -> None:
        st = read_json(self.state_path, {"spent_usd": 0.0, "calls": 0, "by_purpose": {}})
        st["spent_usd"] = round(st.get("spent_usd", 0.0) + usd, 6)
        st["calls"] = st.get("calls", 0) + 1
        bp = st.setdefault("by_purpose", {})
        bp[purpose] = round(bp.get(purpose, 0.0) + usd, 6)
        st["budget_cny"] = self.budget_cny
        atomic_write_json(self.state_path, st)

    def _gateway_spend(self) -> float | None:
        try:
            req = urllib.request.Request(f"{self.cfg.llm_base}/api/me", headers={"Authorization": f"Bearer {self.key}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return float(json.loads(r.read())["spend_usd"])
        except Exception as e:  # noqa: BLE001
            log.debug("读网关花费失败：%s", e)
            return None

    # ---- 调用
    def chat(self, model: str, messages: list[dict[str, Any]], *, purpose: str, max_tokens: int = 4000, temperature: float = 0.7,
             json_mode: bool = False, retries: int = 2) -> dict[str, Any]:
        """返回 {"text", "usage", "usd", "raw_path"}。"""
        if not self.key:
            raise LLMError("no_llm_key", "worker 没有配置 LLM 网关 key", "在 secrets 文件里设置 AUTOCUT_LLM_KEY（见 docs/dev/worker.md）")
        if self.spent_cny() >= self.budget_cny:
            raise BudgetExceeded("budget_exceeded", f"本任务 LLM 花费已达上限 ¥{self.budget_cny:g}（已花 ¥{self.spent_cny():.3f}）",
                                 "告诉用户预算用完；如需继续，开新 job 或用 --budget-cny 调高上限（要用户同意）")
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if not model.startswith("claude"):  # Claude 5 系列网关会拒绝 temperature（400 deprecated）
            body["temperature"] = temperature
        if model in THINKING_OFF:  # sonnet/opus 5.5 默认先长时间思考，3000 token 全花在思考上、正文为空（实测）；haiku 不支持这个参数
            body["thinking"] = {"type": "between_tools"}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        seq = len(list(self.dir.glob("*.json"))) + 1
        raw_path = self.dir / f"{seq:03d}-{purpose}.json"
        before = self._gateway_spend()
        last: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.perf_counter()
            req = urllib.request.Request(f"{self.cfg.llm_base}/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    raw = r.read()
                # 先落盘，再解析
                raw_path.write_bytes(raw)
                data = json.loads(raw)
                msg = data["choices"][0]["message"]
                text = msg.get("content") or ""
                usage = data.get("usage") or {}
                break
            except urllib.error.HTTPError as e:
                err = e.read()
                raw_path.with_suffix(".err.json").write_bytes(err)
                last = LLMError("llm_http_error", f"网关返回 {e.code}：{err[:200].decode('utf-8', 'replace')}", "429/5xx 稍后重试；403 看 key 的模型白名单")
                if e.code not in (429, 500, 502, 503, 504):
                    raise last from None
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as e:
                last = LLMError("llm_call_failed", f"{type(e).__name__}: {e}", "稍后重试")
            log.warning("LLM %s 第 %d 次失败：%s", purpose, attempt + 1, last)
            time.sleep(2 * (attempt + 1))
        else:
            raise last or LLMError("llm_call_failed", "未知失败")
        after = self._gateway_spend()
        usd = estimate_usd(model, usage)
        if before is not None and after is not None and after >= before:
            usd = max(usd, after - before)
        self._add_spend(usd, purpose, model)
        log.info("LLM %s model=%s tokens=%s usd=%.5f（累计 ¥%.3f / ¥%g）%.1fs", purpose, model, usage.get("total_tokens"), usd, self.spent_cny(), self.budget_cny, time.perf_counter() - t0)
        if not text and data["choices"][0].get("finish_reason") == "length":
            raise LLMError("llm_truncated", "模型输出被截断（max_tokens 太小，推理模型会把额度花在思考上）", "调大 max_tokens 或换非推理模型")
        return {"text": text, "usage": usage, "usd": usd, "raw_path": str(raw_path)}

    def chat_json(self, model: str, messages: list[dict[str, Any]], *, purpose: str, validate: Any = None, **kw: Any) -> Any:
        """要求返回 JSON；解析/校验失败时把错误反馈给模型重试一次（再花一次预算）。"""
        msgs = list(messages)
        err = ""
        for attempt in range(2):
            r = self.chat(model, msgs, purpose=purpose if attempt == 0 else purpose + "-retry", **kw)
            try:
                obj = extract_json(r["text"])
                if validate:
                    validate(obj)
                return obj
            except (ValueError, KeyError, TypeError) as e:
                err = str(e)
                log.warning("LLM %s 返回的 JSON 不合格（%s），重试", purpose, err)
                msgs = [*msgs, {"role": "assistant", "content": r["text"][:6000]},
                        {"role": "user", "content": f"上面的输出不能用：{err}。只输出一个合格的 JSON，不要任何其他文字。"}]
        raise LLMError("llm_bad_json", f"模型两次都没给出合格 JSON：{err}", "看 work/llm/ 里的原始响应")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """从模型输出里取出第一个 JSON 对象/数组（容忍 ```json 围栏和前后废话）。"""
    t = text.strip()
    m = _FENCE.search(t)
    if m:
        t = m.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i = t.find(opener)
        j = t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                continue
    raise ValueError("输出里没有可解析的 JSON")


def image_part(path: Path, mime: str = "image/jpeg") -> dict[str, Any]:
    b64 = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
