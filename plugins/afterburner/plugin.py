# -*- coding: utf-8 -*-
"""插件：小飞机关联启动（游戏 ↔ MSI Afterburner / RTSS）。

从 v1 `guard_core.Guard` 搬来的逻辑，逐段对应（行号为 v1.3.1 的 guard_core.py）：
  launch_afterburner() 744 / check_exe_dir() 715 / close_owned() 863
  close_pid() 806 / close_rtss_family() 890 / rtss_names() 555
会话分支（原 `tick()` 里的小飞机部分）1179-1195。

状态归属：`ab_pid` / `ab_by_name` / `rtss_baseline` / `next_launch` / `close_noted`
全部留在本插件内——"小飞机所有权"只对本次启动有意义，内核不该知道。

低层能力（taskkill / 提权启动 / 快照）通过 `ctx.import_core('guard_core')` 复用
内核里冻结的那份实现，本文件不复制任何 Win32 代码。
"""
import os
import subprocess
import time

import core_defaults
from plugin_api import WingmanPlugin

from . import proc_util


class Plugin(WingmanPlugin):
    """游戏启动时拉起小飞机，会话彻底结束后只关"自己拉起的"那个。"""

    # 全部默认值仍以 core_defaults 为唯一来源（插件目录可被替换，默认值不能跟着变）
    DEFAULT_CONFIG = {
        'afterburner_enabled': bool(core_defaults.DEFAULTS['afterburner_enabled']),
        'afterburner_path': core_defaults.DEFAULTS['afterburner_path'],
        'rtss_processes': list(core_defaults.DEFAULTS['rtss_processes']),
        'manage_rtss': bool(core_defaults.DEFAULTS['manage_rtss']),
        'force_close': bool(core_defaults.DEFAULTS['force_close']),
        'close_wait_sec': core_defaults.DEFAULTS['close_wait_sec'],
    }

    def __init__(self, ctx):
        WingmanPlugin.__init__(self, ctx)
        # ---- 本插件私有状态（v1 的 Guard 字段，全部搬走）----
        self.ab_pid = None
        self.ab_by_name = False
        self.rtss_baseline = set()
        self.next_launch = 0.0
        self.close_noted = False
        self.start_seq = -1          # 本局开局的 tick 序号（开局那一轮不判"中途退出"）
        # ---- 配置缓存（on_config 里刷新）----
        self.enabled = True
        self.path = ''
        self.rtss_cfg = []
        self.manage_rtss = True
        self.force_close = True
        self.close_wait = 6.0
        self.acl_checked = set()     # 已做过"目录是否对普通用户可写"检查的目录
        self._core_mod = None

    # ---------------------------------------------------------------- 内部
    def _core(self):
        """懒加载内核低层函数（守护进程里首次用到才 import，面板不会拉起 Win32）。"""
        if self._core_mod is None:
            self._core_mod = self.ctx.import_core('guard_core')
        return self._core_mod

    def _ab_name(self):
        return self._core().AB_NAME

    def _rtss_names(self):
        """RTSS 家族进程名；配置里可改，缺省用内置全套（v1 rtss_names() 555）。"""
        if self.rtss_cfg:
            return set(n.lower() for n in self.rtss_cfg)
        return set(self._core().RTSS_NAMES)

    # ---------------------------------------------------------------- 配置
    def on_config(self, cfg):
        self.enabled = bool(cfg.get('afterburner_enabled', True))
        self.path = cfg.get('afterburner_path') or ''
        self.rtss_cfg = cfg.get('rtss_processes') or []
        self.manage_rtss = bool(cfg.get('manage_rtss', True))
        self.force_close = bool(cfg.get('force_close', True))
        try:
            self.close_wait = float(cfg.get('close_wait_sec', 6))
        except (TypeError, ValueError):
            self.close_wait = 6.0

    def on_load(self):
        # 启动时不主动探测小飞机（v1 也是等到开游戏才找）：on_load 里做只读检查没有意义
        pass

    # ---------------------------------------------------------------- 会话
    def on_session_start(self, tick):
        """idle -> active：判定所有权，必要时拉起小飞机（v1 1179-1191）。"""
        procs = tick.procs or {}
        names = tick.names
        ab_name = self._ab_name()
        # 开局这一轮不做"中途退出"判定：刚拉起的进程可能还没进进程表
        self.start_seq = tick.seq
        rtss = self._rtss_names()
        rtss_pids = set(pid for pid, n in procs.items() if n in rtss)
        # 每局开始重拍 RTSS 基线：只有"本局新出现的" RTSS 组件才归我们关
        self.rtss_baseline = rtss_pids
        ab_running = ab_name in names
        ours = ((self.ab_pid is not None and self.ab_pid in procs
                 and procs[self.ab_pid] == ab_name) or self.ab_by_name)
        if ab_running and ours:
            self.ctx.log('小飞机仍在运行（本脚本上一轮启动），继续接管')
        elif ab_running:
            self.ab_pid = None
            self.ab_by_name = False
            self.ctx.log('小飞机已在运行（非本脚本启动），本次不接管，退出游戏时不会关闭它')
        elif tick.now >= self.next_launch:
            if self.enabled:
                self.launch(tick.now)
            else:
                self.ctx.log('小飞机联动已关闭（设置面板），跳过启动')

    def on_session_resume(self, tick):
        """grace -> active：只保留所有权，不重启、不重设 RTSS 基线（v1 1168-1170）。"""
        self.ctx.log('游戏进程在宽限期内恢复，沿用当前会话与小飞机')

    def on_session_close(self, tick):
        """grace -> idle：关闭"自己拉起的"小飞机（v1 1209-1211 → close_owned 863）。"""
        procs = self.ctx.procs()
        if procs is None:
            # 没读到进程表 ≠ 游戏已退出：本轮不关闭，让内核 2 秒后再发一次 close。
            # 这是 v1 最容易漏掉的隐性语义（guard_core.py:867）。
            self.ctx.retry_close(2)
            if not self.close_noted:
                self.ctx.log('取进程表失败，本轮不关闭小飞机（2 秒后重试）')
                self.close_noted = True
            return
        self.close_noted = False
        self.close_owned(procs)

    def on_tick(self, tick):
        """会话中：本插件拉起的小飞机中途退出 → 放弃所有权，不再自动拉起（v1 1192-1195）。"""
        if tick.session.state != 'active':
            return
        if tick.seq == self.start_seq:
            return
        if self.ab_pid is not None and self._ab_name() not in tick.names:
            self.ctx.log('本脚本启动的小飞机中途退出，本次不再自动拉起')
            self.ab_pid = None
            self.ab_by_name = False

    # ---------------------------------------------------------------- 关闭
    def close_owned(self, procs):
        """关闭本插件启动的小飞机与其带来的 RTSS（v1 863-888）。

        procs 是本轮的进程表；None 表示"没读到"，此时必须重试而不是放弃。
        """
        if procs is None:
            self.ctx.retry_close(2)
            if not self.close_noted:
                self.ctx.log('取进程表失败，本轮不关闭小飞机（2 秒后重试）')
                self.close_noted = True
            return
        ab_name = self._ab_name()
        if self.ctx.games_present(procs):
            self.ctx.log('关闭前复核：游戏进程已回来，放弃本轮关闭')
            return
        if self.ab_pid is not None:
            if self.ab_pid in procs and procs[self.ab_pid] == ab_name:
                self.close_pid(self.ab_pid, ab_name)
        elif self.ab_by_name:
            for pid, name in list(procs.items()):
                if name == ab_name:
                    self.close_pid(pid, ab_name)
        if self.manage_rtss and (self.ab_pid is not None or self.ab_by_name):
            self.close_rtss_family()
        self.ab_pid = None
        self.ab_by_name = False
        self.rtss_baseline = set()

    def _close_batch(self, targets):
        """批量关闭的唯一入口（也是回归测试的观察点）。"""
        return proc_util.close_processes(self.ctx, self._core(), targets,
                                         self.close_wait, self.force_close)

    def close_pid(self, pid, name):
        """WM_CLOSE → 等待 → 可配置强杀（v1 806-857 语义）。

        v2.1.0 起实现搬到 plugin-shared/proc_util.py，与 gamepp 共用同一份：
        等待改用进程句柄（WaitForMultipleObjects）而非 sleep 0.4 + 全表快照，
        退出即刻返回。「等待中复核游戏回归即中止」「pid 复用即停手」
        「快照失败即停手交 retry_close」三条语义逐条保留，见 proc_util 文档。
        """
        return proc_util.close_one(self.ctx, self._core(), pid, name,
                                   self.close_wait, self.force_close)

    def close_rtss_family(self):
        """先关 RTSS.exe 主进程再关子进程，多轮复核（v1 890-922 原文搬迁）。"""
        ctx = self.ctx
        rtss_names = self._rtss_names()
        total = 0
        for round_i in range(3):
            procs = ctx.snapshot()
            if procs is None:
                ctx.log('取进程表失败，RTSS 清理提前结束')
                return
            new_pids = [pid for pid, name in procs.items()
                        if name in rtss_names and pid not in self.rtss_baseline]
            if not new_pids:
                break
            # rtss.exe 排最前：它是父进程，先关掉才不会重新拉起 EncoderServer / HooksLoader
            new_pids.sort(key=lambda pid: (procs[pid] != 'rtss.exe', pid))
            targets = [(pid, procs[pid]) for pid in new_pids if procs.get(pid)]
            # v2.1.0：整批先发 WM_CLOSE 再共享一个 close_wait 期限等待。
            # 发送顺序不变（父进程仍最先收到），但不再逐个各等 6 秒。
            results = self._close_batch(targets)
            closed = [name for _pid, name in targets]
            total += len(closed)
            ctx.log('RTSS 清理第 %d 轮：%d 个（%s）' %
                    (round_i + 1, len(closed), ', '.join(closed)))
            if results and all(v == 'aborted' for v in results.values()):
                ctx.log('RTSS 清理：游戏已回归，停止后续轮次')
                return
            time.sleep(1)
        final = ctx.snapshot()
        if final is None:
            ctx.log('取进程表失败，无法复核 RTSS 残留（已处理 %d 个）' % total)
            return
        left = sorted(n for pid, n in final.items()
                      if n in rtss_names and pid not in self.rtss_baseline)
        if left:
            ctx.log('RTSS 仍有残留: %s' % ', '.join(left))
        elif total:
            ctx.log('RTSS 已清空，共处理 %d 个进程' % total)

    # ---------------------------------------------------------------- 启动
    def launch(self, now=0.0):
        """拉起小飞机（v1 744-807 原文搬迁）。失败则 60 秒内不再重试。"""
        ctx = self.ctx
        core = self._core()
        path = self.path or ''
        if not path or not os.path.isfile(path):
            # 配置里没写（或写的是别的机器上的路径）时兜底找一次
            for var in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
                base = os.environ.get(var)
                cand = os.path.join(base, 'MSI Afterburner', 'MSIAfterburner.exe') \
                    if base else ''
                if cand and os.path.isfile(cand):
                    path = cand
                    break
            else:
                for pid, name in (core.snapshot() or {}).items():
                    if name == core.AB_NAME:
                        cand = core.process_path(pid)
                        if cand and os.path.isfile(cand):
                            path = cand
                            break
            if path and os.path.isfile(path):
                ctx.log('配置未指定小飞机路径，自动探测到: %s' % path)
        if not path or not os.path.isfile(path):
            ctx.log('找不到小飞机主程序：config.json 的 afterburner_path 为空或路径不存在，'
                    '也已查过常见目录与运行中的进程；请在「编辑配置文件」里填写')
            self.next_launch = time.time() + 60
            return
        ctx.log('启动小飞机: %s' % path)
        self.check_exe_dir(path)
        try:
            p = subprocess.Popen([path], cwd=os.path.dirname(path),
                                 creationflags=core.CREATE_NO_WINDOW, close_fds=True)
            self.ab_pid = p.pid
            self.ab_by_name = False
            ctx.log('小飞机已启动，pid=%d' % p.pid)
        except OSError as e:
            if getattr(e, 'winerror', None) == 740:
                rc = core.shell_execute_runas(path)
                if rc and rc > 32:
                    self.ab_pid = None
                    self.ab_by_name = True
                    ctx.log('已通过 UAC 提权启动（拿不到 pid，退出时按进程名关闭）')
                else:
                    ctx.log('提权启动失败，ShellExecute 返回 %s' % rc)
                    self.next_launch = time.time() + 60
            else:
                ctx.log('启动失败: %r' % (e,))
                self.next_launch = time.time() + 60

    def check_exe_dir(self, path):
        """启动前检查小飞机所在目录是否允许普通用户写入（v1 715-742 语义）。

        守护是管理员进程，`subprocess.Popen` 会让目标继承管理员令牌。若目录
        对 Authenticated Users / Everyone / Users 开放写权限，任何本地程序都能把
        MSIAfterburner.exe 换成自己的，等下次开游戏时以管理员身份被执行。

        只告警不阻断：个人机器上常见把软件装在整盘继承权限的分区，直接拒绝
        会让联动彻底失效。日志里给出收紧命令，由用户决定。

        v2.1.0：判据与实现搬到 proc_util（与 gamepp 共用）。原来是
        `powershell Get-Acl` + `'LOOSE' in out`，命令失败/超时/模块加载失败
        时输出里没有 LOOSE，**失败被静默当成权限已收紧**；且每次冷启动
        PowerShell 约 754ms，卡在开局钩子里。现在 ctypes 直读 DACL，
        三态 LOOSE/TIGHT/UNKNOWN，实测 0.0375ms/次，UNKNOWN 明确记「未完成」。
        """
        proc_util.check_exe_dir(self.ctx, path, self.acl_checked)

    # ---------------------------------------------------------------- 自检
    def on_self_test(self, out):
        """--self-test 里的小飞机/RTSS 段落（v1 self_test() 1230-1245）。"""
        core = self._core()
        procs = core.snapshot()
        if procs is None:
            out.append('AB running    : snapshot 失败（取不到进程表）')
            return
        names = set(procs.values())
        out.append('AB running    : %s' % (core.AB_NAME in names))
        out.append('RTSS running  : %s' % sorted(names & self._rtss_names()))
        out.append('RTSS watch    : %d' % len(self._rtss_names()))
