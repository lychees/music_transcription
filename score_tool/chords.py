"""从转录 MIDI 识别和弦进行（节拍对齐窗口 + 模板匹配 + 惯性平滑）。

不引入重型乐理依赖：mido 读 MIDI（muscriptor 已带），
按节拍窗口统计音级权重，与常见和弦模板匹配。
"""

from __future__ import annotations

from dataclasses import dataclass

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# 和弦模板：根音音级为 0 的音级集合 → 后缀
CHORD_TEMPLATES: dict[tuple[int, ...], str] = {
    (0, 4, 7): "",
    (0, 3, 7): "m",
    (0, 4, 7, 10): "7",
    (0, 4, 7, 11): "maj7",
    (0, 3, 7, 10): "m7",
    (0, 3, 7, 11): "m(maj7)",
    (0, 3, 6): "dim",
    (0, 4, 8): "aug",
    (0, 3, 6, 10): "m7♭5",
    (0, 5, 7): "sus4",
    (0, 2, 7): "sus2",
    (0, 7): "5",
    (0, 4, 7, 9): "6",
    (0, 3, 7, 9): "m6",
}


@dataclass
class ChordSpan:
    start: float  # 秒（谱面时间）
    end: float
    label: str  # 如 "C"、"Am"、"G7"；空字符串表示无法判断


def parse_midi_notes(midi_path: str) -> tuple[list[tuple[float, float, int]], float, int]:
    """读 MIDI → (音符列表[(start_s, end_s, pitch)], bpm, 每小节拍数)。

    鼓轨（channel 9）被忽略——鼓点音高对和声无意义。
    """
    import mido

    midi = mido.MidiFile(midi_path)
    tempo = 500000  # 默认 120 BPM
    beats_per_bar = 4
    for msg in midi.tracks[0]:
        if msg.type == "set_tempo":
            tempo = msg.tempo
            break
    for msg in midi.tracks[0]:
        if msg.type == "time_signature":
            beats_per_bar = round(msg.numerator * 4 / msg.denominator)
            break

    notes: list[tuple[float, float, int]] = []
    for track in midi.tracks[1:]:
        t = 0.0
        open_notes: dict[int, float] = {}
        for msg in track:
            t += mido.tick2second(msg.time, midi.ticks_per_beat, tempo)
            if getattr(msg, "channel", None) == 9:
                continue
            if msg.type == "note_on" and msg.velocity > 0:
                open_notes[msg.note] = t
            elif msg.type in ("note_off", "note_on"):
                start = open_notes.pop(msg.note, None)
                if start is not None and t > start:
                    notes.append((start, t, msg.note))
        for pitch, start in open_notes.items():
            notes.append((start, t, pitch))
    notes.sort()
    return notes, 60_000_000 / tempo, beats_per_bar


def _score_template(weights: list[float], bass_pc: int, root: int, template: tuple[int, ...]) -> float:
    """模板匹配得分：覆盖加分、外音扣分、低音=根音加分。"""
    inside = sum(weights[(root + iv) % 12] for iv in template)
    outside = sum(w for pc, w in enumerate(weights) if (pc - root) % 12 not in template)
    score = inside - 0.9 * outside
    if bass_pc == root:
        score += 0.6
    return score


def detect_chords(
    notes: list[tuple[float, float, int]],
    bpm: float,
    beats_per_bar: int = 4,
    window_beats: float = 2.0,
) -> list[ChordSpan]:
    """按节拍窗口识别和弦，返回合并后的和弦段（谱面时间，秒）。"""
    if not notes:
        return []
    beat_s = 60.0 / bpm
    win = beat_s * window_beats
    t0 = min(n[0] for n in notes)
    t1 = max(n[1] for n in notes)
    n_win = max(1, round((t1 - t0) / win))

    labels: list[str] = []
    prev: tuple[int, tuple[int, ...]] | None = None
    for i in range(n_win):
        w0, w1 = t0 + i * win, t0 + (i + 1) * win
        weights = [0.0] * 12
        bass_note: int | None = None
        for start, end, pitch in notes:
            ov = min(end, w1) - max(start, w0)
            if ov <= 0:
                continue
            weights[pitch % 12] += min(ov, win)
            if bass_note is None or pitch < bass_note:
                bass_note = pitch
        total = sum(weights)
        if total < win * 0.15 or bass_note is None:
            labels.append("")
            prev = None
            continue
        bass_pc = bass_note % 12
        best: tuple[float, int, tuple[int, ...]] | None = None
        for root in range(12):
            for template in CHORD_TEMPLATES:
                s = _score_template(weights, bass_pc, root, template)
                if prev is not None and (root, template) == prev:
                    s += 0.35  # 惯性：避免相邻窗口频繁换和弦
                if best is None or s > best[0]:
                    best = (s, root, template)
        assert best is not None
        score, root, template = best
        if score < total * 0.35:
            labels.append("")
            prev = None
        else:
            labels.append(NOTE_NAMES[root] + CHORD_TEMPLATES[template])
            prev = (root, template)

    # 合并相邻同名窗口为和弦段
    spans: list[ChordSpan] = []
    for i, label in enumerate(labels):
        start, end = t0 + i * win, t0 + (i + 1) * win
        if spans and spans[-1].label == label:
            spans[-1].end = end
        else:
            spans.append(ChordSpan(start, end, label))
    return spans


# Krumhansl-Schmuckler 调性分析权重
_KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def estimate_key(notes: list[tuple[float, float, int]]) -> str:
    """估计调性，返回如 "C 大调" / "A 小调"；音符太少返回空串。"""
    weights = [0.0] * 12
    for start, end, pitch in notes:
        weights[pitch % 12] += end - start
    if sum(weights) < 2.0:
        return ""

    def corr(profile: list[float], shift: int) -> float:
        xs = [weights[(i + shift) % 12] for i in range(12)]
        mx, my = sum(xs) / 12, sum(profile) / 12
        num = sum((x - mx) * (y - my) for x, y in zip(xs, profile))
        den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in profile)) ** 0.5
        return num / den if den else 0.0

    best = (-2.0, "")
    for root in range(12):
        c = corr(_KS_MAJOR, -root % 12)
        if c > best[0]:
            best = (c, f"{NOTE_NAMES[root]} 大调")
        c = corr(_KS_MINOR, -root % 12)
        if c > best[0]:
            best = (c, f"{NOTE_NAMES[root]} 小调")
    return best[1]
