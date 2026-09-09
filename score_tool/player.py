"""乐谱跟随播放器：verovio 排版乐谱，播放原音频并实时高亮当前音符。

流程：MusicXML → verovio 排版（SVG + 时间映射）→ SVG 预处理（解嵌套、
补描边）→ Qt 离屏渲染成 PNG → tkinter Canvas 分页展示；
播放时按音频进度查询 verovio 的 getElementsAtTime，把当前音符框高亮。
"""

from __future__ import annotations

import io
import math
import re
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import verovio

_SVG_OPEN_RE = re.compile(r'<svg class="definition-scale"[^>]*?viewBox="([^"]+)"[^>]*>')
_TRANSFORM_RE = re.compile(r"(translate|scale)\(([^)]*)\)")
_STROKE_TAG_RE = re.compile(r"<(?:path|ellipse|polygon|polyline|rect|line)\b[^>]*/?>")


def unwrap_verovio_svg(svg: str) -> str:
    """合并 verovio 的嵌套 <svg>（Qt 渲染嵌套 svg 会输出空白）。

    外层 <svg> 只有宽高，内层 <svg class="definition-scale"> 才带 viewBox；
    把 viewBox 提到单层根元素上。
    """
    m = _SVG_OPEN_RE.search(svg)
    if not m:
        return svg
    vb = m.group(1)
    close_idx = svg.index("</svg>", m.end())  # 内层不再嵌套 svg，其闭标签即下一个
    svg = svg[:close_idx] + svg[close_idx + len("</svg>") :]
    svg = svg[: m.start()] + svg[m.end() :]
    svg = re.sub(
        r"<svg [^>]*>",
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="{vb}">',
        svg,
        count=1,
    )
    return svg


def bake_strokes(svg: str) -> str:
    """verovio 用 CSS（stroke:currentColor）给谱线等描边，Qt 不读 CSS，
    给带 stroke-width 但无 stroke 属性的元素补黑色描边。"""

    def fix(m: re.Match) -> str:
        tag = m.group(0)
        if "stroke=" in tag:
            return tag
        if tag.endswith("/>"):
            return tag[:-2] + ' stroke="black"/>'
        return tag[:-1] + ' stroke="black">'

    return _STROKE_TAG_RE.sub(fix, svg)


def _floats(s: str) -> list[float]:
    return [float(v) for v in re.split(r"[,\s]+", s.strip()) if v]


def parse_svg_positions(svg_text: str) -> tuple[float, float, dict[str, tuple[float, float]]]:
    """从单页 SVG 解析 (页面宽, 页面高, {元素 id: 中心坐标})，坐标为 viewBox 单位。

    音符/和弦/休止符的中心取其第一个带 translate 的后代（即符头的 <use>）。
    """
    root = ET.fromstring(svg_text)
    vb = _floats(root.get("viewBox", "0 0 0 0"))
    width, height = vb[2], vb[3]
    positions: dict[str, tuple[float, float]] = {}

    def walk(el: ET.Element, sx: float, sy: float, tx: float, ty: float) -> None:
        for kind, args in _TRANSFORM_RE.findall(el.get("transform", "")):
            vals = _floats(args)
            if kind == "translate":
                tx += sx * vals[0]
                ty += sy * (vals[1] if len(vals) > 1 else 0.0)
            else:
                sx *= vals[0]
                sy *= vals[1] if len(vals) > 1 else vals[0]
        eid = el.get("id")
        if eid:
            for desc in el.iter():
                if desc is el:
                    continue
                m = re.search(r"translate\(([^)]*)\)", desc.get("transform", ""))
                if m:
                    vals = _floats(m.group(1))
                    positions[eid] = (
                        tx + sx * vals[0],
                        ty + sy * (vals[1] if len(vals) > 1 else 0.0),
                    )
                    break
        for child in el:
            walk(child, sx, sy, tx, ty)

    walk(root, 1.0, 1.0, 0.0, 0.0)
    return width, height, positions


def _sanitize_musicxml(text: str) -> str:
    """精简 score-part 的打击乐器清单。

    MuseScore 导出的鼓组声部会给每件打击乐器一个 score-instrument 条目
    （可达 40+），verovio 因此静默拒绝加载。每个声部最多保留 8 条；
    音符里悬空的 instrument 引用 verovio 能容忍。
    """
    import re as _re

    def trim_part(m: "_re.Match") -> str:
        body = m.group(0)
        si = list(_re.finditer(r'<score-instrument id="[^"]+">.*?</score-instrument>', body, _re.S))
        if len(si) <= 8:
            return body
        keep_ids = set()
        for s in si[:8]:
            idm = _re.search(r'id="([^"]+)"', s.group(0))
            if idm:
                keep_ids.add(idm.group(1))
        body = _re.sub(
            r'<score-instrument id="([^"]+)">.*?</score-instrument>',
            lambda mm: mm.group(0) if mm.group(1) in keep_ids else "",
            body,
            flags=_re.S,
        )
        body = _re.sub(r'<midi-device[^>]*/>|<midi-device[^>]*></midi-device>', "", body)
        body = _re.sub(
            r'<midi-instrument id="([^"]+)">.*?</midi-instrument>',
            lambda mm: mm.group(0) if mm.group(1) in keep_ids else "",
            body,
            flags=_re.S,
        )
        return body

    return _re.sub(r'<score-part id="[^"]+">.*?</score-part>', trim_part, text, flags=_re.S)


class ScoreModel:
    """一份乐谱的 verovio 排版结果：页面 SVG、元素位置表、时间映射。

    注意：构造（toolkit + loadFile）必须在主线程执行——verovio 的字体资源
    在首次 loadFile 时初始化，在工作线程中会失败。之后的 renderToSVG /
    getElementsAtTime 等调用可以安全地放到工作线程（见 build_pages）。
    """

    def __init__(self, musicxml_path: str | Path):
        self.tk = verovio.toolkit()
        text = Path(musicxml_path).read_text(encoding="utf-8")
        if not self.tk.loadData(_sanitize_musicxml(text)):
            raise ValueError(f"verovio 无法加载 {musicxml_path}")
        self.page_count = self.tk.getPageCount()
        self.pages: list[tuple[str, float, float, dict[str, tuple[float, float]]]] = []

    def build_pages(self) -> None:
        """渲染所有页 SVG 并解析元素位置（可放在工作线程）。"""
        self.pages = []
        for p in range(1, self.page_count + 1):
            svg = bake_strokes(unwrap_verovio_svg(self.tk.renderToSVG(p)))
            w, h, positions = parse_svg_positions(svg)
            self.pages.append((svg, w, h, positions))

    def elements_at(self, ms: float) -> dict:
        """当前时刻发声的元素（notes/chords/rests/measure/page）。"""
        return self.tk.getElementsAtTime(max(0, int(ms)))

    def time_for_element(self, eid: str) -> float | None:
        """元素的发声时刻（ms），查不到返回 None。"""
        t = self.tk.getTimeForElement(eid)
        return float(t) if t >= 0 else None

    def first_note_time_ms(self) -> float:
        """第一个音符的谱面时刻（ms），用于音谱对齐。"""
        tm = self.tk.renderToTimemap()
        for entry in tm:
            t = entry.get("tstamp")
            if t is None:
                continue
            el = self.elements_at(t + 1)
            if el.get("notes"):
                return float(t)
        return 0.0


class QtSvgRasterizer:
    """用 Qt 把 SVG 渲染成 PNG 字节（懒加载单例 QGuiApplication）。"""

    _app = None

    @classmethod
    def _ensure_app(cls):
        if cls._app is None:
            from PySide6.QtGui import QGuiApplication

            cls._app = QGuiApplication.instance() or QGuiApplication([])
        return cls._app

    @classmethod
    def to_png(cls, svg_text: str, width_px: int) -> tuple[bytes, int, int]:
        cls._ensure_app()
        from PySide6.QtCore import QByteArray, QBuffer, QRectF
        from PySide6.QtGui import QImage, QPainter, QColor
        from PySide6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
        if not renderer.isValid():
            raise ValueError("SVG 无效，Qt 无法渲染")
        size = renderer.defaultSize()
        height_px = max(1, round(width_px * size.height() / max(size.width(), 1)))
        img = QImage(width_px, height_px, QImage.Format.Format_RGB32)
        img.fill(QColor("white"))
        painter = QPainter(img)
        renderer.render(painter, QRectF(0, 0, width_px, height_px))
        painter.end()
        buf = QBuffer()
        buf.open(QBuffer.OpenModeFlag.ReadWrite)
        img.save(buf, "PNG")
        return bytes(buf.data()), width_px, height_px


def decode_audio(path: str | Path) -> tuple[np.ndarray, int]:
    """读音频为 float32 数组（[n, channels]）；soundfile 不认的格式用 ffmpeg 兜底。"""
    import soundfile as sf

    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        return data, sr
    except Exception:
        tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-ac", "2", "-ar", "44100", str(tmp)],
                check=True,
            )
            data, sr = sf.read(str(tmp), dtype="float32", always_2d=True)
            return data, sr
        finally:
            tmp.unlink(missing_ok=True)


def estimate_first_onset(data: np.ndarray, sample_rate: int) -> float:
    """估计首个音符 onset：RMS 包络首次超过最大值 5% 的时刻（秒）。"""
    mono = data.mean(axis=1) if data.ndim > 1 else data
    win = 2048
    env = np.sqrt(np.convolve(mono ** 2, np.ones(win) / win, "same"))
    peak = env.max()
    if peak <= 0:
        return 0.0
    idx = int(np.argmax(env > 0.05 * peak))
    return idx / sample_rate


# 键盘高亮配色（按声部循环）
KB_PALETTE = ["#e0245e", "#1a7f37", "#0366d6", "#f0883e", "#8a3ffc", "#d29922", "#39c5cf", "#ff7b72"]
_WHITE_PC = (0, 2, 4, 5, 7, 9, 11)


class PianoKeyboard:
    """88 键钢琴键盘：按声部分色点亮当前发声的键。"""

    FIRST, LAST = 21, 108  # A0–C8

    def __init__(self, tk_mod, parent, height: int = 110):
        self._tk = tk_mod
        self.canvas = tk_mod.Canvas(parent, height=height, bg="#dddddd", highlightthickness=0)
        self.canvas.pack(fill=tk_mod.X)
        self._items: dict[int, int] = {}
        self._active: dict[int, int] = {}
        self._drawn_w = 0
        self.canvas.bind("<Configure>", lambda _e: self._draw())

    def _draw(self) -> None:
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 50 or w == self._drawn_w:
            return
        self._drawn_w = w
        self.canvas.delete("all")
        self._items.clear()
        whites = [p for p in range(self.FIRST, self.LAST + 1) if p % 12 in _WHITE_PC]
        wk_w = w / len(whites)
        white_x: dict[int, tuple[float, float]] = {}
        for i, p in enumerate(whites):
            x0 = i * wk_w
            white_x[p] = (x0, wk_w)
            self._items[p] = self.canvas.create_rectangle(
                x0, 0, x0 + wk_w, h, fill="white", outline="#999999"
            )
            if p % 12 == 0:  # C 键标注
                self.canvas.create_text(
                    x0 + 2, h - 11, anchor="nw", text=f"C{p // 12 - 1}",
                    font=("Arial", 6), fill="#999999",
                )
        for p in range(self.FIRST, self.LAST + 1):
            if p % 12 in _WHITE_PC:
                continue
            below = p - 1
            if below not in white_x:
                continue
            x0, wk = white_x[below]
            bx = x0 + wk - wk * 0.3
            self._items[p] = self.canvas.create_rectangle(
                bx, 0, bx + wk * 0.6, h * 0.62, fill="#222222", outline="#000000"
            )

    def set_active(self, active: list[tuple[int, int]]) -> None:
        """点亮/熄灭按键。active: [(pitch, track_idx)]。"""
        new = {p: ti for p, ti in active if self.FIRST <= p <= self.LAST}
        for p in set(self._active) - set(new):
            if p in self._items:
                self.canvas.itemconfigure(
                    self._items[p],
                    fill="white" if p % 12 in _WHITE_PC else "#222222",
                )
        for p, ti in new.items():
            if p not in self._active and p in self._items:
                self.canvas.itemconfigure(self._items[p], fill=KB_PALETTE[ti % len(KB_PALETTE)])
        self._active = new

    def clear(self) -> None:
        self.set_active([])


def build_pitch_slots(tracks, beat_s: float) -> tuple[dict[int, list[tuple[int, int]]], float]:
    """把各声部音符展开成 16 分槽位 → [(pitch, track_idx)]，供 O(1) 查询。

    鼓声部跳过：鼓点音高在钢琴键盘上没有意义。
    """
    slot_s = beat_s / 4
    slots: dict[int, list[tuple[int, int]]] = {}
    for ti, track in enumerate(tracks):
        if getattr(track, "is_drum", False):
            continue
        for start, end, pitch in track.notes:
            i0 = int(start / slot_s)
            i1 = max(i0 + 1, math.ceil(end / slot_s))
            for i in range(i0, i1):
                slots.setdefault(i, []).append((pitch, ti))
    return slots, slot_s


class GuitarBoard:
    """吉他/贝斯指板演示：点亮当前按下的 (弦, 品位)。"""

    STRING_GAP = 18

    def __init__(self, tk_mod, parent, label: str, string_names: list[str], color: str, n_frets: int = 19):
        self._tk = tk_mod
        self.color = color
        self.string_names = string_names
        self.n_frets = n_frets
        self.frame = tk_mod.Frame(parent)
        tk_mod.Label(self.frame, text=label, font=("Microsoft YaHei UI", 8), foreground="#666666").pack(
            anchor="w", padx=4
        )
        h = (len(string_names) - 1) * self.STRING_GAP + 46
        self.canvas = tk_mod.Canvas(self.frame, height=h, bg="#f6f2ea", highlightthickness=0)
        self.canvas.pack(fill=tk_mod.X)
        self._dots: list[int] = []
        self._drawn_w = 0
        self._geom = None
        self.canvas.bind("<Configure>", lambda _e: self._draw())

    def _draw(self) -> None:
        w = self.canvas.winfo_width()
        if w < 80 or w == self._drawn_w:
            return
        self._drawn_w = w
        self.canvas.delete("all")
        self._dots.clear()
        margin_l, margin_r = 46, 12
        top = 12
        ys = [top + i * self.STRING_GAP for i in range(len(self.string_names))]
        fret_w = (w - margin_l - margin_r) / self.n_frets
        for i, nm in enumerate(self.string_names):
            self.canvas.create_text(margin_l - 26, ys[i], text=nm, font=("Arial", 8), fill="#666666", anchor="e")
            self.canvas.create_line(margin_l, ys[i], w - margin_r, ys[i], fill="#333333")
        for f in range(self.n_frets + 1):  # 品丝（琴枕加粗）
            x = margin_l + f * fret_w
            self.canvas.create_line(x, top - 4, x, ys[-1] + 4, fill="#333333", width=3 if f == 0 else 1)
        mid_y = (ys[0] + ys[-1]) / 2  # 品位标记点
        for f in (3, 5, 7, 9, 15, 17, 19):
            if f <= self.n_frets:
                x = margin_l + f * fret_w - fret_w / 2
                self.canvas.create_oval(x - 3, mid_y - 3, x + 3, mid_y + 3, fill="#c9c2b4", outline="")
        if 12 <= self.n_frets:
            x = margin_l + 12 * fret_w - fret_w / 2
            for dy in (-9, 9):
                self.canvas.create_oval(x - 3, mid_y + dy - 3, x + 3, mid_y + dy + 3, fill="#c9c2b4", outline="")
        for f in (1, 3, 5, 7, 9, 12, 15, 17, 19):  # 品位数字
            if f <= self.n_frets:
                self.canvas.create_text(margin_l + f * fret_w - fret_w / 2, ys[-1] + 14,
                                        text=str(f), font=("Arial", 7), fill="#999999")
        self._geom = (margin_l, fret_w, ys)

    def set_active(self, positions: list[tuple[int, str]]) -> None:
        for d in self._dots:
            self.canvas.delete(d)
        self._dots.clear()
        if self._geom is None:
            return
        margin_l, fret_w, ys = self._geom
        for si, text in positions:
            if not 0 <= si < len(ys):
                continue
            if text.isdigit():
                f = int(text)
                x = margin_l + f * fret_w - fret_w / 2 if f > 0 else margin_l - 8
                r = 7
                self._dots.append(
                    self.canvas.create_oval(x - r, ys[si] - r, x + r, ys[si] + r,
                                            fill=self.color, outline="#333333")
                )
            else:  # 无法按出的音：琴枕左侧画 ×
                self._dots.append(
                    self.canvas.create_text(margin_l - 8, ys[si], text="×",
                                            font=("Arial", 9, "bold"), fill="#cc0000")
                )

    def clear(self) -> None:
        self.set_active([])


class AudioPlayer:
    """sounddevice 回调式播放器，支持暂停/继续/跳转与精确位置。"""

    def __init__(self, data: np.ndarray, sample_rate: int):
        import sounddevice as sd

        self._sd = sd
        self.data = data
        self.sr = sample_rate
        self.channels = data.shape[1]
        self.duration = len(data) / sample_rate
        self.pos = 0  # 帧
        self.stream: sd.OutputStream | None = None
        self.playing = False
        self.on_eof = None  # 播放到末尾时的回调（在音频线程触发，仅置标志）

    def _callback(self, outdata, frames, _time, _status):
        end = min(self.pos + frames, len(self.data))
        n = end - self.pos
        if n > 0:
            outdata[:n] = self.data[self.pos : end]
        if n < frames:
            outdata[n:] = 0
            self.pos = len(self.data)
            if self.on_eof is not None:
                self.on_eof()
            raise self._sd.CallbackStop
        self.pos = end

    def play(self) -> None:
        if self.playing or self.pos >= len(self.data):
            if self.pos >= len(self.data):
                self.pos = 0
            else:
                return
        self.stream = self._sd.OutputStream(
            samplerate=self.sr, channels=self.channels, callback=self._callback
        )
        self.stream.start()
        self.playing = True

    def pause(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.playing = False

    def stop(self) -> None:
        self.pause()
        self.pos = 0

    def seek_seconds(self, s: float) -> None:
        self.pos = int(max(0.0, min(s, self.duration)) * self.sr)
        if self.pos >= len(self.data):
            self.pos = max(0, len(self.data) - 1)

    def position_seconds(self) -> float:
        return self.pos / self.sr


class ScorePlayer:
    """tkinter 乐谱跟随播放器（挂载到 Notebook 的 tab 里）。"""

    PAGE_GAP = 24

    def __init__(self, tk_root, parent):
        import tkinter as tk
        from tkinter import ttk

        self._tk = tk
        self.ttk = ttk
        self.root = tk_root
        self.frame = ttk.Frame(parent)
        self.model: ScoreModel | None = None
        self.audio: AudioPlayer | None = None
        self.offset_s = 0.0  # 音频时间 → 谱面时间的偏移（音频 t - offset = 谱面 t）
        self.zoom = 1.0
        self._page_images: list = []  # 防 GC 的 PhotoImage 引用
        self._page_y: list[float] = []  # 每页在 canvas 中的 y 起点
        self._page_scale: list[float] = []  # 每页 px / viewBox 单位
        self._page_positions: list[dict] = []
        self._highlight_items: list[int] = []
        self._timer_on = False
        self._seek_dragging = False
        self._eof_pending = False
        self.chord_spans: list = []
        self._cur_chord_idx: int = -1
        self._chord_overlay: dict[int, int] = {}  # 和弦段序号 → 画布文本项
        self._strip_ranges: list[tuple[str, str]] = []  # 和弦条中每段的字符区间
        self.notation = "staff"  # "staff" 五线谱 | "jianpu" 简谱 | "tab" 吉他谱
        self._jp = None  # JianpuScore（有量化 MIDI 时构建）
        self._jp_layout = None  # 当前简谱布局
        self._tab = None  # GuitarTabScore（含吉他/贝斯声部时非空）
        self._tab_layout = None  # 当前 TAB 布局
        self._pitch_slots: dict = {}  # 16 分槽位 → [(pitch, track_idx)]
        self._tab_slots: dict = {}  # 16 分槽位 → [(tab_track_idx, string_idx, fret)]
        self._slot_s = 0.125
        self._boards: list = []  # GuitarBoard 列表（按 TAB 声部）
        self._last_fit: float | None = None  # 上次渲染的可用宽度（tab 显示时校验重排用）

        self._build_ui()
        self.show_placeholder("转录完成后，乐谱会显示在这里")
        # 快捷键：空格播放/暂停，←/→ 快退/快进 5 秒
        self.root.bind("<space>", self._on_space)
        self.root.bind("<Left>", lambda _e: self._seek_rel(-5.0))
        self.root.bind("<Right>", lambda _e: self._seek_rel(5.0))

    def _focus_is_text_input(self) -> bool:
        w = self.root.focus_get()
        return w is not None and w.winfo_class() in (
            "Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "ScrolledText",
            "Button", "TButton", "Checkbutton", "TCheckbutton",
        )

    def _on_space(self, _e) -> None:
        if not self._focus_is_text_input():
            self.toggle_play()

    def _seek_rel(self, delta: float) -> None:
        if self.audio is not None and not self._focus_is_text_input():
            self.audio.seek_seconds(self.audio.position_seconds() + delta)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        tk, ttk = self._tk, self.ttk

        bar = ttk.Frame(self.frame)
        bar.pack(fill=tk.X, padx=6, pady=4)
        self.play_btn = ttk.Button(bar, text="▶ 播放", command=self.toggle_play, width=8)
        self.play_btn.pack(side=tk.LEFT, padx=2)
        self.stop_btn = ttk.Button(bar, text="⏹ 停止", command=self.stop, width=8)
        self.stop_btn.pack(side=tk.LEFT, padx=2)
        self.time_label = ttk.Label(bar, text="0:00 / 0:00", width=16)
        self.time_label.pack(side=tk.LEFT, padx=8)
        self.progress = ttk.Scale(bar, from_=0, to=1000, orient=tk.HORIZONTAL)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.progress.bind("<ButtonPress-1>", lambda _e: setattr(self, "_seek_dragging", True))
        self.progress.bind("<ButtonRelease-1>", self._on_seek_release)

        ttk.Label(bar, text="对齐：").pack(side=tk.LEFT, padx=(10, 2))
        ttk.Button(bar, text="−50ms", width=6, command=lambda: self.nudge_offset(-0.05)).pack(side=tk.LEFT)
        self.offset_label = ttk.Label(bar, text="0ms", width=7)
        self.offset_label.pack(side=tk.LEFT)
        ttk.Button(bar, text="+50ms", width=6, command=lambda: self.nudge_offset(0.05)).pack(side=tk.LEFT)

        ttk.Label(bar, text="缩放：").pack(side=tk.LEFT, padx=(10, 2))
        self.zoom_var = tk.StringVar(value="100%")
        zoom = ttk.Combobox(
            bar, textvariable=self.zoom_var, width=6, state="readonly",
            values=("60%", "80%", "100%", "125%", "150%"),
        )
        zoom.pack(side=tk.LEFT)
        zoom.bind("<<ComboboxSelected>>", lambda _e: self._on_zoom())

        ttk.Label(bar, text="记谱：").pack(side=tk.LEFT, padx=(10, 2))
        self.notation_var = tk.StringVar(value="五线谱")
        ncb = ttk.Combobox(
            bar, textvariable=self.notation_var, width=6, state="readonly",
            values=("五线谱", "简谱", "吉他谱"),
        )
        ncb.pack(side=tk.LEFT)
        ncb.bind("<<ComboboxSelected>>", lambda _e: self._on_notation())

        self.export_tab_btn = ttk.Button(
            bar, text="导出 TAB", command=self._export_tab, width=9, state=tk.DISABLED
        )
        self.export_tab_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="打开乐谱…", command=self._open_existing, width=9).pack(side=tk.LEFT, padx=2)

        # —— 和弦提示条 ——
        chord_bar = ttk.Frame(self.frame)
        chord_bar.pack(fill=tk.X, padx=6, pady=(0, 2))
        self.key_label = ttk.Label(chord_bar, text="", foreground="#666")
        self.key_label.pack(side=tk.LEFT, padx=(2, 10))
        ttk.Label(chord_bar, text="和弦：").pack(side=tk.LEFT)
        self.chord_label = ttk.Label(
            chord_bar, text="—", width=8, anchor="center",
            font=("Microsoft YaHei UI", 13, "bold"), foreground="#1a7f37",
        )
        self.chord_label.pack(side=tk.LEFT, padx=4)
        self.chord_strip = tk.Text(
            chord_bar, height=1, wrap="none", borderwidth=0,
            highlightthickness=0, font=("Microsoft YaHei UI", 9),
            state=tk.DISABLED, cursor="arrow", exportselection=False,
        )
        self.chord_strip.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.chord_strip.tag_configure("cur", background="#ffe08a", font=("Microsoft YaHei UI", 9, "bold"))
        self.chord_strip.tag_configure("dim", foreground="#999")
        self.chord_strip.bind("<Button-1>", self._on_strip_click)
        self.kb_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            chord_bar, text="钢琴键盘", variable=self.kb_var, command=self._toggle_keyboard
        ).pack(side=tk.RIGHT, padx=4)
        self.gb_var = tk.BooleanVar(value=True)
        self.gb_check = ttk.Checkbutton(
            chord_bar, text="吉他指板", variable=self.gb_var, command=self._toggle_boards
        )
        self.gb_check.pack(side=tk.RIGHT, padx=4)

        self.canvas = tk.Canvas(self.frame, bg="#888888", highlightthickness=0)
        hscroll = ttk.Scrollbar(self.frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        hscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.gb_frame = ttk.Frame(self.frame)
        self.gb_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.kb_frame = ttk.Frame(self.frame)
        self.kb_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.keyboard = PianoKeyboard(tk, self.kb_frame)
        vscroll = ttk.Scrollbar(self.frame, orient=tk.VERTICAL, command=self.canvas.yview)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self._resize_after = None

    def show_placeholder(self, text: str) -> None:
        self.canvas.delete("all")
        self.canvas.create_text(
            30, 30, anchor="nw", text=text, fill="white",
            font=("Microsoft YaHei UI", 12),
        )

    # ------------------------------------------------------------------ 加载
    def load_score(
        self,
        musicxml: str | Path,
        audio: str | Path,
        first_onset_s: float | None = None,
        status_cb=None,
        midi_path: str | Path | None = None,
    ) -> None:
        """后台线程加载乐谱与音频，完成后在 GUI 线程逐页渲染。"""
        import queue as _queue

        self._status_cb = status_cb or (lambda s: None)
        self._load_q: _queue.Queue[tuple] = _queue.Queue()
        self._audio_name = Path(audio).stem

        try:
            model = ScoreModel(musicxml)  # GUI 线程：verovio 字体初始化要求
        except Exception as e:  # noqa: BLE001
            self.show_placeholder(f"乐谱加载失败：{e}")
            return

        def work():
            try:
                model.build_pages()
                data, sr = decode_audio(audio)
                onset = first_onset_s if first_onset_s is not None else estimate_first_onset(data, sr)
                first_ms = model.first_note_time_ms()
                offset = onset - first_ms / 1000.0
                chords, key = [], ""
                jp = None
                tab = None
                pitch_slots, tab_slots, slot_s = {}, {}, 0.125
                if midi_path and Path(midi_path).is_file():
                    from score_tool.chords import detect_chords, estimate_key, parse_midi_notes
                    from score_tool.jianpu import JianpuScore
                    from score_tool.guitartab import GuitarTabScore

                    notes, bpm, bpb = parse_midi_notes(str(midi_path))
                    chords = detect_chords(notes, bpm, bpb)
                    key = estimate_key(notes)
                    jp = JianpuScore(str(midi_path), key)
                    tab = GuitarTabScore(str(midi_path))
                    pitch_slots, slot_s = build_pitch_slots(jp.tracks, 60.0 / bpm)
                    if tab.tracks:
                        tab_slots, _ = tab.build_tab_slots(60.0 / bpm)
                self._load_q.put(("ready", model, data, sr, offset, chords, key, jp, tab, pitch_slots, tab_slots, slot_s))
            except Exception as e:  # noqa: BLE001
                self._load_q.put(("error", e))

        self._status_cb("排版乐谱中…")
        self._load_thread = threading.Thread(target=work, daemon=True)
        self._load_thread.start()
        self.root.after(100, self._poll_load)

    def _poll_load(self) -> None:
        import queue as _queue

        try:
            kind, *payload = self._load_q.get_nowait()
        except _queue.Empty:
            if self._load_thread.is_alive():
                self.root.after(100, self._poll_load)
            return
        if kind == "ready":
            self._on_model_ready(*payload)
        else:
            self.show_placeholder(f"乐谱加载失败：{payload[0]}")

    def _on_model_ready(self, model, data, sr, offset, chords, key, jp, tab, pitch_slots, tab_slots, slot_s) -> None:
        self.stop()
        self.model = model
        self.audio = AudioPlayer(data, sr)
        self.audio.on_eof = lambda: setattr(self, "_eof_pending", True)
        self.offset_s = offset
        self.offset_label.config(text=f"{round(offset * 1000):+d}ms")
        self.chord_spans = chords
        self._jp = jp
        self._jp_layout = None
        self._tab = tab
        self._tab_layout = None
        self._pitch_slots = pitch_slots
        self._tab_slots = tab_slots
        self._slot_s = slot_s
        self._cur_chord_idx = -1
        self.key_label.config(text=f"调性：{key}" if key else "")
        self.export_tab_btn.config(
            state=self._tk.NORMAL if (tab is not None and tab.tracks) else self._tk.DISABLED
        )
        # 吉他指板演示板
        for w in self.gb_frame.winfo_children():
            w.destroy()
        self._boards = []
        if tab is not None and tab.tracks:
            for ti, (track, _tuning, names) in enumerate(tab.tracks):
                color = KB_PALETTE[tab.source_indices[ti] % len(KB_PALETTE)]
                board = GuitarBoard(self._tk, self.gb_frame, track.name, names, color)
                board.frame.pack(fill=self._tk.X, pady=1)
                self._boards.append(board)
        if self._boards:
            self.gb_var.set(True)
            self.gb_check.config(state=self._tk.NORMAL)
            self.gb_frame.pack(side=self._tk.BOTTOM, fill=self._tk.X)
        else:
            self.gb_var.set(False)
            self.gb_check.config(state=self._tk.DISABLED)
            self.gb_frame.pack_forget()
        self._build_chord_strip()
        self._render_pages(self._status_cb)
        self._status_cb("就绪")
        self._start_timer()

    def _build_chord_strip(self) -> None:
        self.chord_strip.config(state=self._tk.NORMAL)
        self.chord_strip.delete("1.0", self._tk.END)
        self._strip_ranges = []
        self._strip_char_ranges = []
        for i, span in enumerate(self.chord_spans):
            label = span.label or "·"
            start_idx = self.chord_strip.index(self._tk.END + "-1c")
            self.chord_strip.insert(self._tk.END, (" " if i == 0 else " | ") + label)
            end_idx = self.chord_strip.index(self._tk.END + "-1c")
            self._strip_ranges.append((start_idx, end_idx))
            self._strip_char_ranges.append(
                (int(start_idx.split(".")[1]), int(end_idx.split(".")[1]))
            )
        self.chord_strip.config(state=self._tk.DISABLED)

    def _on_strip_click(self, e) -> None:
        """点击和弦进行中的某个和弦 → 跳到该段（与点击谱面一致，只跳转不自动播放）。"""
        if self.audio is None or not self.chord_spans:
            return
        char = int(self.chord_strip.index(f"@{e.x},{e.y}").split(".")[1])
        for i, (s, en) in enumerate(self._strip_char_ranges):
            if s <= char < en:
                self.audio.seek_seconds(self.chord_spans[i].start + self.offset_s)
                return

    def _render_pages(self, status=lambda s: None) -> None:
        """按当前记谱法与缩放渲染谱面（GUI 线程）。"""
        if self.model is None:
            return
        if self.notation == "jianpu":
            self._render_jianpu(status)
            return
        if self.notation == "tab":
            self._render_tab(status)
            return
        self.canvas.configure(bg="#888888")
        self.canvas.delete("all")
        self._page_images.clear()
        self._page_y.clear()
        self._page_scale.clear()
        self._page_positions.clear()
        self._highlight_items.clear()

        # 页宽跟随窗口（缩放百分比在此基础上放大）
        fit = self.canvas.winfo_width() - 2 * self.PAGE_GAP - 24
        self._last_fit = fit
        width_px = int(max(400, fit) * self.zoom)
        y = float(self.PAGE_GAP)
        for i, (svg, vbw, vbh, positions) in enumerate(self.model.pages):
            status(f"渲染乐谱第 {i + 1}/{self.model.page_count} 页…")
            png, w, h = QtSvgRasterizer.to_png(svg, width_px)
            img = self._tk.PhotoImage(data=png, format="png")
            self._page_images.append(img)
            x = self.PAGE_GAP
            self.canvas.create_image(x, y, anchor="nw", image=img, tags=("page",))
            self._page_y.append(y)
            self._page_scale.append(w / vbw)
            self._page_positions.append(positions)
            y += h + self.PAGE_GAP
        self.canvas.configure(scrollregion=(0, 0, width_px + 2 * self.PAGE_GAP, y))
        self.canvas.yview_moveto(0.0)
        self.canvas.xview_moveto(0.0)
        # 重绘后叠加和弦标记（画布内容已被清空，需重建）
        self._chord_overlay.clear()
        self._draw_chord_overlay()
        self._cur_chord_idx = -1  # 强制下次 tick 刷新高亮

    def _draw_chord_overlay(self) -> None:
        """把和弦标记画在谱面上（每段起始位置上方）。"""
        if self.notation == "jianpu":
            self._draw_chord_overlay_jianpu()
            return
        if self.notation == "tab":
            self._draw_chord_overlay_tab()
            return
        if not self.chord_spans or self.model is None or not self._page_positions:
            return
        for idx, span in enumerate(self.chord_spans):
            if not span.label:
                continue
            pos = self._position_for_time(span.start)
            if pos is None:
                continue
            item = self.canvas.create_text(
                pos[0], pos[1], text=span.label, anchor="s",
                font=("Microsoft YaHei UI", max(8, round(11 * self.zoom)), "bold"),
                fill="#8a8a8a",
            )
            self._chord_overlay[idx] = item

    def _draw_chord_overlay_jianpu(self) -> None:
        if not self.chord_spans or self._jp_layout is None:
            return
        for idx, span in enumerate(self.chord_spans):
            if not span.label:
                continue
            pos = self._jp_layout.beat_to_x(span.start)
            if pos is None:
                continue
            item = self.canvas.create_text(
                pos[0] + 4, pos[1] - 2, text=span.label, anchor="sw",
                font=("Microsoft YaHei UI", max(8, round(11 * self.zoom)), "bold"),
                fill="#8a8a8a",
            )
            self._chord_overlay[idx] = item

    def _draw_chord_overlay_tab(self) -> None:
        if not self.chord_spans or self._tab_layout is None:
            return
        for idx, span in enumerate(self.chord_spans):
            if not span.label:
                continue
            pos = self._tab_layout.beat_to_x(span.start)
            if pos is None:
                continue
            item = self.canvas.create_text(
                pos[0] + 4, pos[1] - 2, text=span.label, anchor="sw",
                font=("Microsoft YaHei UI", max(8, round(11 * self.zoom)), "bold"),
                fill="#8a8a8a",
            )
            self._chord_overlay[idx] = item

    def _position_for_time(self, t_s: float) -> tuple[float, float] | None:
        """谱面时间 → 画布坐标（该时刻发声音符的最左上位置；休止时向后探测）。"""
        assert self.model is not None
        for probe_ms in range(0, 3000, 100):
            el = self.model.elements_at(t_s * 1000.0 + probe_ms)
            notes = el.get("notes", [])
            if not notes:
                continue
            page = el.get("page", 1) - 1
            if not 0 <= page < len(self._page_positions):
                continue
            positions = self._page_positions[page]
            hits = [positions[n] for n in notes if n in positions]
            if not hits:
                continue
            scale = self._page_scale[page]
            x = self.PAGE_GAP + min(h[0] for h in hits) * scale
            # 标签放在音符上方，但不许超出页面顶部
            y = self._page_y[page] + max((min(h[1] for h in hits) - 750) * scale, 14.0)
            return x, y
        return None

    def _on_canvas_resize(self, _e) -> None:
        """窗口尺寸变化后按新宽度重排（防抖）。"""
        if self.model is None:
            return
        if self._resize_after is not None:
            self.root.after_cancel(self._resize_after)
        self._resize_after = self.root.after(350, self._render_pages)

    def on_tab_shown(self) -> None:
        """播放页 tab 变为可见时：若可用宽度与上次渲染不符则重排。

        首次加载在隐藏 tab 上会以很小的宽度渲染，切回时需按真实宽度重排。
        """
        if self.model is None or self._last_fit is None:
            return
        fit = self.canvas.winfo_width() - 2 * self.PAGE_GAP - 24
        if abs(fit - self._last_fit) > 4:
            self._render_pages()

    def _on_notation(self) -> None:
        self.notation = {"五线谱": "staff", "简谱": "jianpu", "吉他谱": "tab"}.get(
            self.notation_var.get(), "staff"
        )
        if self.model is not None:
            self._render_pages()

    def _export_tab(self) -> None:
        """导出文本六线谱（.txt）。"""
        from tkinter import filedialog, messagebox

        if self._tab is None or not self._tab.tracks:
            messagebox.showinfo("导出 TAB", "此曲未检测到吉他/贝斯声部。")
            return
        path = filedialog.asksaveasfilename(
            title="导出文本六线谱",
            defaultextension=".txt",
            initialfile=f"{self._audio_name}_tab.txt",
            filetypes=[("文本六线谱", "*.txt")],
        )
        if not path:
            return
        Path(path).write_text(self._tab.to_ascii(), encoding="utf-8")
        messagebox.showinfo("导出 TAB", f"已导出：{path}\n\n提示：sheets 目录里另有 MuseScore 排版的 TAB PDF。")

    def _open_existing(self) -> None:
        """打开一份已有的转录结果（score.musicxml + 音频），无需重新转录。"""
        from tkinter import filedialog, messagebox

        musicxml = filedialog.askopenfilename(
            title="选择 score.musicxml",
            filetypes=[("MusicXML", "*.musicxml *.mxl *.xml")],
        )
        if not musicxml:
            return
        musicxml_path = Path(musicxml)
        # 自动找同目录结构的音频：…/某曲_score/sheets/score.musicxml → …/某曲.wav
        audio: Path | None = None
        score_dir = musicxml_path.parent.parent
        wavs = sorted(score_dir.glob("*.wav")) + sorted(score_dir.parent.glob("*.wav"))
        if len(wavs) == 1:
            audio = wavs[0]
        else:
            picked = filedialog.askopenfilename(
                title="选择对应的音频文件",
                filetypes=[("音频文件", "*.wav *.mp3 *.flac *.m4a *.ogg"), ("所有文件", "*.*")],
            )
            if picked:
                audio = Path(picked)
        if audio is None:
            return
        midi = musicxml_path.parent / "score.mid"
        self.load_score(
            musicxml_path,
            audio=audio,
            first_onset_s=None,  # 用包络估计
            status_cb=self._status_cb,
            midi_path=midi if midi.is_file() else None,
        )

    def _render_tab(self, status=lambda s: None) -> None:
        """吉他谱渲染：布局后直接用画布矢量绘制。"""
        from score_tool.guitartab import draw_tab

        if self._tab is None or not self._tab.tracks:
            self.show_placeholder("此曲未检测到吉他/贝斯声部")
            return
        status("吉他谱排版中…")
        self.canvas.configure(bg="white")
        self.canvas.delete("all")
        self._page_images.clear()
        self._page_y.clear()
        self._page_scale.clear()
        self._page_positions.clear()
        self._highlight_items.clear()

        fit = self.canvas.winfo_width() - 2 * self.PAGE_GAP - 24
        self._last_fit = fit
        width_px = max(400, fit) * self.zoom
        self._tab_layout = self._tab.layout(width_px)
        draw_tab(self.canvas, self._tab_layout)
        self.canvas.configure(
            scrollregion=(0, 0, width_px + 2 * self.PAGE_GAP, self._tab_layout.total_height)
        )
        self.canvas.yview_moveto(0.0)
        self.canvas.xview_moveto(0.0)
        self._chord_overlay.clear()
        self._draw_chord_overlay()
        self._cur_chord_idx = -1

    def _render_jianpu(self, status=lambda s: None) -> None:
        """简谱渲染：布局后直接用画布矢量绘制。"""
        from score_tool.jianpu import draw_jianpu

        if self._jp is None:
            self.show_placeholder("简谱需要量化 MIDI（转录时勾选「生成乐谱」）")
            return
        status("简谱排版中…")
        self.canvas.configure(bg="white")
        self.canvas.delete("all")
        self._page_images.clear()
        self._page_y.clear()
        self._page_scale.clear()
        self._page_positions.clear()
        self._highlight_items.clear()

        fit = self.canvas.winfo_width() - 2 * self.PAGE_GAP - 24
        self._last_fit = fit
        width_px = max(400, fit) * self.zoom
        self._jp_layout = self._jp.layout(width_px)
        draw_jianpu(self.canvas, self._jp_layout)
        self.canvas.configure(
            scrollregion=(0, 0, width_px + 2 * self.PAGE_GAP, self._jp_layout.total_height)
        )
        self.canvas.yview_moveto(0.0)
        self.canvas.xview_moveto(0.0)
        self._chord_overlay.clear()
        self._draw_chord_overlay()
        self._cur_chord_idx = -1

    # ------------------------------------------------------------------ 播放
    def toggle_play(self) -> None:
        if self.audio is None:
            return
        if self.audio.playing:
            self.audio.pause()
            self.play_btn.config(text="▶ 播放")
            self.keyboard.clear()
            for b in self._boards:
                b.clear()
        else:
            self.audio.play()
            self.play_btn.config(text="⏸ 暂停")

    def stop(self) -> None:
        if self.audio is not None:
            self.audio.stop()
        self.play_btn.config(text="▶ 播放")
        self._clear_highlights()
        self._update_transport(0.0)
        self.keyboard.clear()
        for b in self._boards:
            b.clear()
        # 和弦提示复位
        self.chord_label.config(text="—")
        for s, e in self._strip_ranges:
            self.chord_strip.tag_remove("cur", s, e)
        for item in self._chord_overlay.values():
            self.canvas.itemconfig(item, fill="#8a8a8a")
        self._cur_chord_idx = -1

    def nudge_offset(self, delta: float) -> None:
        self.offset_s += delta
        self.offset_label.config(text=f"{round(self.offset_s * 1000):+d}ms")

    def _on_seek_release(self, _e) -> None:
        self._seek_dragging = False
        if self.audio is None:
            return
        frac = float(self.progress.get()) / 1000.0
        self.audio.seek_seconds(frac * self.audio.duration)

    def _on_zoom(self) -> None:
        try:
            self.zoom = int(self.zoom_var.get().rstrip("%")) / 100.0
        except ValueError:
            self.zoom = 1.0
        if self.model is not None:
            self._render_pages()

    def _on_wheel(self, e) -> None:
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    # ------------------------------------------------------------------ 跟随
    def _start_timer(self) -> None:
        if not self._timer_on:
            self._timer_on = True
            self._tick()

    def _tick(self) -> None:
        if self.audio is None or self.model is None:
            self._timer_on = False
            return
        if self._eof_pending:
            self._eof_pending = False
            self.audio.pause()
            self.audio.pos = 0
            self.play_btn.config(text="▶ 播放")
            self._clear_highlights()
        pos_s = self.audio.position_seconds()
        self._update_transport(pos_s)
        if self.audio.playing:
            score_ms = (pos_s - self.offset_s) * 1000.0
            el = self.model.elements_at(score_ms)
            if self.notation == "jianpu":
                self._highlight_jianpu(score_ms / 1000.0)
            elif self.notation == "tab":
                self._highlight_tab(score_ms / 1000.0)
            else:
                self._highlight(el.get("notes", []), el.get("page", 1))
            self._update_chord_display(score_ms / 1000.0)
            self._update_keyboard(score_ms / 1000.0)
            self._update_boards(score_ms / 1000.0)
        self.root.after(30, self._tick)

    def _update_boards(self, score_s: float) -> None:
        if not self._boards or not self._tab_slots:
            return
        active = self._tab_slots.get(int(score_s / self._slot_s), [])
        per_track: dict[int, list] = {}
        for ti, si, text in active:
            per_track.setdefault(ti, []).append((si, text))
        for ti, board in enumerate(self._boards):
            board.set_active(per_track.get(ti, []))

    def _toggle_boards(self) -> None:
        if self.gb_var.get():
            self.gb_frame.pack(side=self._tk.BOTTOM, fill=self._tk.X)
        else:
            self.gb_frame.pack_forget()

    def _update_keyboard(self, score_s: float) -> None:
        if not self._pitch_slots:
            return
        active = self._pitch_slots.get(int(score_s / self._slot_s), [])
        self.keyboard.set_active(active)

    def _toggle_keyboard(self) -> None:
        if self.kb_var.get():
            self.kb_frame.pack(side=self._tk.BOTTOM, fill=self._tk.X)
        else:
            self.kb_frame.pack_forget()

    def _highlight_tab(self, score_s: float) -> None:
        self._clear_highlights()
        if self._tab_layout is None:
            return
        idxs = self._tab_layout.glyphs_at(score_s)
        for i in idxs:
            g = self._tab_layout.glyphs[i]
            w = 5.5 * len(g.text) + 5
            self._highlight_items.append(
                self.canvas.create_rectangle(
                    g.x - w, g.y - 9, g.x + w, g.y + 9,
                    outline="#e0245e", width=2, fill="#e0245e", stipple="gray50",
                    tags=("hl",),
                )
            )
        if idxs:
            g0 = self._tab_layout.glyphs[idxs[0]]
            self._scroll_to(g0.x, g0.y)

    def _highlight_jianpu(self, score_s: float) -> None:
        self._clear_highlights()
        if self._jp_layout is None:
            return
        idxs = self._jp_layout.glyphs_at(score_s)
        for i in idxs:
            g = self._jp_layout.glyphs[i]
            self._highlight_items.append(
                self.canvas.create_rectangle(
                    g.x - 10, g.y - 12, g.x + 10, g.y + 13,
                    outline="#e0245e", width=2, fill="#e0245e", stipple="gray50",
                    tags=("hl",),
                )
            )
        if idxs:
            g0 = self._jp_layout.glyphs[idxs[0]]
            self._scroll_to(g0.x, g0.y)

    def _update_chord_display(self, score_s: float) -> None:
        idx = -1
        for i, span in enumerate(self.chord_spans):
            if span.start <= score_s < span.end:
                idx = i
                break
        if idx == self._cur_chord_idx:
            return
        # 清掉上一段的标记
        if 0 <= self._cur_chord_idx < len(self._strip_ranges):
            s, e = self._strip_ranges[self._cur_chord_idx]
            self.chord_strip.tag_remove("cur", s, e)
        old_item = self._chord_overlay.get(self._cur_chord_idx)
        if old_item is not None:
            self.canvas.itemconfig(old_item, fill="#8a8a8a")
        self._cur_chord_idx = idx
        if idx < 0:
            self.chord_label.config(text="—")
            return
        label = self.chord_spans[idx].label or "·"
        self.chord_label.config(text=label)
        s, e = self._strip_ranges[idx]
        self.chord_strip.tag_add("cur", s, e)
        self.chord_strip.see(s)
        item = self._chord_overlay.get(idx)
        if item is not None:
            self.canvas.itemconfig(item, fill="#e0245e")

    def _update_transport(self, pos_s: float) -> None:
        if self.audio is None:
            return
        dur = self.audio.duration
        fmt = lambda s: f"{int(s // 60)}:{int(s % 60):02d}"
        self.time_label.config(text=f"{fmt(pos_s)} / {fmt(dur)}")
        if not self._seek_dragging and dur > 0:
            self.progress.set(pos_s / dur * 1000.0)

    def _page_of_y(self, y_canvas: float) -> int:
        for i in range(len(self._page_y) - 1, -1, -1):
            if y_canvas >= self._page_y[i]:
                return i
        return 0

    def _highlight(self, note_ids: list[str], page: int) -> None:
        self._clear_highlights()
        if not note_ids or not self._page_positions:
            return
        centers: list[tuple[float, float]] = []  # canvas 坐标
        for pid in range(len(self._page_positions)):
            positions = self._page_positions[pid]
            scale = self._page_scale[pid]
            hits = [positions[n] for n in note_ids if n in positions]
            for cx, cy in hits:
                x = self.PAGE_GAP + cx * scale
                y = self._page_y[pid] + cy * scale
                centers.append((x, y))
                r = max(7.0, 200.0 * scale)
                self._highlight_items.append(
                    self.canvas.create_rectangle(
                        x - r, y - r, x + r, y + r,
                        outline="#e0245e", width=2,
                        fill="#e0245e", stipple="gray50",
                        tags=("hl",),
                    )
                )
        if centers:
            self._scroll_to(centers[0][0], centers[0][1])

    def _clear_highlights(self) -> None:
        for item in self._highlight_items:
            self.canvas.delete(item)
        self._highlight_items.clear()

    def _scroll_to(self, x_canvas: float, y_canvas: float) -> None:
        total = self.canvas.bbox("all")
        if not total:
            return
        # 垂直跟随：当前音符接近视口底部时翻页/滚动
        view_top = self.canvas.canvasy(0)
        view_h = self.canvas.winfo_height()
        if view_h > 1 and not (view_top < y_canvas < view_top + view_h * 0.8):
            total_h = max(total[3] - total[1], 1)
            self.canvas.yview_moveto(max(0.0, (y_canvas - view_h * 0.35) / total_h))
        # 水平跟随（缩放超过页宽时）
        view_left = self.canvas.canvasx(0)
        view_w = self.canvas.winfo_width()
        if view_w > 1 and not (view_left < x_canvas < view_left + view_w * 0.85):
            total_w = max(total[2] - total[0], 1)
            self.canvas.xview_moveto(max(0.0, (x_canvas - view_w * 0.3) / total_w))

    def _on_canvas_click(self, e) -> None:
        """点击音符附近 → 跳到该音符的发声时刻。"""
        if self.model is None or self.audio is None:
            return
        if self.notation == "jianpu":
            if self._jp_layout is None:
                return
            x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
            best, best_d = None, 30.0
            for g in self._jp_layout.glyphs:
                d = ((g.x - x) ** 2 + (g.y - y) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = g, d
            if best is not None:
                self.audio.seek_seconds(best.start_s + self.offset_s)
            return
        if self.notation == "tab":
            if self._tab_layout is None:
                return
            x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
            best, best_d = None, 25.0
            for g in self._tab_layout.glyphs:
                d = ((g.x - x) ** 2 + (g.y - y) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = g, d
            if best is not None:
                self.audio.seek_seconds(best.start_s + self.offset_s)
            return
        if not self._page_positions:
            return
        x = self.canvas.canvasx(e.x) - self.PAGE_GAP
        y = self.canvas.canvasy(e.y)
        pid = self._page_of_y(y)
        positions = self._page_positions[pid]
        scale = self._page_scale[pid]
        ly = y - self._page_y[pid]
        best, best_d = None, 40.0  # 40px 以内才算点中
        for eid, (cx, cy) in positions.items():
            d = ((cx * scale - x) ** 2 + (cy * scale - ly) ** 2) ** 0.5
            if d < best_d:
                best, best_d = eid, d
        if best is None:
            return
        t = self.model.time_for_element(best)
        if t is not None:
            self.audio.seek_seconds(t / 1000.0 + self.offset_s)
