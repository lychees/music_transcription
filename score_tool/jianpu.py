"""简谱（数字谱）排版：从量化 MIDI 生成简谱画布布局。

输入 score.mid（已按节拍网格量化），输出每个数字/符号的画布坐标与
时间映射（用于播放高亮与点击跳转）。不依赖外部排版引擎。

记谱规则（简谱惯例）：
- 唱名 1-7 按调性映射（大调主音=1，小调按关系大调记谱，主音=6）；
- C4–B4 为中音区（无点），每高八度上方加一点，每低八度下方加一点；
- 四分音符无减时线，八分一条、十六分两条减时线；
- 增时线（"–"）表示延长一拍，附点加半点；
- 整拍起止的长音用增时线；跨拍音拆分并加连线；
- 休止符为 0，时值标记同音符；同时发声的音符纵向叠置（和弦）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from score_tool.chords import NOTE_NAMES

# 大调音级 → 唱名；变化音用升号（相对前一个自然音级）
_MAJOR_DEGREES = {0: "1", 2: "2", 4: "3", 5: "4", 7: "5", 9: "6", 11: "7"}
_SHARP_DEGREES = {1: "1", 3: "2", 6: "4", 8: "5", 10: "6"}


def key_to_jianpu_tonic(key_label: str) -> tuple[str, int]:
    """调性标签（如 "A 小调"）→ 简谱 "1=X" 的调号字母与主音音级。

    小调按关系大调记谱（关系大调主音 = 小调主音 + 3 半音）。
    """
    if not key_label:
        return "C", 0
    name, _, kind = key_label.partition(" ")
    tonic = NOTE_NAMES.index(name) if name in NOTE_NAMES else 0
    if kind.startswith("小"):
        tonic = (tonic + 3) % 12
    return NOTE_NAMES[tonic], tonic


def pitch_to_degree(pitch: int, tonic_pc: int) -> tuple[str, str, int]:
    """MIDI 音高 → (唱名, 前缀, 八度偏移)。八度偏移相对 C4–B4 中音区。"""
    interval = (pitch - tonic_pc) % 12
    if interval in _MAJOR_DEGREES:
        name, prefix = _MAJOR_DEGREES[interval], ""
    else:
        name, prefix = _SHARP_DEGREES[interval], "#"
    octave = pitch // 12 - 1 - 4  # C4=60 所在八度为中音区
    return name, prefix, octave


def duration_marks(units: int) -> tuple[int, int, bool]:
    """时值（十六分为单位）→ (减时线数, 增时线数, 是否附点)。"""
    if units <= 1:
        return 2, 0, False
    if units == 2:
        return 1, 0, False
    if units == 3:
        return 1, 0, True
    if units == 4:
        return 0, 0, False
    if units <= 6:
        return 0, 0, True
    beats = units // 4
    rem = units % 4
    dashes = beats - 1
    if rem == 0:
        return 0, dashes, False
    if rem == 1:
        return 1, dashes, False
    return 0, dashes, True


@dataclass
class Glyph:
    x: float
    y: float
    text: str  # 唱名/0/×
    prefix: str = ""
    octave: int = 0
    underlines: int = 0
    dashes: int = 0
    dot: bool = False
    tie_target_x: float | None = None  # 连线终点（下一拆分段的数字 x）
    start_s: float = 0.0
    end_s: float = 0.0


@dataclass
class JianpuSystem:
    """一行（一个乐器的连续若干小节）。"""

    y_top: float
    y_bottom: float
    t_start: float
    t_end: float
    label: str = ""
    # 每小节: (小节号, 起始秒, x, 宽)
    measures: list[tuple[int, float, float, float]] = field(default_factory=list)


@dataclass
class JianpuLayout:
    glyphs: list[Glyph]
    systems: list[JianpuSystem]
    total_height: float
    header: str

    def glyphs_at(self, t: float) -> list[int]:
        """时刻 t 发声的 glyph 下标。"""
        return [i for i, g in enumerate(self.glyphs) if g.start_s <= t < g.end_s]

    def beat_to_x(self, t: float) -> tuple[float, float] | None:
        """谱面时间 → (小节 x, 系统顶 y)，用于画和弦标记。"""
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


@dataclass
class _Track:
    name: str
    notes: list[tuple[float, float, int]]  # (start, end, pitch)
    is_drum: bool = False
    program: int | None = None  # GM 乐器号（首个 program_change）


class JianpuScore:
    """一份 MIDI 的简谱排版数据。"""

    DIGIT_W = 15.0  # 单个数字占位宽
    MEASURES_PER_ROW = 4
    ROW_H = 136.0  # 单声部行高（含和弦叠置余量）

    def __init__(self, midi_path: str, key_label: str = ""):
        tracks, bpm, bpb = self._parse_tracks(midi_path)
        self.tracks = tracks
        self.bpm = bpm
        self.bpb = bpb
        self.beat_s = 60.0 / bpm
        self.measure_s = self.beat_s * bpb
        self.tonic_letter, self.tonic_pc = key_to_jianpu_tonic(key_label)
        self.header = f"1={self.tonic_letter}   {bpb}/4   ♩={round(bpm)}"

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def _parse_tracks(midi_path: str) -> tuple[list[_Track], float, int]:
        import mido

        midi = mido.MidiFile(midi_path)
        tempo = 500000
        beats_per_bar = 4
        for msg in midi.tracks[0]:
            if msg.type == "set_tempo":
                tempo = msg.tempo
            elif msg.type == "time_signature":
                beats_per_bar = round(msg.numerator * 4 / msg.denominator)

        tracks: list[_Track] = []
        for track in midi.tracks[1:]:
            name = ""
            t = 0.0
            open_notes: dict[int, float] = {}
            notes: list[tuple[float, float, int]] = []
            is_drum = False
            program: int | None = None
            for msg in track:
                t += mido.tick2second(msg.time, midi.ticks_per_beat, tempo)
                if msg.type == "track_name":
                    name = msg.name
                elif msg.type == "program_change" and program is None:
                    program = msg.program
                elif msg.type == "note_on" and msg.velocity > 0:
                    if msg.channel == 9:
                        is_drum = True
                    open_notes[msg.note] = t
                elif msg.type in ("note_off", "note_on"):
                    start = open_notes.pop(msg.note, None)
                    if start is not None and t > start:
                        notes.append((start, t, msg.note))
            for pitch, start in open_notes.items():
                notes.append((start, t, pitch))
            notes.sort()
            if notes:
                tracks.append(_Track(name=name or f"声部 {len(tracks) + 1}", notes=notes, is_drum=is_drum, program=program))
        return tracks, 60_000_000 / tempo, beats_per_bar

    # ------------------------------------------------------------------ 布局
    def layout(self, width_px: float) -> JianpuLayout:
        margin = 24.0
        content_w = max(200.0, width_px - 2 * margin)
        measure_w = content_w / self.MEASURES_PER_ROW
        beat_w = measure_w / self.bpb

        glyphs: list[Glyph] = []
        systems: list[JianpuSystem] = []
        y = 64.0  # 顶部留标题

        for track in self.tracks:
            t_start = track.notes[0][0]
            t_end = max(n[1] for n in track.notes)
            first_bar = int(math.floor(t_start / self.measure_s))
            last_bar = int(math.ceil(t_end / self.measure_s)) - 1
            rows = math.ceil((last_bar - first_bar + 1) / self.MEASURES_PER_ROW)

            for row in range(rows):
                bar0 = first_bar + row * self.MEASURES_PER_ROW
                bar1 = min(bar0 + self.MEASURES_PER_ROW - 1, last_bar)
                sys = JianpuSystem(
                    y_top=y,
                    y_bottom=y + self.ROW_H,
                    t_start=bar0 * self.measure_s,
                    t_end=(bar1 + 1) * self.measure_s,
                    label=track.name if row == 0 else "",
                )
                for bar in range(bar0, bar1 + 1):
                    sys.measures.append(
                        (bar + 1, bar * self.measure_s, margin + (bar - bar0) * measure_w, measure_w)
                    )
                self._layout_row(track, sys, beat_w, glyphs)
                systems.append(sys)
                y += self.ROW_H

        return JianpuLayout(glyphs=glyphs, systems=systems, total_height=y + 20, header=self.header)

    def _layout_row(self, track: _Track, sys: JianpuSystem, beat_w: float, glyphs: list[Glyph]) -> None:
        base_y = sys.y_top + 72.0  # 唱名基线（留上方八度点与和弦叠置空间）
        unit = self.beat_s / 4  # 十六分时长

        for _no, m_start, m_x, _w in sys.measures:
            m_end = m_start + self.measure_s
            # 本小节的音符，按开始时间聚成事件（和弦）；key 为十六分网格整数
            events: dict[int, list[tuple[float, float, int]]] = {}
            for n in track.notes:
                if m_start - 1e-6 <= n[0] < m_end - 1e-6:
                    events.setdefault(round(n[0] / unit), []).append(n)
            # 补休止符：事件之间的空隙（含行尾）
            filled: list[tuple[float, float, list[tuple[float, float, int]] | None]] = []
            cursor = m_start
            for key in sorted(events):
                start = key * unit
                if start > cursor + 1e-6:
                    filled.append((cursor, start, None))
                end = min(max(n[1] for n in events[key]), m_end)
                filled.append((start, end, events[key]))
                cursor = max(cursor, end)
            if cursor < m_end - 1e-6 and events:
                filled.append((cursor, m_end, None))
            if not events and track.notes[0][0] <= m_start and m_end <= track.notes[-1][1]:
                filled.append((m_start, m_end, None))  # 整小节休止

            for start, end, chord in filled:
                x = m_x + (start - m_start) / self.beat_s * beat_w
                self._layout_event(track, chord, start, end, x, base_y, unit, glyphs)

    def _layout_event(
        self,
        track: _Track,
        chord: list[tuple[float, float, int]] | None,
        start: float,
        end: float,
        x: float,
        base_y: float,
        unit: float,
        glyphs: list[Glyph],
    ) -> None:
        """一个事件（和弦/单音/休止）→ 若干 glyph（整拍长音用增时线，跨拍拆分加连线）。"""
        total_units = max(1, round((end - start) / unit))
        segments: list[int] = []
        pos_in_beat = round((start / self.beat_s) % 1.0 * 4)
        remain = total_units
        if pos_in_beat > 0:  # 先补到下一拍界
            seg = min(remain, 4 - pos_in_beat)
            segments.append(seg)
            remain -= seg
        if remain >= 4:  # 整拍部分（增时线表示）
            whole = remain - remain % 4
            segments.append(whole)
            remain -= whole
        if remain > 0:
            segments.append(remain)

        if chord is None:
            digits = [("0", "", 0)]
        elif track.is_drum:
            digits = [("×", "", 0)]
        else:
            pitches = sorted({p for _, _, p in chord})
            digits = [pitch_to_degree(p, self.tonic_pc) for p in pitches]

        x_cursor = x
        seg_start = start
        for si, seg_units in enumerate(segments):
            underlines, dashes, dot = duration_marks(seg_units)
            seg_end = seg_start + seg_units * unit
            step = self.DIGIT_W + dashes * 11.0 + (6.0 if dot else 0.0) + 2.0
            tie_x = (x_cursor + step + self.DIGIT_W / 2) if si < len(segments) - 1 else None
            for level, (text, prefix, octave) in enumerate(digits):
                glyphs.append(
                    Glyph(
                        x=x_cursor + self.DIGIT_W / 2,
                        y=base_y - level * 21.0,
                        text=text,
                        prefix=prefix,
                        octave=octave,
                        underlines=underlines if level == 0 else 0,  # 减时线只在最下方画一组
                        dashes=dashes,
                        dot=dot,
                        tie_target_x=tie_x,
                        start_s=seg_start,
                        end_s=seg_end,
                    )
                )
            x_cursor += step
            seg_start = seg_end


def draw_jianpu(canvas, layout: JianpuLayout) -> None:
    """把布局画到 tkinter Canvas 上。"""
    import tkinter as tk

    margin = 24.0
    canvas.create_text(margin, 18, anchor="nw", text=layout.header,
                       font=("Times New Roman", 15, "bold"), fill="black")

    digit_font = ("Times New Roman", 15, "bold")
    small_font = ("Times New Roman", 10)

    for sys in layout.systems:
        if sys.label:
            canvas.create_text(4, sys.y_top + 4, anchor="nw",
                               text=sys.label, font=("Microsoft YaHei UI", 8), fill="#888888")
        for i, (no, _m_start, x, w) in enumerate(sys.measures):
            canvas.create_line(x, sys.y_top + 18, x, sys.y_bottom - 18, fill="black", width=1)
            if i == len(sys.measures) - 1:
                x2 = x + w
                canvas.create_line(x2, sys.y_top + 18, x2, sys.y_bottom - 18, fill="black", width=1)
                canvas.create_line(x2 + 3, sys.y_top + 18, x2 + 3, sys.y_bottom - 18, fill="black", width=3)
            canvas.create_text(x + 2, sys.y_top + 2, anchor="nw", text=str(no),
                               font=("Arial", 7), fill="#aaaaaa")

    for g in layout.glyphs:
        cx, cy = g.x, g.y
        canvas.create_text(cx, cy, text=g.text, font=digit_font, fill="black")
        if g.prefix:
            canvas.create_text(cx - 10, cy - 7, text=g.prefix, font=small_font, fill="black")
        for k in range(abs(g.octave)):  # 八度点
            dy = -15 - k * 5 if g.octave > 0 else 13 + g.underlines * 4 + k * 5
            canvas.create_oval(cx - 1.6, cy + dy - 1.6, cx + 1.6, cy + dy + 1.6,
                               fill="black", outline="")
        for k in range(g.underlines):  # 减时线
            ly = cy + 10 + k * 4
            canvas.create_line(cx - 8, ly, cx + 8, ly, fill="black", width=2)
        for k in range(g.dashes):  # 增时线
            canvas.create_text(cx + 13 + k * 11, cy, text="–", font=digit_font, fill="black")
        if g.dot:  # 附点
            canvas.create_oval(cx + 9, cy + 3, cx + 12, cy + 6, fill="black", outline="")
        if g.tie_target_x is not None:  # 连线
            canvas.create_arc(cx + 5, cy - 22, g.tie_target_x - 5, cy - 2,
                              start=20, extent=140, style=tk.ARC, outline="black")
