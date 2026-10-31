"""白名单与路径限定：agent 只能调这些子命令，所有路径参数必须落在 job 目录内。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# 允许的子命令 → 是否"重活"（占 heavy 槽位：ASR / 编码 / 大量 CPU）
COMMANDS: dict[str, bool] = {
    "doctor": False,
    "transcribe": True,
    "transcript": True,  # 第一次要识别（重活）；之后走缓存
    "clip": False,  # agent 按句段 ID 建候选：吃缓存，秒级
    "analyze": True,
    "cut": True,
    "highlights": True,
    "montage": True,
    "narrate": True,
    "show": False,
    "set": False,
    "render": True,
    "export": False,
    "check": True,
}
# 故意不开放：review（阻塞，长驻）、ytpmv（本次范围外）
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_ID_RE = re.compile(r"^r-[0-9]{6}-[0-9a-f]{6}$")


class PolicyError(ValueError):
    """请求被策略拒绝；message 面向 agent，写明哪里不对和怎么改。"""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def is_heavy(cmd: str) -> bool:
    return COMMANDS[cmd]


def check_job_id(job_id: str) -> str:
    if not JOB_ID_RE.match(job_id or ""):
        raise PolicyError("bad_job_id", f"job 名不合法：{job_id!r}", "只能用字母数字和 . _ -，最长 64 位，首字符为字母或数字")
    return job_id


def inside(job_dir: Path, rel: str) -> Path:
    """rel（相对 job 目录或绝对路径）解析后必须在 job 目录内（含符号链接解析），返回解析后的绝对路径。"""
    base = job_dir.resolve()
    target = (base / rel).resolve() if not rel.startswith("~") else Path("/nonexistent-home-expansion-blocked")
    try:
        target.relative_to(base)
    except ValueError:
        raise PolicyError("path_outside_job", f"路径在 job 目录之外：{rel}", f"只能使用 job 目录内的文件（{base}），用相对路径，例如 src/talk.mp4") from None
    return target


def _looks_like_path(tok: str) -> bool:
    return "/" in tok or tok.startswith("~") or tok in ("..", ".")


def check_command(cmd: str, args: list[str], job_dir: Path) -> list[str]:
    """校验子命令和参数，返回原样参数（cwd 是 job 目录，相对路径按它解析）。"""
    if cmd not in COMMANDS:
        raise PolicyError("command_not_allowed", f"不允许的子命令：{cmd}", f"可用：{'、'.join(sorted(COMMANDS))}")
    if not all(isinstance(a, str) for a in args):
        raise PolicyError("bad_args", "args 必须是字符串数组", '例如 ["src/talk.mp4", "--pace", "natural"]')
    for tok in args:
        if "\x00" in tok:
            raise PolicyError("bad_args", "参数里有空字符", "")
        val = tok.split("=", 1)[1] if tok.startswith("--") and "=" in tok else tok
        if tok.startswith("-") and "=" not in tok:
            continue  # 纯选项名
        if _looks_like_path(val):
            inside(job_dir, val)
    log.debug("policy ok：%s %s", cmd, args)
    return list(args)
