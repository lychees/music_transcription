"""完整流程冒烟测试：模拟用户点「开始转录」，验证转录→播放器加载→高亮。"""

import time
import shutil
from pathlib import Path

from PIL import ImageGrab

from score_tool.app import ScoreAssistantApp

app = ScoreAssistantApp()
app.update()
time.sleep(2)
app.update()

shutil.rmtree("test_output/flow", ignore_errors=True)  # 避免「目录非空」确认框
app.audio_var.set("examples/demo.wav")
app.out_var.set("test_output/flow")
app.sheets_var.set(True)
app._start()

deadline = time.time() + 180
while time.time() < deadline:
    app.update()
    time.sleep(0.5)
    if app.player.audio is not None:
        break

assert app.player.audio is not None, "播放器未加载"
assert app.player.model is not None, "乐谱模型未加载"
print("pages:", app.player.model.page_count)
print("duration:", app.player.audio.duration)
print("offset_s:", app.player.offset_s)
print("chords:", " | ".join(s.label for s in app.player.chord_spans))
print("key label:", app.player.key_label.cget("text"))

# 模拟播放中，截图验证高亮与和弦提示
app.nb.select(app.player.frame)
app.player.audio.seek_seconds(10.5)
app.player.audio.playing = True
time.sleep(0.5)
app.update_idletasks()
app.update()
print("highlights:", len(app.player._highlight_items))
print("current chord:", app.player.chord_label.cget("text"))

app.attributes("-topmost", True)
app.lift()
app.update()
time.sleep(0.5)
x, y = app.winfo_rootx(), app.winfo_rooty()
w, h = app.winfo_width(), app.winfo_height()
ImageGrab.grab(bbox=(x, y, x + w, y + h)).save("test_output/gui_flow.png")
print("saved test_output/gui_flow.png")

app.player.audio.playing = False
app.destroy()
