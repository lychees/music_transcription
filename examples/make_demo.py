"""生成一段示例音频（C 大调，旋律 + 低音 + 分解和弦），用于快速试用乐谱助手。

用法： uv run python examples/make_demo.py
输出： examples/demo.wav（16 秒，120 BPM）
"""

from pathlib import Path

import numpy as np

SR = 44100
BPM = 120
BEAT = 60.0 / BPM  # 0.5 s


def note_freq(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def synth_note(midi: int, start: float, dur: float, out: np.ndarray, amp: float = 0.5) -> None:
    """带谐波与指数衰减的类钢琴音色。"""
    t = np.arange(int(dur * SR)) / SR
    f = note_freq(midi)
    env = np.exp(-2.5 * t / max(dur, 0.1))
    sig = (
        0.6 * np.sin(2 * np.pi * f * t)
        + 0.25 * np.sin(2 * np.pi * 2 * f * t)
        + 0.1 * np.sin(2 * np.pi * 3 * f * t)
        + 0.05 * np.sin(2 * np.pi * 4 * f * t)
    ) * env
    i0 = int(start * SR)
    out[i0 : i0 + len(sig)] += amp * sig


def main() -> None:
    bars = 8
    total = bars * 4 * BEAT + 1.0
    out = np.zeros(int(total * SR), dtype=np.float64)

    # C 大调 I–IV–V–I 和弦进行（每两小节一个和弦）
    chords = [  # (根音 MIDI, 三音, 五音)
        (48, 52, 55),  # C
        (48, 52, 55),  # C
        (41, 45, 48),  # F
        (41, 45, 48),  # F
        (43, 47, 50),  # G
        (43, 47, 50),  # G
        (48, 52, 55),  # C
        (48, 52, 55),  # C
    ]
    melody = [72, 74, 76, 77, 79, 77, 76, 74, 72, 76, 79, 84, 79, 76, 74, 72,
              72, 74, 76, 77, 79, 81, 79, 77, 76, 74, 72, 74, 76, 74, 72, 72]

    # 旋律：八分音符
    for i, m in enumerate(melody):
        synth_note(m, i * BEAT / 2, BEAT / 2 * 0.95, out, amp=0.55)

    for bar in range(bars):
        root, third, fifth = chords[bar]
        # 低音：每小节两拍一个根音
        synth_note(root - 12, bar * 4 * BEAT, 2 * BEAT * 0.9, out, amp=0.5)
        synth_note(root - 12, (bar * 4 + 2) * BEAT, 2 * BEAT * 0.9, out, amp=0.5)
        # 分解和弦伴奏：八分音符
        arp = [root, third, fifth, third] * 2
        for j, m in enumerate(arp):
            synth_note(m, bar * 4 * BEAT + j * BEAT / 2, BEAT / 2 * 0.9, out, amp=0.3)

    out /= max(np.abs(out).max(), 1e-9)
    pcm = (out * 0.9 * 32767).astype(np.int16)

    import wave

    path = Path(__file__).parent / "demo.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"已生成 {path}（{total:.1f} 秒）")


if __name__ == "__main__":
    main()
