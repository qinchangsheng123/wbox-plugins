# -*- coding: utf-8 -*-
"""插件：游戏加加联动（游戏 ↔ GamePP）。

与 afterburner 插件同一套「所有权」语义，但不带 RTSS 家族清理：
  - 开局时游戏加加已经在跑（用户自己开的常驻托盘）-> 不接管、退出时不关；
  - 本插件拉起的进程中途退出 -> 放弃所有权，本局不再自动拉起；
  - 会话彻底结束 -> 只关「自己拉起的」+「本局新出现的」GamePP 家族进程，
    基线里原本就在跑的进程绝不碰。

路径解析只用通用规则（配置 / 常见安装目录 / 注册表卸载项 / 运行中进程），
源码里不写任何本机绝对路径。启动失败 60 秒内不重试，避免每局刷日志。
"""
import os
import re
import subprocess
import time

from plugin_api import WingmanPlugin

from . import proc_util

MAIN_NAME = 'gamepp.exe'
DEFAULT_NAMES = ['gamepp.exe', 'gamepplite.exe', 'gamepplauncher.exe']


class Plugin(WingmanPlugin):
    """游戏启动时拉起游戏加加，会话结束后只关本次新起的那一批。"""

    DEFAULT_CONFIG = {
        'gamepp': {
            'enabled': True,
            # 空 = 自动探测；也可在 config.json 里写自己的安装路径
            'path': '',
            'processes': list(DEFAULT_NAMES),
            'manage_children': True,
            'force_close': True,
            'close_wait_sec': 6.0,
        }
    }

    def __init__(self, ctx):
        WingmanPlugin.__init__(self, ctx)
        self.enabled = True
        self.path = ''
        self.names = set(DEFAULT_NAMES)
        self.manage_children = True
        self.force_close = True
        self.close_wait = 6.0
        self.owned_pid = None
        self.owned_by_name = False       # UAC 提权启动拿不到 pid 时按进程名关
        self.pending_close = False       # 本局是否真的接管过（没拉起就不用重试关闭）
        self.baseline = set()            # 本局开始前就在跑的 GamePP 家族 pid
        self.next_launch = 0.0
        self.start_seq = -1
        self.close_noted = False
        self.acl_checked = set()
        self._core_mod = None
        self._detected = ''
        self._next_probe = 0.0           # 注册表全量枚举的负缓存到期时刻

    # ---------------------------------------------------------------- 内部
    def _core(self):
        if self._core_mod is None:
            self._core_mod = self.ctx.import_core('guard_core')
        return self._core_mod

    def _family_pids(self, procs):
        return set(pid for pid, name in (procs or {}).items() if name in self.names)

    # ---------------------------------------------------------------- 配置
    def on_config(self, cfg):
        seg = cfg.get('gamepp') if isinstance(cfg.get('gamepp'), dict) else {}
        self.enabled = bool(seg.get('enabled', True))
        self.path = seg.get('path') or ''
        raw = seg.get('processes')
        names = [str(n).lower() for n in raw if isinstance(n, str)] if isinstance(raw, list) else []
        self.names = set(names or DEFAULT_NAMES)
        self.names.add(MAIN_NAME)
        self.manage_children = bool(seg.get('manage_children', True))
        self.force_close = bool(seg.get('force_close', True))
        try:
            self.close_wait = max(0.5, float(seg.get('close_wait_sec', 6)))
        except (TypeError, ValueError):
            self.close_wait = 6.0
        self._detected = ''              # 配置改了就重新探测路径
        self._next_probe = 0.0           # 负缓存一并作废

    # ---------------------------------------------------------------- 会话
    def on_session_start(self, tick):
        procs = tick.procs or {}
        self.start_seq = tick.seq
        self.baseline = self._family_pids(procs)
        running = MAIN_NAME in tick.names
        ours = ((self.owned_pid is not None and self.owned_pid in procs
                 and procs[self.owned_pid] == MAIN_NAME) or self.owned_by_name)
        if running and ours:
            self.pending_close = True
            self.ctx.log('游戏加加仍在运行（本插件上一轮拉起），继续接管')
        elif running:
            self.owned_pid = None
            self.owned_by_name = False
            self.pending_close = False
            self.ctx.log('游戏加加已在运行（非本插件启动），本次不接管，退出时不关闭')
        elif tick.now >= self.next_launch:
            if self.enabled:
                self.pending_close = True
                self.launch()
            else:
                self.pending_close = False
                self.ctx.log('游戏加加联动已关闭（设置面板），跳过启动')

    def on_session_resume(self, tick):
        self.ctx.log('游戏进程在宽限期内恢复，沿用当前会话与游戏加加')

    def on_session_close(self, tick):
        # 本局没拉起也没接管（用户自己常驻 / 插件被关 / 开局就没探测到）：
        # 没有任何东西要关，取不到进程表时不该跟着申请重试。
        if not self.pending_close:
            return
        procs = self.ctx.procs()
        if procs is None:
            self.ctx.retry_close(2)
            if not self.close_noted:
                self.ctx.log('取进程表失败，本轮不关闭游戏加加（2 秒后重试）')
                self.close_noted = True
            return
        self.close_noted = False
        self.close_owned(procs)
        self.pending_close = False

    def on_tick(self, tick):
        if tick.session.state != 'active' or tick.seq == self.start_seq:
            return
        if self.owned_pid is not None and MAIN_NAME not in tick.names:
            self.ctx.log('本插件拉起的游戏加加中途退出，本次不再自动拉起')
            self.owned_pid = None
            self.owned_by_name = False

    # ---------------------------------------------------------------- 关闭
    def close_owned(self, procs):
        if self.ctx.games_present(procs):
            self.ctx.log('关闭前复核：游戏进程已回来，放弃本轮关闭游戏加加')
            return
        targets = []
        if self.owned_pid is not None and procs.get(self.owned_pid) == MAIN_NAME:
            targets.append((self.owned_pid, MAIN_NAME))
        elif self.owned_by_name:
            targets.extend((pid, name) for pid, name in procs.items()
                           if name == MAIN_NAME and pid not in self.baseline)
        if self.manage_children:
            # 主进程排最前：游戏加加的监控/加速组件由它拉起，先关父进程
            extra = [(pid, name) for pid, name in procs.items()
                     if name in self.names and pid not in self.baseline
                     and pid not in [t[0] for t in targets]]
            extra.sort(key=lambda item: (item[1] != MAIN_NAME, item[0]))
            targets.extend(extra)
        # v1.1.0：整批先发关闭请求再共享期限等待（主进程仍排最前先收到），
        # 不再逐个各等 close_wait。游戏回归会中止整批。
        self._close_batch(targets)
        self.owned_pid = None
        self.owned_by_name = False
        self.baseline = set()

    def _close_batch(self, targets):
        """批量关闭的唯一入口（也是回归测试的观察点：断言选中了哪些 pid）。"""
        return proc_util.close_processes(self.ctx, self._core(), targets,
                                         self.close_wait, self.force_close)

    def close_pid(self, pid, name):
        """taskkill 请求退出 -> 等待 -> 可配置强杀；等待中游戏回归即中止。

        v1.1.0 起与 afterburner 共用 plugin-shared/proc_util.py 的实现：
        句柄等待替代 sleep 0.4 + 全表快照，一批目标共享一个 close_wait 期限。
        """
        return proc_util.close_one(self.ctx, self._core(), pid, name,
                                   self.close_wait, self.force_close)

    # ---------------------------------------------------------------- 启动
    def _pick_exe(self, raw):
        """从注册表值里抠出 GamePP.exe：DisplayIcon 常带 ,0 或安装目录。"""
        s = str(raw).strip().strip('"').replace('/', '\\')
        m = re.match(r'(?i)^([A-Za-z]:\\[^,]*?\.exe)', s)
        if m and os.path.isfile(m.group(1)):
            return m.group(1)
        if os.path.isdir(s):
            cand = os.path.join(s, 'GamePP.exe')
            if os.path.isfile(cand):
                return cand
        return ''

    def _from_registry(self):
        """读卸载项里的 DisplayIcon / InstallLocation，找不到返回空串。"""
        try:
            import winreg
        except ImportError:
            return ''
        subs = (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in subs:
                try:
                    key = winreg.OpenKey(hive, sub)
                except OSError:
                    continue
                try:
                    i = 0
                    while i < 512:
                        try:
                            child = winreg.EnumKey(key, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            item = winreg.OpenKey(key, child)
                        except OSError:
                            continue
                        try:
                            try:
                                disp = str(winreg.QueryValueEx(item, 'DisplayName')[0])
                            except OSError:
                                continue
                            if '游戏加加' not in disp and 'gamepp' not in disp.lower():
                                continue
                            for val in ('DisplayIcon', 'InstallLocation'):
                                try:
                                    text = winreg.QueryValueEx(item, val)[0]
                                except OSError:
                                    continue
                                hit = self._pick_exe(text)
                                if hit:
                                    return hit
                        finally:
                            winreg.CloseKey(item)
                finally:
                    winreg.CloseKey(key)
        return ''

    def resolve_path(self):
        """配置 -> 缓存的探测结果 -> 常见目录 -> 注册表 -> 运行中进程。"""
        if self.path and os.path.isfile(self.path):
            return self.path
        if self._detected and os.path.isfile(self._detected):
            return self._detected
        for var in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
            base = os.environ.get(var)
            cand = os.path.join(base, 'GamePP', 'GamePP.exe') if base else ''
            if cand and os.path.isfile(cand):
                self._detected = cand
                self.ctx.log('自动探测到游戏加加：%s' % cand)
                return cand
        # v1.1.0：注册表枚举加负缓存。没装游戏加加的机器上，原来每局都要
        # HKLM+HKCU × 2 个 Uninstall 子键 × 最多 512 项开键查 DisplayName，
        # 纯浪费。失败后 60 秒内不再全量枚举；on_config 会立刻作废负缓存。
        cand = ''
        if time.time() >= self._next_probe:
            cand = self._from_registry()
            if cand:
                self._next_probe = 0.0
            else:
                self._next_probe = time.time() + 60.0
        if cand:
            self._detected = cand
            self.ctx.log('从卸载信息探测到游戏加加：%s' % cand)
            return cand
        core = self._core()
        for pid, name in (core.snapshot() or {}).items():
            if name == MAIN_NAME:
                cand = core.process_path(pid)
                if cand and os.path.isfile(cand):
                    self._detected = cand
                    self.ctx.log('从运行中进程探测到游戏加加：%s' % cand)
                    return cand
        return ''

    def check_exe_dir(self, path):
        """守护是管理员进程，Popen 会继承管理员令牌；目录对普通用户可写就告警。

        v1.1.0：与 afterburner 共用 proc_util 的三态判定（ctypes 直读 DACL）。
        原 PowerShell 实现把「命令失败」当成「权限收紧」，且每次约 754ms。
        """
        proc_util.check_exe_dir(self.ctx, path, self.acl_checked)

    def launch(self):
        path = self.resolve_path()
        if not path:
            self.ctx.log('找不到游戏加加主程序：可在 config.json 的 gamepp.path 填写 GamePP.exe 的完整路径；'
                         '也可先手动运行一次游戏加加，本插件会从进程表探测')
            self.next_launch = time.time() + 60
            return
        self.check_exe_dir(path)
        core = self._core()
        self.ctx.log('启动游戏加加: %s' % path)
        try:
            p = subprocess.Popen([path], cwd=os.path.dirname(path),
                                 creationflags=core.CREATE_NO_WINDOW, close_fds=True)
            self.owned_pid = p.pid
            self.owned_by_name = False
            self.ctx.log('游戏加加已启动，pid=%d' % p.pid)
        except OSError as e:
            if getattr(e, 'winerror', None) == 740:
                rc = core.shell_execute_runas(path)
                if rc and rc > 32:
                    self.owned_pid = None
                    self.owned_by_name = True
                    self.ctx.log('已通过 UAC 提权启动（拿不到 pid，退出时按进程名关闭）')
                else:
                    self.ctx.log('提权启动失败，ShellExecute 返回 %s' % rc)
                    self.next_launch = time.time() + 60
            else:
                self.ctx.log('启动游戏加加失败: %r' % (e,))
                self.next_launch = time.time() + 60

    # ---------------------------------------------------------------- 自检
    def on_self_test(self, out):
        core = self._core()
        procs = core.snapshot()
        if procs is None:
            out.append('GamePP running : snapshot 失败')
            return
        running = sorted(set(n for n in procs.values() if n in self.names))
        out.append('GamePP running : %s' % (running or 'none'))
        out.append('GamePP path    : %s' % (self.resolve_path() or '未探测到'))
        out.append('GamePP watch   : %d' % len(self.names))
