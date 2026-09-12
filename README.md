# 乐谱助手 · 听曲目生成乐谱

基于论文 [MuScriptor: An Open Model for Multi-Instrument Music Transcription](https://arxiv.org/abs/2607.08168)
发布的开源模型，把一段音乐录音（流行、古典、摇滚等多乐器混音）自动转录为
**MIDI** 和**可读乐谱**（MusicXML + 总谱/分谱 PDF，吉他/贝斯另出六线谱）。

工具为中文图形界面，一次推理同时产出：

```
输出目录/
├── 曲名.mid            未量化 MIDI（保留原始时值，适合聆听、导入 DAW 二次编辑）
└── sheets/
    ├── score.mid       量化 MIDI（音符吸附到节拍网格）
    ├── score.musicxml  排版后的乐谱（可导入 MuseScore / Sibelius / Finale）
    ├── full_score.pdf  总谱（所有乐器在一页谱面上）
    ├── 01_*.pdf        各乐器分谱
    └── 01_*_tab.pdf    吉他/贝斯等拨弦乐器的六线谱
```

## 安装

需要：Python 3.10–3.12、[uv](https://docs.astral.sh/uv/)。

```bash
# 无 NVIDIA GPU（CPU 运行，建议选 small 模型）
uv sync

# 有 NVIDIA GPU（安装 CUDA 版 PyTorch）
UV_TORCH_BACKEND=cu128 uv sync      # Linux/macOS
set UV_TORCH_BACKEND=cu128 && uv sync   # Windows cmd
$env:UV_TORCH_BACKEND="cu128"; uv sync  # Windows PowerShell
```

## 首次使用：一项外部准备

1. **HuggingFace 授权（必需）**：模型权重为 CC BY-NC 4.0 许可（仅限非商业用途），
   下载前需登录 HuggingFace 并在模型页面接受许可
   （[medium](https://huggingface.co/MuScriptor/muscriptor-medium) /
   [large](https://huggingface.co/MuScriptor/muscriptor-large) /
   [small](https://huggingface.co/MuScriptor/muscriptor-small)，
   **每个规格需分别接受一次**），
   然后创建 [Access Token](https://huggingface.co/settings/tokens)。
   图形界面里的「登录设置…」按钮会引导完成这三步。
2. **MuseScore 4+（已随附，无需安装）**：生成乐谱 PDF 用 MuseScore 排版，
   便携版已放在 `vendor/musescore/`，工具会自动发现；
   若你自行安装了其他版本，也可在界面里手动指定。

## 使用

```bash
# 方式一：双击 启动乐谱助手.bat（Windows）

# 方式二：命令行启动图形界面
uv run python -m score_tool
```

界面操作：在「音频/链接」框里输入本地音频文件路径（wav/mp3/flac/m4a…，
可点「浏览…」多选）**或直接粘贴视频链接**——工具自动识别类型
（http(s) 开头按链接处理，支持 **B 站**（BV/av/b23.tv、?p=N 分集）和 **YouTube**
（youtube.com / youtu.be），音轨自动下载转 wav；
同一视频重复转录直接复用已下载音频，并显示下载进度百分比）。
输出目录留空时自动生成：本地文件为 `<文件名>_score`，链接为 `downloads/<视频标题>_score`。
选模型（默认 medium，GPU 建议 large）→（可选）限定乐器 → 开始转录。
首次转录会自动下载模型权重（medium 约 1.2 GB，large 约 5.6 GB），之后缓存本地。

转录完成后会自动切到「乐谱播放」标签页：

- ▶ 播放原音频，**当前发声的音符会在乐谱上实时高亮**，页面自动跟随滚动/翻页；
- **跟弹练习工具条**：变速不变调播放（0.5x–1.5x，预生成慢速版即点即用）；
  AB 段落循环反复练习；节拍器（按节拍网格混音，小节头重音）；预备拍（播放前一小节拍）；
  ◀/▶ 小节快速跳转；**阶梯提速**（AB 循环每过一遍自动 +10% 速度直至原速）；
- **MIDI 伴音**：勾选后用系统 GM 合成器同步播放转录结果，与原音频对比检查转录质量；
- **批量转录**：「浏览…」可多选文件，逐个自动转录（模型只加载一次），单个失败不影响后续；
- **钢琴键盘演示**：底部 88 键键盘实时点亮当前发声的琴键（按声部分色，
  可勾选「钢琴键盘」显示/隐藏，三种记谱法下都可用）；
- **音符瀑布流**：键盘正上方的深色区域里，未来 4 秒要弹的音符以色块落下，
  越过红线即当前音（按声部分色，x 轴与琴键一一对齐，可勾选「瀑布流」显示/隐藏）；
  跟弹时能提前看到接下来要按的键；
- **吉他指板演示**：底部为每个吉他/贝斯声部显示一块指板（六弦/四弦），
  播放时按声部分色点亮当前按下的（弦、品位），空弦音在琴枕处点亮，
  无法按出的音标 ×（可勾选「吉他指板」显示/隐藏）；
- **和弦进行实时提示**：顶部显示调性与当前和弦（含**调内级数**如 G(V)），
  进行序列随播放高亮推进，和弦标记同时叠加在乐谱对应位置上方（红色为当前和弦）；
  和弦由转录 MIDI 按节拍窗口做模板匹配识别，调性用 Krumhansl-Schmuckler 分析估计；
  多声部时和弦条下方显示**声部颜色图例**（与键盘/瀑布流/指板同色）；
- **五线谱 / 简谱 / 吉他谱切换**：工具栏「记谱」下拉切换。简谱按估计调性记谱
  （大调主音=1、小调按关系大调主音=6），含八度点、减时线/增时线、附点、
  跨拍连线、休止符与和弦叠置；吉他谱（六线谱 TAB）按标准调弦自动分配弦/品位
  （单音优先低把位、和弦不同弦且跨度 ≤5 品，贝斯自动用四线谱，
  低于调弦最低音的音标为 x）；三种记谱共享播放、高亮、和弦提示与点击跳转；
- **TAB 导出**：「导出 TAB」按钮把吉他/贝斯声部写成文本六线谱（.txt，
  每 4 小节一行、带小节号；sheets 目录里另有 MuseScore 排版的 TAB PDF）；
- 点击乐谱上任意音符可直接跳转到对应位置播放；和弦进行条中的和弦也可点击跳转；
  瀑布流中的音符同样可以点击跳转；五线谱视图下以蓝色竖线标记当前小节；
- 快捷键：空格播放/暂停，←/→ 快退/快进 5 秒；转录中「开始转录」按钮变为「取消」可随时中断；
- 模型、输出目录、乐器限定、记谱法、缩放等设置在关闭时自动记住；
- 「对齐」可微调音谱时间差（±50ms）；「缩放」调整乐谱大小（随窗口宽度自适应）；
- 进度条可拖动定位。

没有示例音频时，可以先生成一段：

```bash
uv run python examples/make_demo.py   # 生成 examples/demo.wav（17 秒示例曲）
```

## 提示与限制

- **节拍稳定的录音（跟着节拍器演奏的）效果最好**：乐谱需要把音符量化到节拍网格，
  自由速度（rubato）的录音排版效果会明显变差（论文与官方均有此说明）。
- **YouTube 下载**：YouTube 对数据中心 IP 常触发人机验证。若直连失败，
  可用浏览器扩展（如 Get cookies.txt LOCALLY）导出 cookies.txt 放到项目根目录
  或 `downloads/` 下（工具会优先使用），或配置代理（`HTTPS_PROXY` 环境变量）。
- 乐器限定能显著提高跨段落的乐器一致性：知道曲子里有哪些乐器时建议勾选。
- 模型权重仅限**非商业用途**（CC BY-NC 4.0）；本工具代码本身可自由使用。
- 模型对同一乐器同一音高的重叠音符无法区分（论文 4.2.3 节），极端情况下会丢音。

## 命令行（可选）

muscriptor 自带的 CLI 也已随依赖安装，例如：

```bash
uv run muscriptor transcribe examples/demo.wav -m large            # 只要 MIDI
uv run muscriptor transcribe examples/demo.wav -f sheets -o score/ # 乐谱目录
uv run muscriptor serve                                            # 官方 Web UI
```

## 项目结构

```
score_tool/
├── app.py        图形界面（tkinter，转录 + 播放器两个标签页）
├── worker.py     转录流程（含 B 站音频下载）：一次推理产出 MIDI 与乐谱
├── player.py     乐谱跟随播放器（verovio 排版 + Qt 渲染 + 音符高亮跟随）
├── jianpu.py     简谱（数字谱）排版与绘制
├── guitartab.py  吉他谱（六线谱 TAB）排版与绘制
├── chords.py     和弦识别（节拍窗口模板匹配）与调性估计
└── envcheck.py   环境检测（HuggingFace 授权 / MuseScore / GPU / ffmpeg）
examples/
├── make_demo.py        生成示例音频
└── smoke_full_flow.py  完整流程冒烟测试（需 pillow）
```

乐谱播放的实现：MusicXML 由 verovio 排版成 SVG 并给出「时间 → 音符元素」
映射，Qt 离屏把 SVG 渲染成 PNG 在界面里分页展示；播放时按音频进度查询当前
发声音符并高亮。音谱时间偏移按首个音符自动校准，可手动微调。
