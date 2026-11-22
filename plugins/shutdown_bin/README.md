# shutdown_bin —— 关机清回收站（WBOX 插件）

版本 1.1.1 · API 1 · 适用 WBOX v2.13.0+（按 PLUGIN-SPEC.md 编写）

## 功能

- 监听系统关机通知（隐藏窗口接收 `WM_QUERYENDSESSION` / `WM_ENDSESSION`），
  在**关机 / 重启前**自动清空当前用户所有盘的回收站（无确认框、无进度条、无声音）。
- 两阶段清理：
  1. `WM_QUERYENDSESSION` 阶段立即开始清空并同步等待至多 4 秒——回收站不大时
     在系统询问阶段就清完，最可靠；
  2. 没清完则进入 `WM_ENDSESSION` 阶段：弹出一个小的**可见**等待窗口并
     `ShutdownBlockReasonCreate`，系统显示「此应用正在阻止关机」并等待；
     清完或超过「最长等待」（默认 20 秒，可调 5–60 秒）后自动放行。
- 注销默认不清理，可在设置里打开「注销时也清理」。
- 进程关机优先级设为 0x100（低于普通应用的 0x280），等大多数程序退完再清理。
- 每次清理结果（时间 / 清理前项数与体积 / 耗时 / HRESULT）写入
  `plugins\shutdown_bin\data\last_clean.json`，并记入 `guard.log` 与 `events.jsonl`。

## 为什么不用 on_exit 钩子 / 为什么要可见等待窗口

WBOX 的 `on_exit` 只在守护自身停止（stop.flag / 热重载 / fatal）时触发；
Windows 关机时守护进程被系统直接结束，钩子不会执行。因此插件在守护进程内
自建隐藏顶层窗口监听系统广播（消息窗口收不到广播，必须是顶层窗口）。
监听线程只在 `on_tick` 里惰性拉起：面板进程不会调用 `on_tick`，冷启动与
热重载（不重发 `on_start`）两种路径都能覆盖。

v1.0.0 实测教训（2026-11-22）：只在 `WM_ENDSESSION` 阶段清理、且窗口隐藏时，
`ShutdownBlockReasonCreate` 不会被系统理会——通知后约 5 秒进程就被杀，
清理没跑完。v1.1.0 改为两阶段：询问阶段先清（4 秒预算），清不完再靠
**可见**等待窗口触发系统的「阻止关机」等待机制。

## 设置项

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `shutdown_bin_enabled` | bool | true | 总开关，关闭后停止监听 |
| `shutdown_bin.max_wait_sec` | int | 20 | 清理最长等待秒数（5–60），超时先放行关机 |
| `shutdown_bin.clean_on_logoff` | bool | false | 注销时是否也清理 |

## 安装

面板标题栏「＋」选择 `shutdown_bin-v1.1.1.zip`（与 `.zip.sha256` 放同一目录），
确认来源后安装；或直接把 `shutdown_bin\` 目录复制到安装目录 `plugins\` 下，
再写 `plugins_reload.flag` 触发热重载。

## 注意

- **清空回收站是不可恢复的删除**，这正是本插件的唯一用途；除回收站外不碰其它文件。
- 关机被取消时，若清理已经开始则无法中止（系统 API 不支持取消），只会记一行日志。
- 插件运行在管理员守护进程内、无沙箱（规范 §0），请只安装自己信任来源的包。