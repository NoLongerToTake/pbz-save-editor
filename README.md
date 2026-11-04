# PBZ 存档编辑器 1.0 Beta

[下载 Windows x64 免安装版](https://github.com/NoLongerToTake/pbz-save-editor/releases/tag/v1.0.0-beta)。完整解压后双击“PBZ存档编辑器.exe”，无需另装Python或Rust。

《影之刃零》Windows 离线存档编辑器。支持分类物品编辑、支线任务记录与阶段编辑、奖励预览、自定义路径、自动寻档、备份和恢复。

## 1. 当前功能

- 790种物品、14类分类，包括书籍、书页和剧情物品。
- 62项支线目录；51项有已观察成功路线，29项可以选择阶段。
- 完成、完成到阶段、标记失败、回退未接取、仅补发奖励。
- 奖励按证据和阶段过滤，领取情况不明的数量奖励需要明确勾选确认。
- 所有操作先暂存，支持联合撤销；保存前自动备份，检测源文件是否被外部修改。
- 局部编辑JSON原文，保留未改浮点文本、其他任务和未编辑槽位密文。

只修改任务记录和明确选择的奖励。NPC、剧情、地图事件及材料交付暂不适配。书籍和剧情物品的持有状态不代表相关流程已经触发。

## 2. 从源码运行

已验证环境为Windows x64、Python 3.15（包含Tkinter）。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

从源码运行前，需要先按第4节构建解码器，或从免安装包中复制`bin/1031_pbz_decoder_v1.exe`到源码目录的`bin/`下。免安装包已经包含该组件，运行无需Rust；解码器源码及oozextract依赖位于`decoder/`、`third_party/oozextract/`。

启动后自动寻找存档，找不到才要求手动选择。也可以自行填写存档路径和备份目录。编辑正式存档前退出游戏，首次使用建议另存。

详细操作见[使用说明](docs/1104_使用说明_v2.md)，逐任务支持范围见[支线覆盖](docs/1104_支线覆盖_v2.md)。

## 3. 测试

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

测试使用合成数据和临时文件，不需要个人存档。需要原始本地化证据的检查在未提供证据时会跳过。

“不灭侠胆”已在存档副本中验证S1→1、最终奖励TU_19006×1、失败/回退/撤销、保存/恢复和重复补发判断。游戏内载入效果尚未验证。详见[验证范围](docs/VALIDATION.md)。

## 4. 构建解码器与便携包

如需自行重建解码器，请先安装Rust及适配的Windows编译工具链，然后在仓库根目录执行：

```powershell
cargo build --locked --release --manifest-path decoder/Cargo.toml
Copy-Item decoder/target/release/pbz_decoder.exe bin/1031_pbz_decoder_v1.exe
```

制作便携包时还需准备完整Python/Tk运行时到`runtime/`，并把Python依赖安装到`vendor/`，然后用C#编译器把`tools/Launcher.cs`编译为根目录下的`PBZ存档编辑器.exe`，最后运行`python tools/package_portable.py`。源码仓库不附带整个Python运行时。

`tools/build_catalog.py`、`tools/build_quests.py`是目录维护工具，重建需要另备游戏资产、映射和读取工具，并显式指定`--workspace`；日常运行不需要这些原始证据。

## 5. 仓库内容

```text
pbz_editor/       编辑器核心与中文界面
data/             物品、任务及奖励的派生目录
tests/            自动化测试
decoder/          Rust解码器入口与锁定依赖
third_party/      oozextract源码
bin/              本地构建或从免安装包提取的解码器（不提交二进制）
tools/            启动器、目录维护和打包工具
docs/             操作、支持范围与验证说明
```

仓库不包含个人存档、备份、账号登录信息或本机测试报告。目录中的资产引用和结构模板用于识别游戏数据，不包含游戏本体。第三方来源见[组件说明](THIRD_PARTY.md)。项目自身的开源许可证尚未指定。
