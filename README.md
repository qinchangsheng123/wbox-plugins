# WBOX 插件库

[WBOX](https://github.com/qinchangsheng123/wbox) 的官方插件。主程序从 **v2.11.3**
起不再随安装包附带插件，功能全部由这里分发：装好主程序，再按需装插件。

插件 API 版本 **1**。当前这批插件与 WBOX **v2.14.1** 内核配套发布
（ace 2.2.0 起用到内核 `events` 模块，需要 v2.13.0 及更高版本）。

## 插件清单

每个插件一个 Release，标签是 `<id>-v<版本>`，资产里是 zip 和 `.sha256`。

| 插件 | 版本 | 作用 | 需要管理员 | 下载 |
|---|---|---|---|---|
| `afterburner` 小飞机联动启动 | 2.2.1 | 开游戏自动拉起 MSI Afterburner / RTSS，中途意外退出会自动重开，退出后优雅关闭；可设关闭倒计时 | 否 | [zip][r-ab] |
| `ace` ACE 防护 | 2.2.0 | 游戏期间压低反作弊进程的 CPU / 磁盘占用，退出后清理残留；判定留决策记录 | 是 | [zip][r-ace] |
| `popup` 弹窗关闭 | 2.1.0 | 游戏会话中关掉 ClientBrowser 广告 / 直播弹窗；观察模式只记录不动手 | 否 | [zip][r-pop] |
| `gamepp` 游戏加加联动 | 1.2.0 | 开游戏拉起游戏加加，退出后关闭；可设关闭倒计时 | 否 | [zip][r-gp] |
| `desktop_hud` 桌面悬浮窗 | 1.7.0 | 桌面半透明悬浮窗：CPU / GPU / 显存 / 内存占用与频率；显示位置可四边居中 | 否 | [zip][r-hud] |
| `shutdown_bin` 关机清回收站 | 1.1.2 | 关机 / 重启前自动清空回收站（两阶段，清不完才挂起并显示等待窗口），最长等待可调；注销默认不清 | 否 | [zip][r-sbin] |
| `idle_shutdown` 任务后自动关机 | 1.0.0 | 夜间等 WorkBuddy / Qoder 任务静默后关机；会话 + CPU + 键鼠三重判据，先见到任务才关；可选先关飞牛 NAS | 是 | [zip][r-isd] |
| `local_ai` 本地 AI 按需启停 | 1.0.4 | 有本地 AI 请求才拉起 llama.cpp 服务，请求结束且空闲后自动关闭；只关自己拉起的进程，手动启动的不动 | 否 | [zip][r-lai] |

[r-ab]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/afterburner-v2.2.1
[r-ace]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/ace-v2.2.0
[r-pop]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/popup-v2.1.0
[r-gp]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/gamepp-v1.2.0
[r-hud]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/desktop_hud-v1.7.0
[r-sbin]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/shutdown_bin-v1.1.2
[r-isd]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/idle_shutdown-v1.0.0
[r-lai]: https://github.com/qinchangsheng123/wbox-plugins/releases/tag/local_ai-v1.0.4

## 插件详解

### afterburner — 小飞机联动启动（v2.2.1）

**干什么**：开游戏自动拉起 MSI Afterburner（超频 / 风扇曲线 / 硬件监控），
连同 RTSS 帧率显示与录制组件一起管理；游戏全部退出后，把**本局拉起来的那一批**
优雅关闭。你自己常驻的小飞机不会被碰。

**怎么工作**：

1. 游戏开局（会话 `idle → active`）：小飞机没在跑就启动它，记下
   **pid + 创建时刻**作为身份；已经在跑就什么都不做（不重启、不接管你的实例）。
2. RTSS 组件按「本局起点」认领：只有本局开始**之后**创建的
   `rtss.exe` / `rtsshooksloader*` / `encoderserver*` / `desktopoverlayhost*`
   才算我们拉起的，赛前就在跑的组件不动。
3. 游戏途中若小飞机自己退出 / 被关：默认等 `ab_relaunch_sec`（30 秒）自动重开；
   设为 0 则保留旧行为（本局不再拉起）。重开前若它已自行恢复，或联动已关，
   待办自动取消。配置里填安装目录也能识别（自动补 `MSIAfterburner.exe`）。
4. 游戏全部退出：先等 `close_wait_sec`（避免误伤还在收尾的程序），再正常请求退出；
   `force_close` 开着时，没反应的才结束进程。若设了 `close_delay_sec`，
   退出后先倒计时，**期间回到游戏自动取消关闭**。
5. 关闭前逐个复核身份：pid 会被 Windows 复用，同名进程在同一个 pid 上重新出现时，
   创建时刻对不上就绝不关；拿不到创建时间的也一律不动（宁可不关，不能关错）。
   UAC 提权启动拿不到 pid 时按名字 + 创建时刻接管，早于本次启动的实例视为你自己的。

**设置项**（面板行尾滑块打开）：

| 设置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| 小飞机程序 `afterburner_path` | 路径 | 空 | 留空自动查找（卸载项 → 常见目录 → 运行中进程） |
| 一并管理帧率组件 `manage_rtss` | 开关 | 开 | 退出时连同本局新起的 RTSS / 录制组件一起收起 |
| 关不掉时强制结束 `force_close` | 开关 | 开 | 先正常请求退出，没反应再结束进程 |
| 退出后等待 `close_wait_sec` | 3–30 秒 | 6 | 游戏结束后等多久才开始关 |
| 关闭倒计时 `close_delay_sec` | 0–60 秒 | 0 | 退出后先倒计时再关，回到游戏自动取消；0 = 立即 |
| 掉线自动重开 `ab_relaunch_sec` | 0–300 秒 | 30 | 游戏途中意外退出后多久重开；0 = 本局不再重开 |

**绝不做什么**：不关你自己开的小飞机 / RTSS；不改小飞机任何配置与超频参数；
身份无法确认的进程一律不动。

**排查**：`guard.log` 里看「启动小飞机」「中途退出，N 秒后自动重开」「关闭判定」逐目标结果；
设置页「关于 → 导出诊断」打包全部日志与事件。

---

### gamepp — 游戏加加联动（v1.2.0）

**干什么**：开游戏自动拉起游戏加加（GamePP，游戏帧率 / 硬件监控 / 录像），
退出后只关**这次打开的那一批**（含它拉起的子程序）。你常驻的游戏加加不受影响。

**怎么工作**：

1. 路径自动探测，按优先级：配置指定 → 常见安装目录 → 卸载项注册表 → 运行中进程；
   命中来源写日志，排查「拉起的是哪一份」一目了然。
2. 开局记录家族基线：`gamepp.exe` / `gamepplite.exe` / `gamepplauncher.exe`
   里哪些 pid 赛前就在跑。本局新出现的才算我们的；基线 pid 被系统复用后
   按创建时刻复核，不再算基线。
3. 退出后等 `close_wait_sec` 再关；`manage_children` 开着时连同本局新起的
   子进程一起收；`close_delay_sec` 倒计时期间回到游戏自动取消。
4. 与 afterburner 同一套身份校验（pid + 创建时刻双确认，共用 `shared\proc_util.py`），
   身份不符不关，未关掉的进程与原因明确写日志。

**设置项**：

| 设置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| 游戏加加程序 `gamepp.path` | 路径 | 空 | 留空自动探测 |
| 一并关闭子程序 `gamepp.manage_children` | 开关 | 开 | 只关这次游戏拉起来的那一批，不动你自己开的 |
| 关不掉时强制结束 `gamepp.force_close` | 开关 | 开 | 先正常请求退出，没反应再结束进程 |
| 退出后等待 `gamepp.close_wait_sec` | 1–120 秒 | 6 | 游戏结束后等多久才开始关 |
| 关闭倒计时 `gamepp.close_delay_sec` | 0–60 秒 | 0 | 退出后先倒计时再关，回到游戏自动取消；0 = 立即 |

**绝不做什么**：不关赛前就在跑的游戏加加家族进程；不碰身份未确认的进程；
不改游戏加加自身设置。

---

### ace — ACE 防护（v2.2.0，需要管理员）

**干什么**：腾讯 ACE 反作弊（`SGuard64` / `SGuardSvc64` / `ACE-Service64` 等）
在游戏期间常驻且吃 CPU / 磁盘。本插件做两件事：

1. **游戏期间限速**：把 ACE 进程的 CPU 优先级降到空闲、磁盘 IO 降到 very-low，
   改完读回校验；游戏退出后恢复。反作弊功能不受影响——只调优先级，
   **不注入、不改文件、不拦截通信**。
2. **退出后清理残留**：游戏退干净后，ACE 有时留着进程 / 服务不退。
   按四道闸门决定动不动手：

| 闸门 | 默认 | 作用 |
|---|---|---|
| 宽限期 `grace_sec` | 180 秒 | 游戏退出后先等等，防切局误判 |
| ACE 保护期 `min_ace_age_sec` | 300 秒 | 刚启动的 ACE 不动，避免打断初始化 |
| 更新后静默 `quiet_after_update_sec` | 30 秒 | 见到 `SGuardUpdate64` 等更新组件就跳过本轮 |
| 冷却期 `cooldown_sec` | 600 秒 | 两次清理的最小间隔 |

**决策记录（v2.2.0 新增）**：每一轮判定都留一条结构化记录——清 / 不清 / 为什么、
四道闸门当时的取值、目标身份（名字 + pid + 已运行秒数 + 当前 CPU/IO 状态）。
最近 20 条落盘，`--self-check` 与「导出诊断」直接读；关键判定同时写内核
`events.jsonl`。「退出后为什么没清」不再靠猜。

**设置项**：

| 设置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| 游戏期间降低占用 `ace_throttle.enabled` | 开关 | 开 | CPU 空闲优先级 + 磁盘 very-low |
| 退出后清理残留 `ace_cleanup.enabled` | 开关 | 开 | 四道闸门全过才动手 |
| 开始前等待 `ace_throttle.delay_sec` | 0–60 秒 | 20 | 检测到游戏后等多久再限速，避免和启动过程抢资源 |

闸门秒数、进程 / 服务名单等高级参数在 `config.json` 的 `ace_throttle` /
`ace_cleanup` 段（默认值见主程序 `core_defaults.py`，随 exe 冻结，插件改不动）。

**绝不做什么**：取不到进程创建时间一律判 YOUNG 不动手（宁可漏清不误清）；
`dry_run` 可只判定不执行；更新组件 / 登录器（`TASLogin` / `TP3Helper` 等）
在 blockers 名单，见到就跳过本轮。

---

### popup — 弹窗关闭（v2.1.0）

**干什么**：WeGame 系游戏退出后会弹直播 / 赛事 / 广告窗口。宿主是游戏目录下的
`clientbrowser2\ClientBrowser.exe`（类名 `#32770`、无标题，靠窗口标题抓不到），
本插件按 **exe 名 + 路径片段**双命中定位它。

**怎么工作**（两段式）：

1. **游戏会话中**：弹窗窗口比「游戏全部退出」早约 7 秒就显示。窗口一露面立刻发
   `WM_CLOSE`（`early_close_enabled`，最多重试 `early_max_tries` 轮）——
   只关窗口、**不动进程**（ClientBrowser 还承载游戏内嵌页面）。
2. **游戏全退后**：结束宿主进程及其整个 CEF 子进程树（`kill_tree`），
   随后 `watch_sec` 秒内继续拦截复活的新窗口。

**观察模式（v2.1.0 新增）**：`observe_only` 打开后，窗口拦截 / 提前监视 /
进程树清理三条路径**全部只判定、只记录，不动手**。怀疑误拦时先开一轮观察，
看「本应关闭谁」再决定。最近 20 条动作记录（进程名 / 标题 / pid / 命中规则）
落盘到 `popup_recent`，面板与诊断包可读；`--self-check` 也会输出观察模式状态、
保护名单完整性检查与最近 3 条动作。

**设置项**：

| 设置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| 游戏中提前关窗 `popup_guard.early_close_enabled` | 开关 | 开 | 窗口一露头就关，不等游戏退出 |
| 退出后清后台 `popup_guard.kill_tree` | 开关 | 开 | 宿主进程 + CEF 子进程树一起收 |
| 退出后盯多久 `popup_guard.watch_sec` | 30–300 秒 | 90 | 过期自动收摊 |
| 记录所有新窗口 `popup_guard.log_all_new` | 开关 | 关 | 排查漏拦时开，平时别开（噪声大） |
| 观察模式 `popup_guard.observe_only` | 开关 | 关 | 只记录本应关闭的窗口和进程，不动手 |

**保护名单（绝不碰）**：`explorer.exe` / `wegame.exe` / `wegame_env.exe` /
`tcls_core.exe` / `tgame.exe` / `qq.exe` / `wechat.exe` / `weixin.exe` /
`tim.exe` / `qqprotect.exe`——它们的窗口也常带「直播 / 赛事」字样，
但结束它们你会直接掉线。名单是安全并集：用户配置删不掉默认保护项。

---

### desktop_hud — 桌面监控悬浮窗（v1.7.0）

**干什么**：桌面上一块半透明悬浮窗，实时显示 CPU / GPU / 显存 / 内存占用，
可选 CPU / GPU 频率。逐项开关、四角摆放、琥珀 / 红色阈值告警；
不遮挡其他窗口，进游戏自动隐藏或停采样。

**怎么工作**：

- 采样走 Windows PDH（GPU Engine / Adapter Memory 计数器），多显卡机器可选
  「显存最大」或「占用最大」的那张卡，右键菜单可精确指定具体显卡；
  PDH 不可用时回落 DXGI / ADL，失败原因写诊断。
- **v1.6.1 采样线程重入修复**：采样器（PDH / ADL 句柄）由采样线程自己创建与关闭，
  窗口线程不再共享句柄。旧线程卡在驱动调用里没退出时，恢复监控只登记「待恢复」，
  等旧线程退出再起新线程——不会出现两条线程同读一组句柄的未定义行为。
- 桌面常驻模式下悬浮窗不置顶：其他窗口未最大化时在空隙可见，但绝不遮挡窗口；
  关闭该模式则始终置顶。鼠标穿透打开后点悬浮窗等于点桌面。
- 只声明 `process.watch` 权限：**不关任何进程、不改任何系统设置**。

**设置项**：

| 组 | 设置 | 说明 |
|---|---|---|
| 显示项 | 处理器占用 / 显卡占用 / 显存占用 / 内存占用 / 处理器频率 / 显卡频率 | 六个独立开关 |
| 游戏行为 | 游戏中自动隐藏 | 只藏窗口，采样继续 |
| | 游戏中停止监控 | 停采样 + 隐藏，退出自动恢复（比隐藏更省） |
| | 桌面常驻模式 | 不遮挡窗口；关闭则始终置顶 |
| | 鼠标可穿透 | 点击穿透到下层窗口 |
| 外观 | 显示位置 | 右上 / 左上 / 右下 / 左下 / 四边居中（v1.7.0 起） |
| | 刷新间隔 | 200–5000 毫秒，默认 1000 |
| | 琥珀阈值 / 红色阈值 | 默认 70% / 90%，数值变色告警 |
| | 百分比小数位 | 0（整数）或 1 位小数 |
| 显卡 | 目标显卡 | 自动·显存最大 / 自动·占用最大；精确选卡用右键菜单 |

悬浮窗右键菜单与面板设置等价：逐项开关 / 换角 / 穿透 / 目标显卡，改动立即生效。

### shutdown_bin — 关机清回收站（v1.1.2）

**干什么**：关机 / 重启前自动清空当前用户所有盘的回收站，无确认框、无进度条、
无声音。注销默认不清，可在设置里打开。

**怎么工作**：

- WBOX 的 `on_exit` 在 Windows 关机时不会触发（守护被系统直接结束），所以插件
  在守护进程里自建隐藏顶层窗口，监听 `WM_QUERYENDSESSION` / `WM_ENDSESSION`。
- 两阶段清理：`WM_QUERYENDSESSION` 阶段立即开清并同步等至多 4 秒（低于系统
  5 秒的挂起判定），回收站不大时在这里就清完；没清完才进 `WM_ENDSESSION`
  阶段，弹出小的**可见**等待窗口并 `ShutdownBlockReasonCreate`，系统显示
  「此应用正在阻止关机」并等待，清完或超过「最长等待」（默认 20 秒，可调
  5–60 秒）后自动放行。
- 同一轮关机只清一次：阶段一已清完时阶段二不再重起（v1.1.1），关机被取消
  则本轮记录作废，下次关机重新清。
- 回收站本来就空时 `SHEmptyRecycleBinW` 返回 `0x8000FFFF`，按「无需清理」记
  成功（v1.1.2），不再发假的 `CLEAN_FAIL`。
- v1.0.0 实测教训（2026-11-22）：只在 `WM_ENDSESSION` 清理、且窗口隐藏时，
  `ShutdownBlockReasonCreate` 不被系统理会，通知后约 5 秒进程就被杀、清理没
  跑完。所以 v1.1.0 起改成两阶段 + 可见等待窗口。
- 进程关机优先级设为 0x100（低于普通应用的 0x280），等大多数程序退完再清理，
  尽量把它们退出前删进回收站的东西一并清掉。
- 监听线程只在 `on_tick` 里惰性拉起：面板进程不调用 `on_tick`，冷启动与热重载
  （不重发 `on_start`）两条路径都覆盖，不会误建窗口。
- 结果写 `plugins\shutdown_bin\data\last_clean.json`，同时进 `guard.log` 与
  `events.jsonl`（时间 / 清理前项数与体积 / 耗时 / HRESULT）。

**设置项**：

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `shutdown_bin_enabled` | bool | true | 总开关，关闭后停止监听 |
| `shutdown_bin.max_wait_sec` | int | 20 | 清理最长等待秒数（5–60），超时先放行 |
| `shutdown_bin.clean_on_logoff` | bool | false | 注销时是否也清理 |

**注意**：清空回收站是不可恢复的删除，这正是本插件的唯一用途；除回收站外
不读写任何其它文件。只声明 `files.any` 权限，不需要管理员。

### idle_shutdown — 任务后自动关机（v1.0.0，需要管理员）

**干什么**：把本机 `shutdown_after_midnight.py` v1.3.0 搬进 WBOX。夜间任务跑完后
不用守着屏幕，插件检测「任务真的结束了」再发起关机；原来的 `ShutdownAfterMidnight`
计划任务可以停用，避免两套逻辑各关一次。

**怎么判**：三条同时满足并持续 10 分钟（可调）——
① `~\.workbuddy\projects` 与 `~\.qoder\projects` 下的会话 jsonl 不再写入
（日志目录是心跳，不看）；② WorkBuddy / Qoder 进程组 CPU < 8%（没开算空闲）；
③ 键鼠空闲。再加两道闸门：必须在 00:00–08:00（可调），且本窗口内确实见到过任务在忙。
单轮忙不算忙，连续两轮才清零计时；探测失败一律按忙处理。

**怎么关**：命中后先关飞牛 NAS（可选，调用旧脚本目录的 `nas_poweroff.py`，
SSH 口令由它在运行时从本机凭证文件读取，不进插件包），再走 `InitiateSystemShutdownExW`，失败回退
`shutdown /s /t`。倒计时用 Windows 自带窗口；写 `stop.flag` / `.cancelled`、
在面板关掉插件、或在倒计时窗口点取消，都会调用 `AbortSystemShutdownW` 撤销，
并用代次号拦住已排队的关机线程。

| 设置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| 启用 | 开关 | 开 | 总开关 |
| 时间窗 | 选择 | 夜间 00:00–08:00 | 另有晚间 20:00–23:59、全天 |
| 静默多久后关机 | 整数 | 10 分钟 | 三项判据同时满足后计时 |
| 关机倒计时 | 整数 | 60 秒 | Windows 倒计时，期间可取消 |
| 必须先见到任务在跑 | 开关 | 开 | 防空窗夜误关，不建议关 |
| 关机前先关飞牛 NAS | 开关 | 关 | 失败只记日志，不拦本机 |

探测与关机都在后台线程，on_tick 只做计数；窗口外、取消标记存在、
插件被禁用三种情况下不会发起任何关机。`dry_run` 可在 `config.json` 里开，
只记日志不真关。

### local_ai — 本地 AI 按需启停（v1.0.4）

**干什么**：本地 llama.cpp 冷启动约 40 秒，Q5 + 64K 上下文要占 10 GB 以上显存。
本插件让它**只在有请求时运行**：请求结束、`/slots` 全部空闲并持续一段时间后自动关闭，
显存还给游戏。**用户自己启动的服务不会被插件关闭**，只在面板里显示为 `external`。

**触发协议**：请求方在调用模型前写 `local_ai.signal_path`（默认
`~/.workbuddy/scripts/local_ai_request.json`）：

```json
{"action":"start","client":"local_ai_prep.py","pid":12345,"ts":1764000000}
```

插件看到文件 → 拉起 `llama-server.exe` → 轮询 `/health` 就绪 → 写同目录
`local_ai_ready.json`（`starting` / `ready` / `error` / `stopped`）。调用方用完删掉请求文件，
插件确认没有在处理的任务后再关。

**状态机**：`idle → starting → ready → idle`；已探测到外部服务走 `external`（连续 3 次
探测不到，约 6 秒退回 `idle`）；自己拉起的进程在 `ready` 下退出会立即清状态，
不会让 `ready.json` 悬着。

| 设置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| 启用 | 开关 | 开 | 总开关 |
| 空闲多久后关闭 | 整数 | 120 秒 | 请求消失且无在处理任务后计时 |
| 启动等待上限 | 整数 | 120 秒 | 超时判定失败并终止进程 |
| 启动上下文长度 | 整数 | 8192 | 8192 足够摘要类任务，64K 冷启动明显变长 |

**路径要自己填**：`local_ai.exe_path` / `local_ai.model_path` / `local_ai.mmproj_path` /
`local_ai.signal_path` 的默认值只是示例布局，装好后请在 `config.json` 改成自己机器上的
llama.cpp 目录与权重文件。启动参数固定带 `--host 127.0.0.1`、`-np 1`，不会监听外网。

## 安装

**方式零：从 Releases 下载（推荐给普通用户）**

打开 <https://github.com/qinchangsheng123/wbox-plugins/releases>，按插件名找到对应
Release，下载 `<id>-v<版本>.zip`（**不要解压**），然后照下面「方式一」装进面板。

**方式一：面板安装**

1. 打开 WBOX 主窗口，点右上角 **＋**；
2. 选刚下载的 `<id>-v<版本>.zip`；
3. 确认框会显示发布者、权限、文件数与整包 SHA-256，确认后立即生效
   （游戏会话中会等本局结束再热重载，不打断限速 / 弹窗监视）。

**方式二：手动放目录**

解压后把 `<id>\` 整个目录放进 `%LOCALAPPDATA%\WBOX\plugins\`，再让守护重扫插件。

**校验（可选）**：每个包旁边有 `<包名>.zip.sha256`，安装界面会自动比对。
侧车只能证明"和发布时是同一份文件"，不是签名，也不证明作者身份。

## 安全边界

插件是**在 WBOX 守护进程（管理员权限）里运行的 Python 代码**，装进来就等于
完全信任作者。主程序能拦路径穿越、超大包、可执行文件，但拦不住插件代码本身
做什么。装第三方插件前先读源码。

内核侧的护栏（v2.13.0）：钩子超 5 秒记警告、连续 3 次慢钩子降级隔离；
关进程前统一「pid + 创建时刻」身份复核；插件声明的权限只展示不授权。

## 目录结构

```
plugins\<id>\        插件源码（plugin.json + plugin.py + 资源）
shared\              afterburner / gamepp 共用的 proc_util.py（唯一正文）
dist\                发布用插件包（最新版）+ .sha256 侧车
tools\pack.py        打包 / 校验工具（独立实现，不依赖主程序）
```

插件规范在主程序仓库：`docs/PLUGIN-SPEC.md`（钩子耗时约定 §5.6、事件流 §9.6）。

## 打包

```bat
python tools\pack.py                              :: 全部 -> dist\
python tools\pack.py plugins\ace                  :: 单个
python tools\pack.py --check dist\ace-v2.2.0.zip  :: 只校验
```

产物与会话目录里的运行期文件（`__pycache__`、`data\`、`*.log`）一律不进包。

## 版本

插件版本独立演进，遵循三段式 `vMAJOR.MINOR.PATCH`；同一插件的旧版本包不保留，
`dist\` 里永远只有最新版，发布到 Releases 的 tag 记作 `<id>-v<版本>`
（例如 `ace-v2.2.0`），一个插件一个 Release，不打包捆绑。