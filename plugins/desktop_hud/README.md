# 桌面监控（desktop_hud）

在桌面显示一块半透明小面板，实时显示 **CPU / GPU / 显存 / 内存** 占用，
可选显示 **CPU / GPU 频率**。每一项都有独立显示开关。桌面常驻：其他窗口
未最大化时在空隙里照常可见、但绝不遮挡窗口（v1.2.0 起挂为桌面壁纸层子窗）。
游戏会话中自动隐藏 / 停止监控（v1.3.0 起可停采样，交给游戏内其他监控软件；
与 RTSS 游戏内 OSD 互补，回到桌面自动恢复）。

纯 ctypes + 标准库实现，无第三方依赖、不携带任何二进制；悬浮窗跑在独立线程，
不阻塞守护主循环。

> 开发注意：live 的 Wingman.exe 是裁剪过的冻结运行时，标准库只打包了一部分
> （实测 PYZ 含 `threading`/`ctypes`/`json`，**不含 `queue`**）。插件只能依赖
> 已打包模块；跨线程队列用 `threading.Lock` + list 代替 `queue.Queue`，
> 否则面板会报「未能加载：ModuleNotFoundError: No module named 'queue'」。
>
> 热重载注意：宿主只重新 exec entry（plugin.py），`from . import hud_overlay`
> 默认命中 sys.modules 里上一轮的旧兄弟模块。plugin.py 顶部主动 pop 掉
> `hud_overlay`/`hud_metrics` 缓存，保证改动能真正生效（否则右键修复等改动
> 在 live 进程里不生效，独立新进程测试却正常）。

## 默认行为

- 位置：桌面右上角（工作区内，避开任务栏）。
- 刷新：1000 毫秒（`refresh_ms`，200..5000 可调）；外观：深色半透明、等宽
  字体、占用 ≥70% 琥珀 / ≥90% 红（阈值可配）。
- 显示项：CPU / GPU / 显存 / 内存 占用开；CPU / GPU 频率**默认关**。
- 游戏中自动隐藏：开。鼠标穿透：关。
- 游戏中停止监控：开（会话中停采样并隐藏，退出立即补采样恢复；关闭则仅按
  「自动隐藏」处理，采样继续）。
- 桌面常驻：开（挂为桌面壁纸层 WorkerW 的子窗：高于壁纸、低于一切应用
  窗口，Win+D 不最小化）。关闭则回顶层 + 始终置顶。
- 目标显卡：自动（DXGI 显存总量最大的那张）；多卡机器可在右键菜单里指定。

## 右键菜单

在面板上右键：

- 逐项勾选：CPU 占用 / GPU 占用 / 显存占用 / 内存占用 / CPU 频率 / GPU 频率
- 游戏中自动隐藏、游戏中停止监控、桌面常驻（不遮挡窗口）、鼠标穿透
- 目标显卡：自动（显存总量最大）/ 自动（当前占用最大）/ 指定某一张卡
  （枚举出本机所有活动适配器，名称后面带显存总量）
- 位置：右上 / 左上 / 右下 / 左下 / 顶部居中 / 底部居中 / 左侧居中 / 右侧居中
  （也可直接左键拖拽到任意位置）
- 诊断信息写入日志（把采样后端分项状态、目标显卡、最近错误、采样耗时写进
  `guard.log`，一条一行）
- 打开配置文件、恢复默认、关闭悬浮窗

菜单与拖拽的改动**立即生效**并持久化；「恢复默认」回到 config.json 的基础值。

> 鼠标穿透开启后右键不再响应（鼠标事件穿透到桌面）。如需改回，请在面板
> 关闭本插件开关再打开，或编辑 config.json 的 `desktop_hud.click_through`。

## 配置（config.json）

```jsonc
{
  "desktop_hud_enabled": true,        // 总开关（面板拨动）
  "desktop_hud": {
    "show_cpu": true, "show_gpu": true, "show_vram": true, "show_ram": true,
    "show_cpu_freq": false, "show_gpu_freq": false,
    "hide_in_game": true, "pause_in_game": true,
    "desktop_only": true, "click_through": false,
    "position": "top-right",          // 四角(top/bottom-left/right)、四边居中(top/bottom/left/right-center)、custom
    "x": 0, "y": 0,                   // position=custom 时的坐标
    "opacity": 215,                   // 0..255
    "refresh_ms": 1000,               // 200..5000（v1.6.0 起取代 refresh_sec）
    "warn_percent": 70, "high_percent": 90,  // 染色阈值
    "pct_decimals": 0,                // 0 / 1
    "gpu_select": "auto",             // auto(显存最大) / load(占用最大) / luid(指定)
    "gpu_luid": "",                   // gpu_select=luid 时的目标 LUID（右键菜单写入）
    "font_size": 14, "margin": 16
  }
}
```

> v1.6.0 起 `refresh_sec`（秒，浮点）迁移为 `refresh_ms`（毫秒，整数）：旧的
> 配置和旧运行期快照在首次加载时自动换算，两项同时存在时以 `refresh_ms` 为准。

运行期菜单 / 拖拽覆盖保存在 `plugins\desktop_hud\data\hud.json`
（`{"prefs":…, "hidden":…}`），优先级高于 config.json；删除该文件即回到配置基础值。

## 取数口径与限制

- CPU 占用：`GetSystemTimes` 差分；CPU 频率：注册表标称 `~MHz` ×
  PDH `% Processor Performance`（与任务管理器「速度」口径一致）。
- 内存：`GlobalMemoryStatusEx`。
- GPU 占用 / 显存占用：PDH `GPU Engine` / `GPU Adapter Memory`。**读哪张卡**
  由 `gpu_select` 决定：`auto` = DXGI 枚举里显存总量最大的活动适配器（不是
  「此刻占用最高」——双卡机上浏览器 / 录屏 / 编码器会把读数抢走）；`load`
  = 旧规则（显存占用最大）；`luid` = 右键菜单指定的具体卡。
  LUID → 显卡名的映射来自 `dxgi.dll`（`CreateDXGIFactory1` +
  `EnumAdapters1` + `GetDesc`，纯 ctypes，实测一次枚举 ~0.12 ms）。
- 显存总量：注册表 `HardwareInformation.qwMemorySize`（WMI AdapterRAM 4GB 封顶，弃用）。
- **GPU 频率仅 AMD 且 ADL 可用时有效**，默认关闭。v1.4.0 起优先走
  `ADL2_New_QueryPMLogData_Get`（PMLog，Navi/RDNA 起），传感器 `CLK_GFXCLK`
  单位已是 MHz；老驱动无 PMLog 时回退 `Overdrive5_CurrentActivity_Get`
  （`iEngineClock` 单位 10kHz，需 /100）。非 AMD 显卡显示「-」。
  本机 RX 9070 GRE（RDNA4，驱动 32.0.31019.2002）实测：Overdrive5 恒返回 -1，
  PMLog 正常，空闲约 110–140 MHz。
- 目标卡失效自愈：连续 3 轮在当前读数里都找不到目标 LUID（睡眠恢复 / 驱动
  重启 / 拔卡）就作废重选；PDH 建查询或收集失败按 20 秒退避重建，不再
  「一次失败、整场显示 -」。
- 多显示器时角落位置取**窗口所在显示器**的工作区；拖拽可放到任意显示器。

## v1.4.0 变更（修复）

- `_cdll()` 误用未定义的 `_cdecl`（实际定义为 `_CDECL`）→ NameError 被宽
  except 吞掉，ADL 从未初始化成功，GPU 频率在任何 AMD 卡上都显示「-」。已修正。
- GPU 频率改走 PMLog（RDNA4 上 Overdrive5 不支持），量纲 MHz 不再除 100。
- 刷新节拍不再被守护 tick 劫持：`set_prefs` 内容未变不投递，`_apply_prefs`
  只在 `refresh_sec` 真变化时 `SetTimer`（同 id 重装会重置计时，0.4s 节拍
  下 1s 定时器实测 12s 内 0 次触发）。
- `on_exit` 先排空请求队列再停窗：菜单/拖拽改动不再因 30s 空闲节拍而丢失。
- `--self-test` 新增 `gpu_clock_error=`，区分「后端正常」与「初始化失败」。

## v1.5.0 变更（性能 / 多屏）

- GDI 不再每 tick 全量重做：
  - `_make_font` 缓存 `(字号px, dpi)`，未变不 `DeleteObject`+`CreateFontW`；
  - `_relayout` 缓存**各行字符数**（Consolas 等宽且全 ASCII，宽度只取决于
    字符数，不取决于具体数字），位数没变就跳过 `GetDC` + 6 次
    `GetTextExtentPoint32W`；量文本失败时不写缓存，下次重量；
  - `_reposition` 记住上次 `(x, y, w, h)`，未变不 `SetWindowPos`；
  - `_paint` 的背景刷改为成员缓存，`_teardown` 里统一释放（原来每帧建了再删）。
- 角落定位改用 `MonitorFromWindow` + `GetMonitorInfoW` 取**窗口所在显示器**的
  工作区。原来只用 `SPI_GETWORKAREA`，它只返回主屏工作区，副屏上选
  「右上/右下」会落到主屏坐标。拿不到显示器信息时回退旧行为。
- 热重载摘模块缓存只摘**本包内**的名字（`__package__ + '.hud_overlay'`）。
  原来无条件 pop 顶层 `hud_overlay`/`hud_metrics`，会踢掉其它插件的同名模块。
  离线独立导入（无包上下文）时仍回退顶层名。

## v1.6.0 变更（线程隔离 / 选卡 / 诊断）

- 采样搬进独立线程（`hud-sampler`）：窗口线程只读最近一次快照并重绘，
  不再在 `WM_TIMER` 里调 PDH/ADL。原实现里驱动更新、睡眠恢复、GPU 计数器
  提供者重启都可能让一次采样卡住几百毫秒到数秒，直接把窗口一起冻住（重绘、
  右键菜单、拖动全停）。建窗路径也不再等首帧采样——实测首帧（PDH 开查询 +
  ADL 初始化）384 ms，而 `start()` 只等 2 s，本机 2026-11-19 17:56 的
  `guard.log` 里就出现过一次「悬浮窗创建失败（桌面会话不可用？）」随后 2 s
  才建好。
- 慢采样退避：单次耗时 > max(40ms, 节拍一半) 判慢，本轮按 2 倍耗时退避
  （上限 5s），日志 60 秒去重（慢一次记一行，恢复再记一行）。
- 选卡规则可改可见：`auto` / `load` / `luid`（右键「目标显卡」子菜单，
  逐项勾选，名称带显存总量）。目标 LUID 连续 3 轮消失就重选，PDH 失败按
  20 秒退避重建（原来 `_failed` 一旦置位永不重试）。
- 诊断分项：`status()` / `--self-test` / 右键「诊断信息写入日志」给出
  PDH 建查询与收集返回码、重建次数、目标卡与来源、适配器清单（含显存）、
  最近错误与最近事件、单次采样耗时与慢事件计数。界面不再只有一个「-」。
- 阈值与精度：`warn_percent` / `high_percent` / `pct_decimals` /
  `refresh_ms` 进 `settings`，可在面板设置里调；`refresh_sec` 自动迁移。
- 顺带修掉一个日志误导：启动瞬间不再打印「恢复采样（游戏会话）」，
  现在只在采样状态真的翻转时记一行「暂停 / 恢复（游戏会话结束）」。
- 屏外坐标回收：`position=custom` 存下来的坐标如果在可见虚拟屏之外（拔掉副屏、
  改过分辨率之后会出现），启动时回收到当前屏右上角并记一行日志，避免
  「悬浮窗明明在跑却看不见」。
- `--self-test` 读数修正：自检是独立进程，`reload_config` 才把悬浮窗建出来，
  采样线程还没跑第一拍就读 `status()`，会得到 `sample=0.0ms / collect_rc=None`
  这种看起来像「线程活着但不采样」的假象。现在遇到空读数会另起一次性采样器
  补一条真实读数（带 `note` 行标注来源）。

## v1.7.0 变更（显示位置）

- 显示位置新增四边居中：`top-center` / `bottom-center` / `left-center` /
  `right-center`。面板设置与右键菜单同步多出「顶部 / 底部 / 左侧 / 右侧」
  四项；居中按**窗口所在显示器**的工作区计算，随面板宽高变化自动保持居中。

## 安装 / 卸载

- 安装：面板「＋」选择 `desktop_hud-v1.7.0.zip`，或解压到 `plugins\desktop_hud\`。
- 卸载：面板垃圾桶，或删除目录。卸载不影响其它插件。
- 自检：`WBOX.exe --self-test` 会输出本插件的取数后端、目标显卡、适配器
  清单、采样线程节拍与最近错误。