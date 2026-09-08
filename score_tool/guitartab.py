"""吉他谱（六线谱/TAB）排版：从量化 MIDI 生成吉他/贝斯 TAB 画布布局。

音符 → (弦, 品位)：标准调弦（吉他 E2 A2 D3 G3 B3 E4，贝斯 E1 A1 D2 G2），
单音优先低把位并兼顾把位连续；和弦要求不同弦、把位跨度 ≤ 5 品。
只渲染吉他/贝斯声部（按 GM 乐器号判断），其余声部忽略。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from score_tool.jianpu import JianpuScore, _Track

# 调弦：显示顺序（高音弦在上）
GUITAR_TUNING = [64, 59, 55, 50, 45, 40]  # e4 B3 G3 D3 A2 E2
GUITAR_NAMES = ["e", "B", "G", "D", "A", "E"]
BASS_TUNING = [43, 38, 33, 28]  # G2 D2 A1 E1
BASS_NAMES = ["G", "D", "A", "E"]

GUITAR_PROGRAMS = set(range(24, 32))  # GM 24-31：各类吉他
BASS_PROGRAMS = set(range(32, 40))  # GM 32-39：各类贝斯

MAX_FRET = 19


def is_guitar(program: int | None) -> bool:
    return program in GUITAR_PROGRAMS


def is_bass(program: int | None) -> bool:
    return program in BASS_PROGRAMS


@dataclass
class TabGlyph:
    x: float
    y: float
    text: str  # 品位数字（"x" 表示无法按出的音）
    flags: int  # 节奏旗：0=四分及以上 1=八分 2=十六分
    start_s: float
    end_s: float
    stem: bool = False  # 是否画符干（和弦事件只在最高音上画一根）
    sys_top: float = 0.0  # 所属系统的最上弦 y（符干基准）


@dataclass
class TabSystem:
    y_top: float
    y_bottom: float
    t_start: float
    t_end: float
    tuning: list[int]
    string_names: list[str]
    label: str = ""
    measures: list[tuple[int, float, float, float]] = field(default_factory=list)
    # 每小节: (小节号, 起始秒, x, 宽)


@dataclass
class TabLayout:
    glyphs: list[TabGlyph]
    systems: list[TabSystem]
    total_height: float

    def glyphs_at(self, t: float) -> list[int]:
        return [i for i, g in enumerate(self.glyphs) if g.start_s <= t < g.end_s]

    def beat_to_x(self, t: float) -> tuple[float, float] | None:
        for sys in self.systems:
            if sys.t_start <= t < sys.t_end:
                x = sys.measures[0][2]
                for _no, m_start, m_x, _w in sys.measures:
                    if m_start <= t:
                        x = m_x
                    else:
                        break
                return x, sys.y_top
        return None


def _candidates(pitch: int, tuning: list[int]) -> list[tuple[int, int]]:
    """音高 → 可选 (弦号, 品位)。弦号 0 = 最上（高音）弦。"""
    return [(si, pitch - open_) for si, open_ in enumerate(tuning) if 0 <= pitch - open_ <= MAX_FRET]


def assign_frets(pitches: list[int], tuning: list[int], prev_mean: float) -> list[tuple[int, str]]:
    """一组同时发声的音 → [(弦号, 品位文本)]。

    单音：低把位优先 + 把位连续；和弦：不同弦、跨度 ≤ 5 品，小范围穷举。
    无法按出的音 → (-1, "x")。
    """
    pitches = sorted(set(pitches))
    cand = [_candidates(p, tuning) for p in pitches]
    result: dict[int, tuple[int, str]] = {}

    def fallback() -> dict[int, tuple[int, str]]:
        out = {}
        used: set[int] = set()
        for p, c in zip(pitches, cand):
            pick = None
            for si, f in sorted(c, key=lambda cf: (cf[1], cf[0])):
                if si not in used:
                    pick = (si, str(f))
                    used.add(si)
                    break
            out[p] = pick if pick else (-1, "x")
        return out

    if len(pitches) > 4 or any(not c for c in cand):
        # 太多音或存在无法按的音：贪心（无法按的标 x）
        return fallback()

    best: tuple[float, tuple] | None = None
    for combo in itertools.product(*cand):
        strings = [s for s, _f in combo]
        if len(set(strings)) < len(strings):
            continue
        frets = [f for _s, f in combo]
        span = max(frets) - min(frets) if len(frets) > 1 else 0
        if span > 5:
            continue
        mean = sum(frets) / len(frets)
        cost = mean + 0.3 * span + 0.4 * abs(mean - prev_mean) + sum(1.5 for f in frets if f > 12)
        if best is None or cost < best[0]:
            best = (cost, combo)
    if best is None:
        return fallback()
    return {p: (si, str(f)) for p, (si, f) in zip(pitches, best[1])}


class GuitarTabScore:
    """一份 MIDI 的吉他/贝斯 TAB 排版数据（无吉他声部时 tracks 为空）。"""

    MEASURES_PER_ROW = 4
    STRING_GAP = 13.0

    def __init__(self, midi_path: str):
        base = JianpuScore(midi_path)  # 复用解析（音轨/BPM/拍号）
        self.bpm = base.bpm
        self.bpb = base.bpb
        self.beat_s = base.beat_s
        self.measure_s = base.measure_s
        self.tracks: list[tuple[_Track, list[int], list[str]]] = []
        for track in base.tracks:
            if track.is_drum:
                continue
            if is_guitar(track.program):
                self.tracks.append((track, GUITAR_TUNING, GUITAR_NAMES))
            elif is_bass(track.program):
                self.tracks.append((track, BASS_TUNING, BASS_NAMES))

    # ------------------------------------------------------------------ 布局
    def layout(self, width_px: float) -> TabLayout:
        margin = 34.0  # 左侧留弦名
        content_w = max(200.0, width_px - 2 * margin)
        measure_w = content_w / self.MEASURES_PER_ROW
        beat_w = measure_w / self.bpb

        glyphs: list[TabGlyph] = []
        systems: list[TabSystem] = []
        y = 30.0

        for track, tuning, names in self.tracks:
            n_strings = len(tuning)
            row_h = (n_strings - 1) * self.STRING_GAP + 64.0  # 弦区 + 符干与小节号余量
            t_end = max(n[1] for n in track.notes)
            first_bar = int(math.floor(track.notes[0][0] / self.measure_s))
            last_bar = int(math.ceil(t_end / self.measure_s)) - 1
            rows = math.ceil((last_bar - first_bar + 1) / self.MEASURES_PER_ROW)

            for row in range(rows):
                bar0 = first_bar + row * self.MEASURES_PER_ROW
                bar1 = min(bar0 + self.MEASURES_PER_ROW - 1, last_bar)
                sys = TabSystem(
                    y_top=y,
                    y_bottom=y + row_h,
                    t_start=bar0 * self.measure_s,
                    t_end=(bar1 + 1) * self.measure_s,
                    tuning=tuning,
                    string_names=names,
                    label=track.name if row == 0 else "",
                )
                for bar in range(bar0, bar1 + 1):
                    sys.measures.append(
                        (bar + 1, bar * self.measure_s, margin + (bar - bar0) * measure_w, measure_w)
                    )
                self._layout_row(track, sys, beat_w, glyphs)
                systems.append(sys)
                y += row_h + 16.0

        return TabLayout(glyphs=glyphs, systems=systems, total_height=y + 16)

    def _layout_row(self, track: _Track, sys: TabSystem, beat_w: float, glyphs: list[TabGlyph]) -> None:
        unit = self.beat_s / 4
        string_y = [sys.y_top + 34.0 + i * self.STRING_GAP for i in range(len(sys.tuning))]
        prev_mean = 5.0

        for _no, m_start, m_x, _w in sys.measures:
            m_end = m_start + self.measure_s
            for key, chord in sorted(self._measure_events(track, m_start, m_end, unit).items()):
                start = key * unit
                end = min(max(n[1] for n in chord), m_end)
                dur_units = max(1, round((end - start) / unit))
                flags = 2 if dur_units <= 1 else (1 if dur_units <= 3 else 0)
                pitches = [p for _, _, p in chord]
                assigned = assign_frets(pitches, sys.tuning, prev_mean)
                frets = [int(f) for _, f in assigned.values() if f.isdigit()]
                if frets:
                    prev_mean = sum(frets) / len(frets)
                x = m_x + (start - m_start) / self.beat_s * beat_w
                for gi, pitch in enumerate(sorted(set(pitches), reverse=True)):  # 高音先画（在上弦）
                    si, text = assigned[pitch]
                    y = string_y[si] if si >= 0 else string_y[-1]
                    glyphs.append(
                        TabGlyph(
                            x=x, y=y, text=text,
                            flags=flags if gi == 0 else 0,
                            start_s=start, end_s=end,
                            stem=gi == 0,
                            sys_top=string_y[0],
                        )
                    )

    @staticmethod
    def _measure_events(
        track: _Track, m_start: float, m_end: float, unit: float
    ) -> dict[int, list[tuple[float, float, int]]]:
        """一小节内按开始时间（十六分网格）聚成事件的音符。"""
        events: dict[int, list[tuple[float, float, int]]] = {}
        for n in track.notes:
            if m_start - 1e-6 <= n[0] < m_end - 1e-6:
                events.setdefault(round(n[0] / unit), []).append(n)
        return events

    # ------------------------------------------------------------------ 文本导出
    def to_ascii(self, measures_per_row: int = 4) -> str:
        """导出文本六线谱（ASCII tab），每个吉他/贝斯声部一段。"""
        out: list[str] = []
        unit = self.beat_s / 4
        for track, tuning, names in self.tracks:
            out.append(f"# {track.name}（调弦：{' '.join(reversed(names))}，♩={round(self.bpm)}）")
            prev_mean = 5.0
            t_end = max(n[1] for n in track.notes)
            first_bar = int(math.floor(track.notes[0][0] / self.measure_s))
            last_bar = int(math.ceil(t_end / self.measure_s)) - 1
            # 有两位品位时加宽格距，避免 "1" 与 "10" 粘连误读
            max_fret = 0
            for _, _, p in track.notes:
                for open_ in tuning:
                    if 0 <= p - open_ <= MAX_FRET:
                        max_fret = max(max_fret, p - open_)
                        break
            slot_w = 3 if max_fret >= 10 else 2
            bar = first_bar
            while bar <= last_bar:
                row_bars = list(range(bar, min(bar + measures_per_row, last_bar + 1)))
                out.append(f"  小节 {row_bars[0] + 1}-{row_bars[-1] + 1}")
                row_lines = [f"{nm}|" for nm in names]
                for b in row_bars:
                    m_start, m_end = b * self.measure_s, (b + 1) * self.measure_s
                    slots = [["-"] * 16 for _ in names]
                    for key, chord in sorted(self._measure_events(track, m_start, m_end, unit).items()):
                        slot = round((key * unit - m_start) / unit)
                        if not 0 <= slot < 16:
                            continue
                        assigned = assign_frets([p for _, _, p in chord], tuning, prev_mean)
                        frets = [int(f) for _, f in assigned.values() if f.isdigit()]
                        if frets:
                            prev_mean = sum(frets) / len(frets)
                        for pitch, (si, text) in assigned.items():
                            if si >= 0:
                                slots[si][slot] = text
                    for si in range(len(names)):
                        row_lines[si] += "".join(t.rjust(slot_w, "-") for t in slots[si]) + "|"
                out.extend(row_lines)
                out.append("")
                bar += measures_per_row
        return "\n".join(out)


def draw_tab(canvas, layout: TabLayout) -> None:
    """把 TAB 布局画到 tkinter Canvas 上。"""
    import tkinter as tk

    margin = 34.0
    fret_font = ("Arial", 10, "bold")
    name_font = ("Arial", 8)

    for sys in layout.systems:
        n = len(sys.tuning)
        top = sys.y_top + 34.0
        bottom = top + (n - 1) * GuitarTabScore.STRING_GAP
        # 弦名（每行左侧）
        for i, sname in enumerate(sys.string_names):
            canvas.create_text(margin - 8, top + i * GuitarTabScore.STRING_GAP,
                               text=sname, font=name_font, fill="#666666", anchor="e")
        if sys.label:
            canvas.create_text(margin, sys.y_top - 12, anchor="nw", text=sys.label,
                               font=("Microsoft YaHei UI", 8), fill="#888888")
        # 弦线与小节线
        x_end = sys.measures[-1][2] + sys.measures[-1][3]
        for i in range(n):
            ly = top + i * GuitarTabScore.STRING_GAP
            canvas.create_line(margin, ly, x_end, ly, fill="black", width=1)
        for i, (no, _m_start, x, _w) in enumerate(sys.measures):
            canvas.create_line(x, top, x, bottom, fill="black", width=1)
            if i == len(sys.measures) - 1:
                x2 = x + sys.measures[-1][3]
                canvas.create_line(x2, top, x2, bottom, fill="black", width=1)
                canvas.create_line(x2 + 3, top, x2 + 3, bottom, fill="black", width=3)
            canvas.create_text(x + 2, sys.y_top + 2, anchor="nw", text=str(no),
                               font=("Arial", 7), fill="#aaaaaa")

    # 品位数字（白底盖住弦线）
    for g in layout.glyphs:
        w = 5.5 * len(g.text) + 3
        canvas.create_rectangle(g.x - w, g.y - 7, g.x + w, g.y + 7, fill="white", outline="")
        canvas.create_text(g.x, g.y, text=g.text, font=fret_font, fill="black")
        if g.stem:
            # 符干与节奏旗（统一在弦区上方）
            stem_top = g.sys_top - 30
            canvas.create_line(g.x, g.sys_top - 4, g.x, stem_top, fill="black", width=1)
            for k in range(g.flags):
                fy = stem_top + k * 6
                canvas.create_line(g.x, fy, g.x + 7, fy + 4, fill="black", width=2)
