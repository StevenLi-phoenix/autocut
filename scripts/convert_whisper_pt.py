#!/usr/bin/env python
"""把 OpenAI Whisper 的 PyTorch 权重（.pt）转成 whisper.cpp 的 ggml 格式，全程不依赖 torch。

用法：
    python scripts/convert_whisper_pt.py <in.pt> <out.bin> [--assets DIR] [--f32]

.pt 是 zip 容器：`<root>/data.pkl`（pickle，描述 dims 和 state_dict）+
`<root>/data/<key>`（每个 storage 的原始字节）。这里用白名单版 `pickle.Unpickler`
把 torch 的 `_rebuild_tensor_v2` / `*Storage` 换成基于 numpy 的实现，从而不必安装 torch。

输出布局与 whisper.cpp `models/convert-pt-to-ggml.py` 完全一致：
    magic | hparams(10 个 int32 + ftype) | mel filters | 词表 | 张量...
"""

from __future__ import annotations

import argparse
import base64
import logging
import pickle
import struct
import sys
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

logger = logging.getLogger("convert_whisper_pt")

GGML_MAGIC = 0x67676D6C  # "ggml"
# hparams 在文件里的写入顺序（与官方脚本一致；n_mels 之后紧跟 ftype）
HPARAM_ORDER = (
    "n_vocab",
    "n_audio_ctx",
    "n_audio_state",
    "n_audio_head",
    "n_audio_layer",
    "n_text_ctx",
    "n_text_state",
    "n_text_head",
    "n_text_layer",
    "n_mels",
)
# 即使在 f16 模式下也必须保持 f32 的张量（ggml 对这些算子尚无 f16 路径）；
# 另外所有 ndim < 2 的张量（bias、layernorm 参数）同样用 f32
F32_TENSORS = frozenset(
    {
        "encoder.conv1.bias",
        "encoder.conv2.bias",
        "encoder.positional_embedding",
        "decoder.positional_embedding",
    }
)
CONV_BIAS_TENSORS = frozenset({"encoder.conv1.bias", "encoder.conv2.bias"})
# 多语种模型的词表大小下限（51865 = 多语种 v1/v2，51866 = large-v3 系列）
MULTILINGUAL_MIN_VOCAB = 51865

# torch storage 类名 → numpy dtype
STORAGE_DTYPES: dict[str, np.dtype] = {
    "HalfStorage": np.dtype("<f2"),
    "FloatStorage": np.dtype("<f4"),
    "DoubleStorage": np.dtype("<f8"),
}


# --------------------------------------------------------------------------- #
# 无 torch 读取 .pt
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StorageRef:
    """pickle 里的 storage 引用：`data/<key>` 文件，按 dtype 解释。"""

    key: str
    dtype: np.dtype
    numel: int


def _rebuild_tensor_v2(
    storage: np.ndarray,
    storage_offset: int,
    size: tuple[int, ...],
    stride: tuple[int, ...],
    *_ignored: Any,
) -> np.ndarray:
    """`torch._utils._rebuild_tensor_v2` 的 numpy 版：offset/stride 单位都是“元素”。"""
    itemsize = storage.dtype.itemsize
    view = np.lib.stride_tricks.as_strided(
        storage[storage_offset:],
        shape=tuple(size),
        strides=tuple(s * itemsize for s in stride),
    )
    return np.ascontiguousarray(view)


class _StorageType:
    """`torch.HalfStorage` 等类的占位对象，只带一个 dtype。"""

    def __init__(self, dtype: np.dtype) -> None:
        self.dtype = dtype


class TorchFreeUnpickler(pickle.Unpickler):
    """只放行 .pt 里实际出现的几个全局对象，其余一律拒绝（pickle 不可信输入）。"""

    def __init__(self, fp: BinaryIO, zf: zipfile.ZipFile, root: str) -> None:
        super().__init__(fp)
        self._zf = zf
        self._root = root
        self._storages: dict[str, np.ndarray] = {}

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) == ("collections", "OrderedDict"):
            return OrderedDict
        if (module, name) == ("torch._utils", "_rebuild_tensor_v2"):
            return _rebuild_tensor_v2
        if module == "torch" and name in STORAGE_DTYPES:
            return _StorageType(STORAGE_DTYPES[name])
        raise pickle.UnpicklingError(f"不支持的 pickle 全局对象: {module}.{name}")

    def persistent_load(self, pid: tuple[Any, ...]) -> np.ndarray:
        # pid = ("storage", <_StorageType>, key, location, numel)
        tag, storage_type, key, _location, numel = pid
        if tag != "storage":
            raise pickle.UnpicklingError(f"未知 persistent id: {tag!r}")
        if key not in self._storages:
            raw = self._zf.read(f"{self._root}/data/{key}")
            arr = np.frombuffer(raw, dtype=storage_type.dtype)
            if arr.size != numel:
                raise ValueError(f"storage {key} 元素数不符: 文件 {arr.size} != 声明 {numel}")
            self._storages[key] = arr
        return self._storages[key]


def load_pt(path: Path) -> dict[str, Any]:
    """读取 Whisper .pt，返回 {"dims": {...}, "model_state_dict": {name: ndarray}}。"""
    with zipfile.ZipFile(path) as zf:
        pkl_names = [n for n in zf.namelist() if n.endswith("/data.pkl")]
        if len(pkl_names) != 1:
            raise ValueError(f"{path} 不是 PyTorch zip 权重（找到 data.pkl: {pkl_names}）")
        root = pkl_names[0].removesuffix("/data.pkl")
        # 字节序文件（新版 torch 才有）：本脚本只支持小端
        if f"{root}/byteorder" in zf.namelist():
            byteorder = zf.read(f"{root}/byteorder").decode().strip()
            if byteorder != "little":
                raise ValueError(f"不支持的字节序: {byteorder}")
        logger.info("读取 %s（zip 根目录 %r）", path.name, root)
        with zf.open(pkl_names[0]) as fp:
            ckpt = TorchFreeUnpickler(fp, zf, root).load()
    if "dims" not in ckpt or "model_state_dict" not in ckpt:
        raise ValueError(f"checkpoint 缺少 dims/model_state_dict，实际 keys: {list(ckpt)}")
    return ckpt


# --------------------------------------------------------------------------- #
# 资产（mel filters / 词表）
# --------------------------------------------------------------------------- #


def find_default_assets() -> Path | None:
    """自动寻找含 mel_filters.npz 的目录：已安装的 mlx_whisper / whisper，或 uv 缓存里的 mlx_whisper。"""
    import importlib.util

    for pkg in ("whisper", "mlx_whisper"):
        spec = importlib.util.find_spec(pkg)
        if spec and spec.submodule_search_locations:
            for loc in spec.submodule_search_locations:
                cand = Path(loc) / "assets"
                if (cand / "mel_filters.npz").exists():
                    return cand
    for cand in sorted((Path.home() / ".cache/uv").glob("archive-v0/*/mlx_whisper/assets")):
        if (cand / "mel_filters.npz").exists():
            return cand
    return None


def load_mel_filters(assets: Path, n_mels: int) -> np.ndarray:
    """返回 float32 的 (n_mels, 201) 梅尔滤波器组。"""
    with np.load(assets / "mel_filters.npz") as f:
        key = f"mel_{n_mels}"
        if key not in f:
            raise KeyError(f"mel_filters.npz 里没有 {key}，可用: {list(f.files)}")
        return np.ascontiguousarray(f[key], dtype="<f4")


def load_vocab(assets: Path, n_vocab: int) -> list[bytes]:
    """读 tiktoken 词表，按 rank 顺序返回 token 字节串（多语种用 multilingual，否则 gpt2）。"""
    name = "multilingual.tiktoken" if n_vocab >= MULTILINGUAL_MIN_VOCAB else "gpt2.tiktoken"
    tokens: dict[int, bytes] = {}
    for line in (assets / name).read_text().splitlines():
        if not line.strip():
            continue
        b64, rank = line.split()
        tokens[int(rank)] = base64.b64decode(b64)
    ordered = [tokens[i] for i in range(len(tokens))]  # rank 必须连续，缺了就 KeyError
    logger.info("词表 %s: %d 个 token", name, len(ordered))
    return ordered


# --------------------------------------------------------------------------- #
# 写 ggml
# --------------------------------------------------------------------------- #


def write_header(fout: BinaryIO, hparams: dict[str, int], use_f16: bool) -> None:
    fout.write(struct.pack("<i", GGML_MAGIC))
    for key in HPARAM_ORDER:
        fout.write(struct.pack("<i", hparams[key]))
    fout.write(struct.pack("<i", int(use_f16)))


def write_mel_and_vocab(fout: BinaryIO, filters: np.ndarray, vocab: list[bytes]) -> None:
    fout.write(struct.pack("<ii", *filters.shape))
    fout.write(filters.tobytes())  # 行优先 f32，等价于官方的逐元素 struct.pack("f")
    fout.write(struct.pack("<i", len(vocab)))
    for tok in vocab:
        fout.write(struct.pack("<i", len(tok)))
        fout.write(tok)


def tensor_for_ggml(name: str, data: np.ndarray, use_f16: bool) -> tuple[np.ndarray, int]:
    """按官方规则决定张量的存储精度，返回 (写出的数组, ftype)；ftype 0=f32, 1=f16。"""
    data = data.squeeze()
    if name in CONV_BIAS_TENSORS:
        data = data.reshape(data.shape[0], 1)  # [n] → [n, 1]
    if use_f16 and not (data.ndim < 2 or name in F32_TENSORS):
        return np.ascontiguousarray(data, dtype="<f2"), 1
    return np.ascontiguousarray(data, dtype="<f4"), 0


def write_tensor(fout: BinaryIO, name: str, data: np.ndarray, ftype: int) -> None:
    name_b = name.encode("utf-8")
    fout.write(struct.pack("<iii", data.ndim, len(name_b), ftype))
    for dim in reversed(data.shape):  # ggml 的维度顺序与 numpy 相反
        fout.write(struct.pack("<i", dim))
    fout.write(name_b)
    fout.write(data.tobytes())


def convert(src: Path, dst: Path, assets: Path, use_f16: bool = True) -> None:
    ckpt = load_pt(src)
    hparams: dict[str, int] = {k: int(v) for k, v in ckpt["dims"].items()}
    state: OrderedDict[str, np.ndarray] = ckpt["model_state_dict"]
    logger.info("hparams: %s", hparams)
    logger.info("%d 个张量，精度 %s", len(state), "f16" if use_f16 else "f32")

    filters = load_mel_filters(assets, hparams["n_mels"])
    vocab = load_vocab(assets, hparams["n_vocab"])

    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as fout:
        write_header(fout, hparams, use_f16)
        write_mel_and_vocab(fout, filters, vocab)
        for i, (name, arr) in enumerate(state.items(), 1):
            data, ftype = tensor_for_ggml(name, arr, use_f16)
            write_tensor(fout, name, data, ftype)
            logger.debug("[%d/%d] %s %s %s", i, len(state), name, data.shape, "f16" if ftype else "f32")
            if i % 100 == 0 or i == len(state):
                logger.info("已写 %d/%d 个张量", i, len(state))
    logger.info("完成: %s (%.1f MB)", dst, dst.stat().st_size / 1e6)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Whisper .pt → whisper.cpp ggml（无需 torch）")
    ap.add_argument("src", type=Path, help="输入 .pt（OpenAI Whisper 权重）")
    ap.add_argument("dst", type=Path, help="输出 ggml .bin")
    ap.add_argument("--assets", type=Path, help="含 mel_filters.npz 与 *.tiktoken 的目录（缺省自动探测）")
    ap.add_argument("--f32", action="store_true", help="全部张量写 f32（缺省 f16，与官方脚本一致）")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐张量打印")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    assets = args.assets or find_default_assets()
    if assets is None:
        logger.error("找不到 assets 目录，请用 --assets 指定")
        return 2
    logger.info("assets: %s", assets)
    convert(args.src, args.dst, assets, use_f16=not args.f32)
    return 0


if __name__ == "__main__":
    sys.exit(main())
