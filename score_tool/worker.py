"""转录工作逻辑：一次推理同时产出未量化 MIDI 与量化乐谱。

与 muscriptor CLI 的区别：CLI 每次运行只产一种输出，这里把事件流收集起来后
分别生成「聆听/DAW 用」的未量化 MIDI 和「排版用」的量化 MIDI + 乐谱，
模型只跑一遍。
"""

from __future__ import annotations

import contextlib
import io
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from muscriptor.events import NoteStartEvent, ProgressEvent
from muscriptor.transcription_model import TranscriptionModel
from muscriptor.utils.sheets import prepare_output_dir, write_sheets

ProgressCb = Callable[[int, int], None]  # (已完成块数, 总块数)
LogCb = Callable[[str], None]

AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}


@dataclass
class TranscriptionResult:
    midi_path: Path  # 未量化 MIDI（适合聆听、导入 DAW）
    n_notes: int
    audio_seconds: float
    elapsed_seconds: float
    sheets_dir: Path | None = None  # 乐谱目录（量化 MIDI + MusicXML + PDF）
    sheets_files: list[Path] = field(default_factory=list)
    first_note_onset: float | None = None  # 首个音符的音频时刻（音谱对齐用）
    tempo_bpm: float | None = None  # 检测到的速度
    audio_path: Path | None = None  # 实际转录的音频文件（B 站下载时指向下载产物）
    out_dir: Path | None = None  # 实际输出目录（链接模式下按视频标题生成）


def _safe_name(title: str) -> str:
    import re as _re

    return _re.sub(r'[\\/:*?"<>|]+', "_", title).strip() or "bilibili"


_BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _bili_get_json(url: str, referer: str | None = None) -> dict:
    import json
    import urllib.request

    headers = {"User-Agent": _BILI_UA}
    if referer is not None:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _bili_view(bvid: str) -> dict:
    # 注意：view 接口带 Referer 会被反爬 412，只发 UA。
    return _bili_get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")


def _bili_resolve_bvid(url: str, log: LogCb) -> tuple[str, dict | None, int]:
    """从链接解析 (bvid, view 数据或 None, 分P 序号)。

    支持 BV 链接、av 链接、b23.tv 短链、?p=N 分集。
    """
    import re

    page_no = 1
    m = re.search(r"[?&]p=(\d+)", url)
    if m:
        page_no = max(1, int(m.group(1)))
    if "b23.tv" in url:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": _BILI_UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            url = r.geturl()
        log(f"短链跳转 → {url}")
    m = re.search(r"(BV[0-9A-Za-z]+)", url)
    if m:
        return m.group(1), None, page_no
    m = re.search(r"av(\d+)", url, re.IGNORECASE)
    if m:
        data = _bili_get_json(f"https://api.bilibili.com/x/web-interface/view?aid={m.group(1)}")
        if data.get("code") == 0:
            return data["data"]["bvid"], data, page_no
    raise ValueError("无法从链接中解析视频号（BV/av）")


def download_bilibili(url: str, out_dir: str | Path, on_log: LogCb | None = None) -> Path:
    """下载 B 站视频音轨并转成 wav，返回文件路径。

    直接走 B 站公开 API（www 页面有反爬 412，API 没有），不依赖 yt-dlp。
    """
    import shutil
    import subprocess
    import urllib.request

    log = on_log or (lambda s: None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    bvid, data, page_no = _bili_resolve_bvid(url, log)
    if data is None:
        data = _bili_view(bvid)
    if data.get("code") != 0:
        raise RuntimeError(f"B 站接口返回错误：{data.get('message')}（code {data.get('code')}）")
    v = data["data"]
    pages = v.get("pages") or [{"cid": v["cid"], "part": v["title"]}]
    if page_no > len(pages):
        raise RuntimeError(f"视频只有 {len(pages)} 个分集，没有 P{page_no}")
    page = pages[page_no - 1]
    title = v["title"] if page_no == 1 else f"{v['title']} P{page_no} {page.get('part', '')}".strip()
    cid = page["cid"]
    log(f"视频：{title}（{page.get('duration', v.get('duration', 0))} 秒）")

    play = _bili_get_json(
        f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&fnval=16",
        f"https://www.bilibili.com/video/{bvid}",
    )
    audios = (play.get("data") or {}).get("dash", {}).get("audio") or []
    if play.get("code") != 0 or not audios:
        raise RuntimeError("未能获取音频流地址（视频可能需要登录、付费或已下架）")
    best = max(audios, key=lambda a: a.get("bandwidth", 0))

    log("下载音频流…")
    req = urllib.request.Request(
        best["baseUrl"],
        headers={"User-Agent": _BILI_UA, "Referer": f"https://www.bilibili.com/video/{bvid}"},
    )
    m4s = out_dir / f"{_safe_name(title)}.m4s"
    with urllib.request.urlopen(req, timeout=600) as r, open(m4s, "wb") as f:
        shutil.copyfileobj(r, f)

    wav = m4s.with_suffix(".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(m4s), "-ac", "1", "-ar", "44100", str(wav)],
        check=True,
    )
    m4s.unlink(missing_ok=True)
    log(f"已下载：{wav.name}")
    return wav


class _QueueWriter(io.TextIOBase):
    """把 muscriptor 打到 stderr 的进度/计时信息转发进消息队列。"""

    def __init__(self, q: "queue.Queue[tuple]"):
        self._q = q
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                self._q.put(("log", line))
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._q.put(("log", self._buf.strip()))
        self._buf = ""


class ScoreTranscriber:
    """加载一次模型，可连续转录多个文件。"""

    def __init__(self, model_size: str = "medium", device: str | None = None):
        self.model_size = model_size
        self._device = device
        self._model: TranscriptionModel | None = None

    def load(self) -> None:
        if self._model is None:
            self._model = TranscriptionModel.load_model(
                weights_path=self.model_size, device=self._device
            )

    def transcribe(
        self,
        audio_path: str | Path,
        out_dir: str | Path | None,
        want_sheets: bool = True,
        instruments: list[str] | None = None,
        cfg_coef: float = 1.0,
        detect_tempo: str | bool = "best-effort",
        on_progress: ProgressCb | None = None,
        on_log: LogCb | None = None,
    ) -> TranscriptionResult:
        """转录一个音频文件。

        产出：
          out_dir/<曲名>.mid   —— 未量化 MIDI
          out_dir/sheets/      —— 量化 MIDI + MusicXML + 总谱/分谱 PDF（want_sheets 时）
        out_dir 为 None 时按音频文件名生成 <曲名>_score。
        """
        self.load()
        assert self._model is not None
        model = self._model

        audio_path = Path(audio_path)
        if out_dir is None:
            out_dir = audio_path.with_suffix("").parent / (audio_path.stem + "_score")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        sheets_dir = out_dir / "sheets"
        if want_sheets:
            # 提前检查：乐谱目录必须为空或不存在，避免转录半天才报错。
            prepare_output_dir(sheets_dir)

        log = on_log or (lambda s: None)
        progress = on_progress or (lambda done, total: None)

        # 与 CLI 一致：字符串三态 → TempoDetection（True/False/"best-effort"）
        tempo_mode = {"true": True, "false": False}.get(detect_tempo, detect_tempo)

        t0 = time.perf_counter()
        log(f"节拍检测：{audio_path.name}")
        beat_grid = model.detect_beat_grid_for(audio_path, tempo_mode)

        log("模型转录中（按 5 秒分块）…")
        events = []
        for ev in model.transcribe(
            audio_path,
            cfg_coef=cfg_coef,
            instruments=instruments or None,
        ):
            if isinstance(ev, ProgressEvent):
                progress(ev.completed, ev.total)
            else:
                events.append(ev)

        n_notes = sum(1 for e in events if isinstance(e, NoteStartEvent))
        if n_notes == 0:
            log("警告：没有识别到任何音符（音频可能不含可转录的乐器）")

        onsets = [e.start_time for e in events if isinstance(e, NoteStartEvent)]
        if beat_grid is not None:
            beat_grid = beat_grid.with_onset_delay(onsets)

        # 未量化 MIDI：保留原始时值，适合聆听与二次编辑。
        midi_bytes = model.events_to_midi_bytes(
            iter(events), beat_grid=beat_grid, quantize=False
        )
        midi_path = out_dir / f"{audio_path.stem}.mid"
        midi_path.write_bytes(midi_bytes)
        log(f"已保存 MIDI：{midi_path}")

        sheets_files: list[Path] = []
        if want_sheets:
            # 量化 MIDI：音符吸附到节拍网格，是乐谱排版的正确输入。
            quantized_ok = beat_grid is not None and beat_grid.beat_subdivision is not None
            if not quantized_ok:
                log("警告：未能检测出稳定节拍，乐谱将按未量化时值排版，效果可能欠佳")
            midi_quant = model.events_to_midi_bytes(
                iter(events), beat_grid=beat_grid, quantize=True
            )
            log(f"用 MuseScore 排版乐谱 → {sheets_dir}")
            sheets_files = write_sheets(midi_quant, sheets_dir, quantized=quantized_ok)
            log(f"已生成 {len(sheets_files)} 个乐谱文件")

        try:
            from muscriptor.utils.audio import load_audio

            wav = load_audio(audio_path, target_sr=16000)
            audio_seconds = wav.shape[-1] / 16000.0
        except Exception:
            audio_seconds = 0.0

        return TranscriptionResult(
            midi_path=midi_path,
            n_notes=n_notes,
            audio_seconds=audio_seconds,
            elapsed_seconds=time.perf_counter() - t0,
            sheets_dir=sheets_dir if want_sheets else None,
            sheets_files=sheets_files,
            first_note_onset=min(onsets) if onsets else None,
            tempo_bpm=beat_grid.bpm if beat_grid is not None else None,
            audio_path=audio_path,
            out_dir=out_dir,
        )


def run_in_thread(
    msg_queue: "queue.Queue[tuple]",
    make_transcriber: Callable[[], ScoreTranscriber],
    bilibili_url: str | None = None,
    **kwargs,
) -> None:
    """线程入口：把日志/进度/结果全部塞进队列，由 GUI 线程轮询。"""
    writer = _QueueWriter(msg_queue)
    try:
        with contextlib.redirect_stderr(writer):
            if bilibili_url:
                # 输出目录未指定时，下载完成拿到标题后再生成（downloads/<标题>_score）
                dl_dir = kwargs.get("out_dir") or "downloads"
                kwargs["audio_path"] = download_bilibili(
                    bilibili_url, dl_dir,
                    on_log=lambda s: msg_queue.put(("log", s)),
                )
                if kwargs.get("out_dir") is None:
                    wav = Path(kwargs["audio_path"])
                    kwargs["out_dir"] = wav.parent / (wav.stem + "_score")
            transcriber = make_transcriber()
            msg_queue.put(("log", f"加载模型（{transcriber.model_size}）…首次使用需下载权重，请稍候"))
            result = transcriber.transcribe(
                on_progress=lambda d, t: msg_queue.put(("progress", d, t)),
                on_log=lambda s: msg_queue.put(("log", s)),
                **kwargs,
            )
        writer.flush()
        msg_queue.put(("done", result))
    except Exception as e:  # noqa: BLE001 —— 全部转交 GUI 显示
        writer.flush()
        msg_queue.put(("error", f"{type(e).__name__}: {e}"))
