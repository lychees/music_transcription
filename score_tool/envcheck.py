"""环境检测：HuggingFace 授权、MuseScore、GPU、ffmpeg。"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

HF_TOKEN_PATH = Path.home() / ".cache" / "huggingface" / "token"
# 三种规格的模型权重各自独立授权，需分别到对应页面接受许可。
HF_MODEL_PAGES = {
    size: f"https://huggingface.co/MuScriptor/muscriptor-{size}"
    for size in ("small", "medium", "large")
}
HF_MODEL_PAGE = HF_MODEL_PAGES["medium"]
HF_TOKENS_PAGE = "https://huggingface.co/settings/tokens"

_CONFIG_PATH = Path.home() / ".score_assistant.json"

# Windows 上 MuseScore 的默认安装位置（不会加入 PATH，muscriptor 找不到，
# 需要由我们代为设置 MUSCRIPTOR_MUSESCORE）。
_MUSESCORE_DEFAULTS = (
    r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
    r"C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe",
)


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    try:
        _CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def hf_token_present() -> bool:
    """是否已配置 HuggingFace 凭据（环境变量或 token 文件）。"""
    if os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"):
        return True
    return HF_TOKEN_PATH.is_file() and bool(HF_TOKEN_PATH.read_text().strip())


def load_settings() -> dict:
    """读取用户设置（模型、输出目录等）。"""
    return _load_config()


def save_settings(updates: dict) -> None:
    """合并写入用户设置。"""
    cfg = _load_config()
    cfg.update(updates)
    _save_config(cfg)


def save_hf_token(token: str) -> Path:
    """把 token 写入 ~/.cache/huggingface/token（等价于 `hf auth login`）。"""
    token = token.strip()
    if not token:
        raise ValueError("token 不能为空")
    HF_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    HF_TOKEN_PATH.write_text(token)
    try:
        HF_TOKEN_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows 上 chmod 可能失败，忽略
    return HF_TOKEN_PATH


def musescore_path() -> str | None:
    """找到 MuseScore 4+ 可执行文件则返回路径，否则 None。

    依次尝试：MUSCRIPTOR_MUSESCORE 环境变量 → 用户保存的配置 →
    Windows 默认安装位置 → muscriptor 自身的查找（PATH 等）。
    """
    from muscriptor.utils.sheets import find_musescore, MuseScoreNotFoundError

    if "MUSCRIPTOR_MUSESCORE" not in os.environ:
        candidates = [_load_config().get("musescore"), *_MUSESCORE_DEFAULTS]
        for cand in candidates:
            if cand and Path(cand).is_file():
                os.environ["MUSCRIPTOR_MUSESCORE"] = cand
                break
    try:
        return find_musescore()
    except MuseScoreNotFoundError:
        return None


def set_musescore_override(path: str) -> None:
    """手动指定 MuseScore 路径（写入环境变量并持久化到配置文件）。"""
    os.environ["MUSCRIPTOR_MUSESCORE"] = path
    cfg = _load_config()
    cfg["musescore"] = path
    _save_config(cfg)


def gpu_info() -> str | None:
    """CUDA GPU 名称，不可用则 None。"""
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return None


def ffmpeg_present() -> bool:
    return shutil.which("ffmpeg") is not None
