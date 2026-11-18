# WBOX 插件库

[WBOX](https://github.com/qinchangsheng123/wbox) 的官方插件。主程序从 **v2.11.3**
起不再随安装包附带插件，功能全部由这里分发：装好主程序，再按需装插件。

插件 API 版本 **1**，适用于 WBOX v2.11.3 及更高版本。

## 插件清单

每个插件一个 Release，标签是 `<id>-v<版本>`，资产里是 zip 和 `.sha256`。

| 插件 | 版本 | 作用 | 需要管理员 | 下载 |
|---|---|---|---|---|
| `afterburner` 小飞机联动启动 | 2.1.1 | 开游戏自动拉起 MSI Afterburner / RTSS，退出后优雅关闭 | 否 | [zip][r-ab] |
| `ace` ACE 防护 | 2.1.1 | 游戏期间压低反作弊进程的 CPU / 磁盘占用，退出后清理残留 | 是 | [zip][r-ace] |
| `popup` 弹窗关闭 | 2.0.1 | 游戏会话中关掉 ClientBrowser 广告 / 直播弹窗 | 否 | [zip][r-pop] |
| `gamepp` 游戏加加联动 | 1.1.1 | 开游戏拉起游戏加加，退出后关闭 | 否 | [zip][r-gp] |
| `desktop_hud` 桌面悬浮窗 | 1.5.1 | 桌面上显示帧率 / 占用等指标 | 否 | [zip][r-hud] |

[r-ab]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/afterburner-v2.1.1
[r-ace]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/ace-v2.1.1
[r-pop]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/popup-v2.0.1
[r-gp]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/gamepp-v1.1.1
[r-hud]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/desktop_hud-v1.5.1

## 安装

**方式零：从 Releases 下载（推荐给普通用户）**

打开 <https://github.com/qinchangsheng123/wbox-plugins/releases>，按插件名找到对应
Release，下载 `<id>-v<版本>.zip`（**不要解压**），然后照下面「方式一」装进面板。

**方式一：面板安装**

1. 打开 WBOX 主窗口，点右上角 **＋**；
2. 选刚下载的 `<id>-v<版本>.zip`；
3. 确认框会显示发布者、权限、文件数与整包 SHA-256，确认后立即生效。

**方式二：手动放目录**

解压后把 `<id>\` 整个目录放进 `%LOCALAPPDATA%\WBOX\plugins\`，再让守护重扫插件。

**校验（可选）**：每个包旁边有 `<包名>.zip.sha256`，安装界面会自动比对。
侧车只能证明"和发布时是同一份文件"，不是签名，也不证明作者身份。

## 安全边界

插件是**在 WBOX 守护进程（管理员权限）里运行的 Python 代码**，装进来就等于
完全信任作者。主程序能拦路径穿越、超大包、可执行文件，但拦不住插件代码本身
做什么。装第三方插件前先读源码。

## 目录结构

```
plugins\<id>\        插件源码（plugin.json + plugin.py + 资源）
shared\              afterburner / gamepp 共用的 proc_util.py（唯一正文）
dist\                发布用插件包（最新版）+ .sha256 侧车
tools\pack.py        打包 / 校验工具（独立实现，不依赖主程序）

插件规范在主程序仓库：docs/PLUGIN-SPEC.md。
```

## 打包

```bat
python tools\pack.py                              :: 全部 -> dist\
python tools\pack.py plugins\ace                  :: 单个
python tools\pack.py --check dist\ace-v2.1.1.zip  :: 只校验
```

产物与会话目录里的运行期文件（`__pycache__`、`data\`、`*.log`）一律不进包。

## 版本

插件版本独立演进，遵循三段式 `vMAJOR.MINOR.PATCH`；同一插件的旧版本包不保留，
`dist\` 里永远只有最新版，发布到 Releases 的 tag 记作 `<id>-v<版本>`
（例如 `ace-v2.1.1`），一个插件一个 Release，不打包捆绑。
