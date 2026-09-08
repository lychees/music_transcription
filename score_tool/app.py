"""听曲目生成乐谱的辅助工具 — 图形界面（tkinter）。"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from score_tool import envcheck
from score_tool.worker import AUDIO_SUFFIXES, ScoreTranscriber, run_in_thread

MODEL_SIZES = ("small", "medium", "large")
MODEL_HINTS = {
    "small": "约 1 亿参数，最快，CPU 可用",
    "medium": "约 3 亿参数，速度/精度均衡（默认）",
    "large": "约 14 亿参数，最准，建议 GPU",
}
TEMPO_MODES = ("best-effort", "true", "false")
TEMPO_HINTS = {
    "best-effort": "尽力检测，失败则用 120 BPM 占位（推荐）",
    "true": "强制检测，检测不到稳定节拍则报错",
    "false": "不检测节拍",
}


class ScoreAssistantApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("乐谱助手 · 听曲目生成乐谱（MuScriptor）")
        self.geometry("860x720")
        self.minsize(760, 640)
        self.option_add("*Font", ("Microsoft YaHei UI", 9))

        self.q: queue.Queue[tuple] = queue.Queue()
        self._transcribers: dict[str, ScoreTranscriber] = {}
        self._worker: threading.Thread | None = None
        self._instruments: list[str] = []  # 空 = 自动检测
        self._all_instruments: list[str] = []
        self._last_out_dir: Path | None = None

        self._build_widgets()
        self._load_instrument_names()
        self.after(50, self._refresh_env_async)
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------ UI
    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True)
        main = ttk.Frame(self.nb)
        self.nb.add(main, text="转录")

        # —— 文件选择 ——
        files = ttk.LabelFrame(main, text="音频与输出")
        files.pack(fill=tk.X, **pad)
        files.columnconfigure(1, weight=1)

        ttk.Label(files, text="音频/链接：").grid(row=0, column=0, sticky=tk.W, padx=6, pady=4)
        self.audio_var = tk.StringVar()
        ttk.Entry(files, textvariable=self.audio_var).grid(row=0, column=1, sticky=tk.EW, padx=6)
        ttk.Button(files, text="浏览…", command=self._pick_audio).grid(row=0, column=2, padx=6)

        ttk.Label(files, text="输出目录：").grid(row=1, column=0, sticky=tk.W, padx=6, pady=4)
        self.out_var = tk.StringVar()
        ttk.Entry(files, textvariable=self.out_var).grid(row=1, column=1, sticky=tk.EW, padx=6)
        ttk.Button(files, text="浏览…", command=self._pick_outdir).grid(row=1, column=2, padx=6)

        ttk.Label(
            files, text="本地音频文件路径或 B 站视频链接（BV/av/b23.tv）均可，自动识别",
            foreground="#888",
        ).grid(row=2, column=1, sticky=tk.W, padx=6, pady=(0, 4))

        # —— 转录设置 ——
        opts = ttk.LabelFrame(main, text="转录设置")
        opts.pack(fill=tk.X, **pad)
        for c in range(6):
            opts.columnconfigure(c, weight=0)
        opts.columnconfigure(5, weight=1)

        ttk.Label(opts, text="模型：").grid(row=0, column=0, sticky=tk.W, padx=6, pady=4)
        self.model_var = tk.StringVar(value="medium")
        cb = ttk.Combobox(opts, textvariable=self.model_var, values=MODEL_SIZES, width=8, state="readonly")
        cb.grid(row=0, column=1, sticky=tk.W)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._update_model_hint())
        self.model_hint = ttk.Label(opts, text="", foreground="#666")
        self.model_hint.grid(row=0, column=2, columnspan=4, sticky=tk.W, padx=6)
        self._update_model_hint()

        ttk.Label(opts, text="乐器限定：").grid(row=1, column=0, sticky=tk.W, padx=6, pady=4)
        self.inst_label = ttk.Label(opts, text="自动检测（全部乐器）", width=34)
        self.inst_label.grid(row=1, column=1, columnspan=3, sticky=tk.W)
        ttk.Button(opts, text="选择乐器…", command=self._pick_instruments).grid(row=1, column=4, sticky=tk.W, padx=6)

        ttk.Label(opts, text="CFG 强度：").grid(row=2, column=0, sticky=tk.W, padx=6, pady=4)
        self.cfg_var = tk.StringVar(value="1.0")
        ttk.Spinbox(opts, from_=1.0, to=3.0, increment=0.5, textvariable=self.cfg_var, width=6).grid(row=2, column=1, sticky=tk.W)
        ttk.Label(opts, text="节拍检测：").grid(row=2, column=2, sticky=tk.E, padx=6)
        self.tempo_var = tk.StringVar(value="best-effort")
        ttk.Combobox(opts, textvariable=self.tempo_var, values=TEMPO_MODES, width=12, state="readonly").grid(row=2, column=3, sticky=tk.W)

        self.sheets_var = tk.BooleanVar(value=True)
        self.sheets_cb = ttk.Checkbutton(
            opts, text="生成乐谱（MusicXML + 总谱/分谱 PDF，需 MuseScore 4+）",
            variable=self.sheets_var,
        )
        self.sheets_cb.grid(row=3, column=0, columnspan=6, sticky=tk.W, padx=6, pady=4)

        # —— 环境状态 ——
        env = ttk.LabelFrame(main, text="环境状态")
        env.pack(fill=tk.X, **pad)
        self.gpu_label = ttk.Label(env, text="GPU：检测中…")
        self.gpu_label.pack(side=tk.LEFT, padx=10, pady=4)
        self.ms_label = ttk.Label(env, text="MuseScore：检测中…")
        self.ms_label.pack(side=tk.LEFT, padx=10)
        self.ms_btn = ttk.Button(env, text="手动指定…", command=self._pick_musescore)
        self.ms_btn.pack(side=tk.LEFT)
        self.hf_label = ttk.Label(env, text="HuggingFace：检测中…")
        self.hf_label.pack(side=tk.LEFT, padx=10)
        self.hf_btn = ttk.Button(env, text="登录设置…", command=self._hf_dialog)
        self.hf_btn.pack(side=tk.LEFT)

        # —— 运行 ——
        run = ttk.Frame(main)
        run.pack(fill=tk.X, **pad)
        self.start_btn = ttk.Button(run, text="开始转录", command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=6)
        self.progress = ttk.Progressbar(run, mode="determinate", length=300)
        self.progress.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)
        self.status_label = ttk.Label(run, text="就绪")
        self.status_label.pack(side=tk.LEFT, padx=6)

        # —— 日志 ——
        logf = ttk.LabelFrame(main, text="日志")
        logf.pack(fill=tk.BOTH, expand=True, **pad)
        self.log = ScrolledText(logf, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # —— 底部 ——
        bottom = ttk.Frame(main)
        bottom.pack(fill=tk.X, **pad)
        self.open_btn = ttk.Button(bottom, text="打开输出文件夹", command=self._open_outdir, state=tk.DISABLED)
        self.open_btn.pack(side=tk.LEFT, padx=6)
        ttk.Label(
            bottom,
            text="模型：MuScriptor（arXiv:2607.08168，CC BY-NC 4.0，仅限非商业用途）",
            foreground="#888",
        ).pack(side=tk.RIGHT, padx=6)

        # —— 乐谱播放 tab ——
        from score_tool.player import ScorePlayer

        self.player = ScorePlayer(self, self.nb)
        self.nb.add(self.player.frame, text="乐谱播放")

    # ------------------------------------------------------------------ 环境
    def _refresh_env_async(self) -> None:
        def worker() -> None:
            result = {
                "gpu": envcheck.gpu_info(),
                "musescore": envcheck.musescore_path(),
                "hf": envcheck.hf_token_present(),
                "ffmpeg": envcheck.ffmpeg_present(),
            }
            self.q.put(("env", result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_env(self, result: dict) -> None:
        gpu = result["gpu"]
        self.gpu_label.config(text=f"GPU：{gpu}" if gpu else "GPU：无（将用 CPU，较慢）")
        ms = result["musescore"]
        self.ms_label.config(text=f"MuseScore：{Path(ms).name}" if ms else "MuseScore：未找到")
        if not ms:
            self.sheets_var.set(False)
            self.sheets_cb.config(state=tk.DISABLED)
        hf = result["hf"]
        self.hf_label.config(text="HuggingFace：已登录" if hf else "HuggingFace：未登录（必需）")
        if not result["ffmpeg"]:
            self._log("提示：未找到 ffmpeg，部分音频格式可能无法读取")

    def _load_instrument_names(self) -> None:
        try:
            from muscriptor.tokenizer.mt3 import MT3_FULL_PLUS_GROUP_NAMES

            self._all_instruments = sorted(MT3_FULL_PLUS_GROUP_NAMES)
        except Exception as e:  # noqa: BLE001
            self._log(f"无法加载乐器列表：{e}")

    # ------------------------------------------------------------------ 对话框
    def _pick_audio(self) -> None:
        patterns = " ".join(f"*{s}" for s in sorted(AUDIO_SUFFIXES))
        path = filedialog.askopenfilename(
            title="选择音频文件",
            filetypes=[("音频文件", patterns), ("所有文件", "*.*")],
        )
        if path:
            self.audio_var.set(path)
            if not self.out_var.get():
                self.out_var.set(str(Path(path).with_suffix("")) + "_score")

    def _pick_outdir(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_var.set(path)

    def _pick_musescore(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 MuseScore 可执行文件",
            filetypes=[("MuseScore", "MuseScore*.exe mscore*.exe"), ("所有文件", "*.*")],
        )
        if not path:
            return
        envcheck.set_musescore_override(path)
        ms = envcheck.musescore_path()
        if ms:
            self.ms_label.config(text=f"MuseScore：{Path(ms).name}")
            self.sheets_cb.config(state=tk.NORMAL)
            self.sheets_var.set(True)
        else:
            messagebox.showerror("MuseScore", "该文件不是可用的 MuseScore 4+，请重新选择。")

    def _pick_instruments(self) -> None:
        if not self._all_instruments:
            messagebox.showinfo("乐器", "乐器列表尚未加载完成，请稍候。")
            return
        dlg = tk.Toplevel(self)
        dlg.title("限定要转录的乐器（不选 = 自动检测）")
        dlg.geometry("360x480")
        dlg.transient(self)
        dlg.grab_set()

        lb = tk.Listbox(dlg, selectmode=tk.MULTIPLE, exportselection=False)
        for name in self._all_instruments:
            lb.insert(tk.END, name)
        lb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        for name in self._instruments:
            if name in self._all_instruments:
                lb.selection_set(self._all_instruments.index(name))

        btns = ttk.Frame(dlg)
        btns.pack(fill=tk.X, padx=8, pady=8)

        def ok() -> None:
            self._instruments = [lb.get(i) for i in lb.curselection()]
            if self._instruments:
                text = "、".join(self._instruments)
                self.inst_label.config(text=text if len(text) <= 40 else f"已选 {len(self._instruments)} 种乐器")
            else:
                self.inst_label.config(text="自动检测（全部乐器）")
            dlg.destroy()

        ttk.Button(btns, text="全选", command=lambda: lb.selection_set(0, tk.END)).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="清空", command=lambda: lb.selection_clear(0, tk.END)).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="确定", command=ok).pack(side=tk.RIGHT, padx=4)
        dlg.wait_window()

    def _hf_dialog(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("HuggingFace 登录（下载模型权重所必需）")
        dlg.geometry("560x430")
        dlg.transient(self)

        text = (
            "模型权重托管在 HuggingFace，采用 CC BY-NC 4.0 许可（非商业用途），\n"
            "首次下载前需要：\n\n"
            "1. 注册并登录 HuggingFace 账号；\n"
            "2. 打开模型页面，点击接受许可协议（自动通过）。\n"
            "   注意：small / medium / large 三个规格的授权页相互独立，\n"
            "   打算用哪个规格就去哪个页面接受一次；\n"
            "3. 在 Token 设置页创建一个 Access Token（read 权限即可）；\n"
            "4. 把 Token 粘贴到下方并保存。\n\n"
            "保存后权重会在首次转录时自动下载并缓存到本地。"
        )
        ttk.Label(dlg, text=text, justify=tk.LEFT).pack(padx=12, pady=10, anchor=tk.W)

        links = ttk.Frame(dlg)
        links.pack(anchor=tk.W, padx=12)
        for size in ("small", "medium", "large"):
            ttk.Button(
                links, text=f"① {size} 授权页",
                command=lambda s=size: webbrowser.open(envcheck.HF_MODEL_PAGES[s]),
            ).pack(side=tk.LEFT, padx=2)
        ttk.Button(links, text="② Token 设置页", command=lambda: webbrowser.open(envcheck.HF_TOKENS_PAGE)).pack(side=tk.LEFT, padx=6)

        row = ttk.Frame(dlg)
        row.pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(row, text="Token：").pack(side=tk.LEFT)
        token_var = tk.StringVar()
        ttk.Entry(row, textvariable=token_var, show="*", width=40).pack(side=tk.LEFT, padx=6)

        def save() -> None:
            try:
                envcheck.save_hf_token(token_var.get())
            except ValueError as e:
                messagebox.showerror("Token", str(e), parent=dlg)
                return
            self.hf_label.config(text="HuggingFace：已登录")
            messagebox.showinfo("Token", "已保存。现在可以开始转录了。", parent=dlg)
            dlg.destroy()

        ttk.Button(dlg, text="保存 Token", command=save).pack(pady=6)

    # ------------------------------------------------------------------ 运行
    def _start(self) -> None:
        source = self.audio_var.get().strip()
        url = source if source.startswith(("http://", "https://")) else ""
        audio = "" if url else source
        if url:
            if "bilibili.com" not in url and "b23.tv" not in url:
                messagebox.showerror("链接", "请输入有效的 B 站视频链接（bilibili.com 或 b23.tv）。")
                return
        elif not audio or not Path(audio).is_file():
            messagebox.showerror("音频", "请输入存在的本地文件路径，或 B 站视频链接。")
            return
        default_out = str(Path(audio).with_suffix("")) + "_score" if audio else ""
        out_text = self.out_var.get().strip() or default_out
        out_dir = Path(out_text) if out_text else None  # 链接模式留空 → 下载后按标题生成
        want_sheets = self.sheets_var.get()

        if want_sheets and envcheck.musescore_path() is None:
            messagebox.showerror("MuseScore", "生成乐谱需要 MuseScore 4+，请安装或在下方手动指定路径。")
            return
        if not envcheck.hf_token_present():
            if not messagebox.askyesno("HuggingFace", "尚未配置 HuggingFace 登录，首次下载模型会失败。\n仍要继续吗？"):
                self._hf_dialog()
                return

        sheets_dir = out_dir / "sheets" if out_dir is not None else None
        if want_sheets and sheets_dir is not None and sheets_dir.exists() and any(sheets_dir.iterdir()):
            if messagebox.askyesno("输出目录", f"{sheets_dir} 已存在且非空，清空后继续？"):
                shutil.rmtree(sheets_dir)
            else:
                return

        try:
            cfg = float(self.cfg_var.get())
        except ValueError:
            cfg = 1.0

        size = self.model_var.get()
        if size not in self._transcribers:
            self._transcribers[size] = ScoreTranscriber(model_size=size)
        transcriber = self._transcribers[size]

        self._log_clear()
        self._log(f"链接：{url}" if url else f"音频：{audio}")
        self._log(f"输出：{out_dir if out_dir else '（下载后按标题生成）'}")
        if self._instruments:
            self._log(f"乐器限定：{'、'.join(self._instruments)}")
        self.start_btn.config(state=tk.DISABLED)
        self.open_btn.config(state=tk.DISABLED)
        self.progress.config(value=0, maximum=100)
        self.status_label.config(text="准备中…")

        self._worker = threading.Thread(
            target=run_in_thread,
            args=(self.q, lambda: transcriber),
            kwargs=dict(
                bilibili_url=url or None,
                audio_path=audio,
                out_dir=out_dir,
                want_sheets=want_sheets,
                instruments=self._instruments or None,
                cfg_coef=cfg,
                detect_tempo=self.tempo_var.get(),
            ),
            daemon=True,
        )
        self._worker.start()

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "progress":
                    _, done, total = msg
                    self.progress.config(maximum=max(total, 1), value=done)
                    self.status_label.config(text=f"转录中 {done}/{total} 块…")
                elif kind == "env":
                    self._on_env(msg[1])
                elif kind == "done":
                    self._on_done(msg[1])
                elif kind == "error":
                    self._on_error(msg[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_done(self, result) -> None:
        self.start_btn.config(state=tk.NORMAL)
        self.status_label.config(text="完成")
        self._last_out_dir = result.out_dir or Path(self.out_var.get())
        if result.out_dir is not None:
            self.out_var.set(str(result.out_dir))  # 链接模式：显示按标题生成的目录
        self.open_btn.config(state=tk.NORMAL)
        self._log(
            f"完成：识别 {result.n_notes} 个音符，耗时 {result.elapsed_seconds:.1f} 秒"
            + (f"（音频 {result.audio_seconds:.1f} 秒）" if result.audio_seconds else "")
            + (f"，速度 {result.tempo_bpm:.0f} BPM" if result.tempo_bpm else "")
        )
        self._log(f"MIDI：{result.midi_path}")
        musicxml = result.sheets_dir / "score.musicxml" if result.sheets_dir else None
        if musicxml and musicxml.is_file():
            self._log(f"乐谱目录：{result.sheets_dir}")
            self.nb.select(self.player.frame)  # 先切到播放页，让首屏渲染拿到真实宽度
            self.player.load_score(
                musicxml,
                audio=result.audio_path or self.audio_var.get(),
                first_onset_s=result.first_note_onset,
                status_cb=lambda s: self.status_label.config(text=s),
                midi_path=result.sheets_dir / "score.mid",
            )
        else:
            self.player.show_placeholder("本次未生成乐谱（转录时未勾选「生成乐谱」）")
            messagebox.showinfo("完成", f"转录完成！\nMIDI：{result.midi_path}")

    def _on_error(self, err: str) -> None:
        self.start_btn.config(state=tk.NORMAL)
        self.status_label.config(text="出错")
        self._log(f"错误：{err}")
        hint = ""
        low = err.lower()
        if any(k in low for k in ("401", "403", "unauthorized", "gated", "token", "auth")):
            hint = "\n\n这通常是 HuggingFace 授权问题：请点击「登录设置…」完成授权后重试。"
        elif "MuseScore" in err:
            hint = "\n\n请安装 MuseScore 4+ 或在环境状态栏手动指定其路径。"
        messagebox.showerror("转录失败", err + hint)

    # ------------------------------------------------------------------ 小工具
    def _update_model_hint(self) -> None:
        self.model_hint.config(text=MODEL_HINTS.get(self.model_var.get(), ""))

    def _log(self, text: str) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _log_clear(self) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)

    def _open_outdir(self) -> None:
        if self._last_out_dir and self._last_out_dir.exists():
            os.startfile(self._last_out_dir)  # type: ignore[attr-defined]


def main() -> None:
    app = ScoreAssistantApp()
    app.mainloop()


if __name__ == "__main__":
    main()
