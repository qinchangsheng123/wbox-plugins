# -*- coding: utf-8 -*-
"""WBOX 插件：任务静默后自动关机（idle_shutdown）v1.0.0。

移植自 D:\\01-Tools\\shutdown-after-midnight\\shutdown_after_midnight.py v1.3.0，
判据保持一致：会话 jsonl 静默 + WorkBuddy/Qoder 进程组 CPU 低 + 键鼠空闲，
且当前处于时间窗内、本次运行确实见到过任务在忙，才允许关机。

与原脚本的差异（插件化必要改动）：
  1. 常驻在 WBOX 守护进程里，按 tick 轮询，不在前台开 tkinter 窗口；
     倒计时交给 Windows 的关机倒计时（InitiateSystemShutdownExW / shutdown.exe）。
  2. 所有阻塞操作（1.5s CPU 采样、glob、SSH 关 NAS、拉起 python）都走 ctx.spawn
     后台线程；on_tick 只做计数和状态判断。
  3. 取消口：插件数据目录与旧脚本目录的 stop.flag / .cancelled 都认；
     取消时 AbortSystemShutdownW + shutdown /a，并用代次号阻止已排队的关机线程。
  4. 口令不进插件：飞牛关机仍调用旧脚本目录的 nas_poweroff.py，口令由它在运行时
     从 secrets.local.md 读取；插件包里没有任何凭证。

安全边界（不可改）：
  - 默认只在 00:00-08:00 动作；窗口外只待命。
  - 默认 require_activity：没观察到任务在忙就绝不关机。
  - 单轮忙不重置计时，连续两轮忙才算真忙。
  - 探测失败一律按「忙」处理，宁可不关。
  - 模块顶层无副作用（面板进程也会 import）；不 import uuid/platform/wmi。
"""
import ctypes
import json
import os
import subprocess
import threading
import time
from ctypes import wintypes

from plugin_api import WingmanPlugin

PLUGIN_VERSION = '1.0.0'

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260

CPU_SAMPLE_SEC = 1.5
DEFAULT_LEGACY_DIR = 'D:/01-Tools/shutdown-after-midnight'

# 时间窗配置。start == end 表示全天。
WINDOWS = {
    'night': ('00:00', '08:00'),
    'evening': ('20:00', '23:59'),
    'always': ('00:00', '00:00'),
}

HOME = os.path.expanduser('~')
WB_PROJ_DIR = os.path.join(HOME, '.workbuddy', 'projects')
QODER_PROJ_DIR = os.path.join(HOME, '.qoder', 'projects')


# ---------------------------------------------------------------- ctypes 结构
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_wchar * MAX_PATH),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]


class LUID(ctypes.Structure):
    _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Luid', LUID), ('Attributes', wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [('PrivilegeCount', wintypes.DWORD),
                ('Privileges', LUID_AND_ATTRIBUTES * 1)]


_dll_lock = threading.Lock()
_dll_cache = {}


def _dlls():
    """惰性加载 Win32 DLL。面板进程只 import 本模块时不会碰它。"""
    with _dll_lock:
        if not _dll_cache:
            k = ctypes.WinDLL('kernel32', use_last_error=True)
            a = ctypes.WinDLL('advapi32', use_last_error=True)
            u = ctypes.WinDLL('user32', use_last_error=True)
            k.GetProcessTimes.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME)]
            k.GetProcessTimes.restype = wintypes.BOOL
            k.GetTickCount64.restype = ctypes.c_uint64
            u.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
            u.GetLastInputInfo.restype = wintypes.BOOL
            a.InitiateSystemShutdownExW.argtypes = [
                wintypes.LPWSTR, wintypes.LPWSTR, wintypes.UINT,
                wintypes.BOOL, wintypes.BOOL, wintypes.DWORD]
            a.InitiateSystemShutdownExW.restype = wintypes.BOOL
            a.AbortSystemShutdownW.argtypes = [wintypes.LPWSTR]
            a.AbortSystemShutdownW.restype = wintypes.BOOL
            a.LookupPrivilegeValueW.argtypes = [
                wintypes.LPWSTR, wintypes.LPWSTR, ctypes.POINTER(LUID)]
            a.AdjustTokenPrivileges.argtypes = [
                wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
                wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
            _dll_cache.update({'k': k, 'a': a, 'u': u})
        return _dll_cache


# ---------------------------------------------------------------- 探测（后台线程）
def _now_local():
    """测试可替换的时间源。冻结运行时没有 datetime，只能靠 time.localtime。"""
    return time.localtime()


def _parse_hhmm(text):
    try:
        hh, mm = str(text).split(':')
        value = int(hh) * 60 + int(mm)
        if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59:
            return value
    except Exception:
        pass
    return None


def _in_window(now_min, start_min, end_min):
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def _window_bounds(cfg):
    """-> (start_min, end_min, 展示文本)。坏值按夜间窗，不按全天。"""
    key = str(cfg.get('window', 'night') or 'night')
    a, b = WINDOWS.get(key, WINDOWS['night'])
    return _parse_hhmm(a), _parse_hhmm(b), '%s-%s' % (a, b)


def _enum_processes():
    k = _dlls()['k']
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return {}
    out = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = (entry.szExeFile or '').lower()
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return out


def _ft(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _cpu_times(names, procs):
    k = _dlls()['k']
    out = {}
    for pid, pname in procs.items():
        if pname not in names:
            continue
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            continue
        try:
            c, e, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                 ctypes.byref(kernel), ctypes.byref(user)):
                out[int(pid)] = _ft(kernel) + _ft(user)
        finally:
            k.CloseHandle(h)
    return out


def _group_cpu_percent(names, sample_sec=CPU_SAMPLE_SEC):
    a = _cpu_times(names, _enum_processes())
    if not a:
        return None
    t0 = time.time()
    time.sleep(sample_sec)
    b = _cpu_times(names, _enum_processes())
    dt = max(0.001, time.time() - t0)
    delta = 0
    for pid, value in b.items():
        if pid in a:
            delta += max(0, value - a[pid])
    return 100.0 * (delta / 1e7) / dt / (os.cpu_count() or 1)


def _idle_seconds():
    k, u = _dlls()['k'], _dlls()['u']
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not u.GetLastInputInfo(ctypes.byref(lii)):
        return None
    tick = int(k.GetTickCount64())
    return ((tick - int(lii.dwTime)) & 0xFFFFFFFF) / 1000.0


def _iter_jsonl(base_dir, max_depth=4):
    """只用 os.scandir 遍历会话文件：冻结运行时没有 glob 模块。"""
    try:
        with os.scandir(base_dir) as top:
            for entry in top:
                if entry.is_file() and entry.name.endswith('.jsonl'):
                    yield entry.path
                    continue
                if not entry.is_dir(follow_symlinks=False) or max_depth < 2:
                    continue
                try:
                    with os.scandir(entry.path) as second:
                        for item in second:
                            if item.is_file() and item.name.endswith('.jsonl'):
                                yield item.path
                            elif item.is_dir(follow_symlinks=False) and max_depth >= 3:
                                try:
                                    with os.scandir(item.path) as third:
                                        for leaf in third:
                                            if leaf.is_file() and leaf.name.endswith('.jsonl'):
                                                yield leaf.path
                                except OSError:
                                    continue
                except OSError:
                    continue
    except OSError:
        return


def _activity_age(base_dir, max_depth=4):
    """会话 jsonl 最近一次写入距今秒数；日志目录是心跳，刻意不看。"""
    if not base_dir or not os.path.isdir(base_dir):
        return None
    newest, found = 0.0, False
    for path in _iter_jsonl(base_dir, max_depth):
        try:
            newest = max(newest, os.path.getmtime(path))
            found = True
        except Exception:
            continue
    return max(0.0, time.time() - newest) if found else None


def _sample_system(cfg):
    """一次完整判据采样。只由后台线程调用，可阻塞约 1.5 秒。"""
    watch_wb = bool(cfg.get('watch_workbuddy', True))
    watch_qoder = bool(cfg.get('watch_qoder', True))
    threshold = max(60.0, float(cfg.get('idle_min', 10) or 10) * 60.0)
    cpu_max = float(cfg.get('cpu_max', 8.0) or 8.0)

    wb_age = _activity_age(WB_PROJ_DIR) if watch_wb else None
    wb_cpu = _group_cpu_percent(('workbuddy.exe',)) if watch_wb else None
    if watch_qoder:
        q_age = _activity_age(QODER_PROJ_DIR)
        q_cpu = _group_cpu_percent(('qoder.exe',))
    else:
        q_age, q_cpu = None, None
    idle = _idle_seconds() if bool(cfg.get('require_idle_input', True)) else None

    age_ok = ((wb_age is None) or (wb_age >= threshold)) and \
             ((q_age is None) or (q_age >= threshold))
    cpu_ok = ((wb_cpu is None) or (wb_cpu < cpu_max)) and \
             ((q_cpu is None) or (q_cpu < cpu_max))
    idle_ok = (idle is None) or (idle >= threshold)

    def fmt_age(value):
        return '%.0fs前' % value if value is not None else '无记录'

    def fmt_cpu(value):
        return '%.1f%%' % value if value is not None else '未运行'

    detail = 'WB 活动 %s/CPU %s' % (fmt_age(wb_age), fmt_cpu(wb_cpu))
    if watch_qoder:
        detail += '｜Qoder 活动 %s/CPU %s' % (fmt_age(q_age), fmt_cpu(q_cpu))
    detail += '｜键鼠空闲 %s' % ('%.0fs' % idle if idle is not None else '未检查')
    return {'busy': not (age_ok and cpu_ok and idle_ok),
            'detail': detail,
            'wb_age': wb_age, 'wb_cpu': wb_cpu,
            'q_age': q_age, 'q_cpu': q_cpu, 'idle': idle}


# ---------------------------------------------------------------- 关机（后台线程）
def _enable_shutdown_privilege():
    k, a = _dlls()['k'], _dlls()['a']
    token = wintypes.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(), 0x0020 | 0x0008,
                              ctypes.byref(token)):
        return False
    try:
        luid = LUID()
        if not a.LookupPrivilegeValueW(None, 'SeShutdownPrivilege',
                                       ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 0x00000002
        if not a.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None):
            return False
        return ctypes.get_last_error() == 0
    finally:
        k.CloseHandle(token)


def _request_shutdown(grace_sec, reason):
    """先走系统 API，失败回退 shutdown.exe；两个子进程都不许弹黑窗。"""
    a = _dlls()['a']
    grace = max(0, int(grace_sec))
    try:
        _enable_shutdown_privilege()
        ok = a.InitiateSystemShutdownExW(None, reason, grace, True, False, 0x80040001)
        if ok:
            return True, 'InitiateSystemShutdownExW'
        code = ctypes.get_last_error()
    except Exception as exc:
        code = -1
        _note = exc  # 仅用于下方日志文案，不把异常抛给宿主
    try:
        subprocess.Popen(['shutdown', '/s', '/t', str(grace), '/c', reason],
                         shell=False, close_fds=True,
                         creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS)
        return True, 'shutdown.exe（API 错误码 %s）' % code
    except Exception as exc:
        return False, 'shutdown.exe 失败：%s' % exc


def _abort_shutdown():
    a = _dlls()['a']
    ok = False
    try:
        _enable_shutdown_privilege()
        ok = bool(a.AbortSystemShutdownW(None))
    except Exception:
        pass
    if not ok:
        try:
            r = subprocess.run(['shutdown', '/a'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=8,
                               creationflags=CREATE_NO_WINDOW)
            ok = (r.returncode == 0)
        except Exception:
            pass
    return ok


def _paramiko_python():
    candidates = []
    roots = [os.path.join(HOME, '.workbuddy', 'binaries', 'python', 'versions'),
             'C:/Python313', 'C:/Python312', 'C:/Python311', 'C:/Python38']
    for root in roots:
        if not os.path.isdir(root):
            continue
        if 'versions' in root.lower():
            try:
                subs = sorted(os.listdir(root), reverse=True)
            except Exception:
                subs = []
            for sub in subs:
                exe = os.path.join(root, sub, 'python.exe')
                if os.path.isfile(exe):
                    candidates.append(exe)
        else:
            exe = os.path.join(root, 'python.exe')
            if os.path.isfile(exe):
                candidates.append(exe)
    for exe in candidates:
        try:
            rc = subprocess.run([exe, '-c', 'import paramiko'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=25, creationflags=CREATE_NO_WINDOW).returncode
            if rc == 0:
                return exe
        except Exception:
            continue
    return None


def _shutdown_nas(cfg):
    helper = os.path.normpath(str(cfg.get('nas_helper') or
                                  os.path.join(DEFAULT_LEGACY_DIR, 'nas_poweroff.py')))
    if not os.path.isfile(helper):
        return False, '找不到 %s' % helper
    py = _paramiko_python()
    if not py:
        return False, '没有可用的 paramiko 解释器'
    try:
        p = subprocess.run([py, helper], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=240,
                           creationflags=CREATE_NO_WINDOW)
        text = (p.stdout or b'').decode('utf-8', 'replace').strip()
        for line in text.splitlines():
            if line.strip() and 'Could not chdir' not in line:
                print('NAS| ' + line)
        return (True, 'NAS 已执行关机') if p.returncode == 0 else \
               (False, 'NAS 关机返回码 %d' % p.returncode)
    except Exception as exc:
        return False, 'NAS 关机异常: %s' % exc


# ---------------------------------------------------------------- 插件
class Plugin(WingmanPlugin):
    API = 1
    DEFAULT_CONFIG = {
        'idle_shutdown_enabled': True,
        'idle_shutdown': {
            'window': 'night',
            'idle_min': 10,
            'poll_sec': 30,
            'grace_sec': 60,
            'cpu_max': 8.0,
            'require_activity': True,
            'require_idle_input': True,
            'watch_workbuddy': True,
            'watch_qoder': True,
            'nas_shutdown': False,
            'dry_run': False,
            'legacy_stop_dir': DEFAULT_LEGACY_DIR,
            'nas_helper': DEFAULT_LEGACY_DIR + '/nas_poweroff.py',
            'shutdown_reason': 'WorkBuddy/Qoder 任务已完成，按计划关机',
        },
    }

    def __init__(self, ctx):
        WingmanPlugin.__init__(self, ctx)
        self._lock = threading.RLock()
        self._cfg = {'idle_shutdown_enabled': True,
                     'idle_shutdown': dict(self.DEFAULT_CONFIG['idle_shutdown'])}
        self._generation = 0
        self._phase = 'idle'          # idle / preparing / requested
        self._pending = None
        self._sample_seq = 0
        self._handled_seq = 0
        self._result = None
        self._checking = False
        self._last_check = 0.0
        self._quiet_since = None
        self._busy_streak = 0
        self._seen_target = False
        self._window_key = None
        self._last_report = 0.0
        self._summary = ''

    # ------------------------------------------------------------ 配置
    def _cfg_section(self):
        value = self._cfg.get('idle_shutdown')
        return value if isinstance(value, dict) else {}

    def _c(self, key, default=None):
        return self._cfg_section().get(key, default)

    def _enabled(self):
        return bool(self._cfg.get('idle_shutdown_enabled', True))

    def on_config(self, cfg):
        with self._lock:
            self._cfg = cfg if isinstance(cfg, dict) else {}
            # 配置变了：丢弃在途采样，防止拿旧参数触发关机。
            self._sample_seq += 1
            self._checking = False
            self._result = None
        if not self._enabled() and self._pending is not None:
            self._cancel('插件已禁用')
        self._log_summary()

    def _log_summary(self):
        _, _, text = _window_bounds(self._cfg_section())
        stamp = '%s|%s|%s|%s|%s|%s' % (
            self._enabled(), text, self._c('idle_min', 10),
            self._c('require_activity', True), self._c('nas_shutdown', False),
            self._c('dry_run', False))
        if stamp == self._summary:
            return
        self._summary = stamp
        self.ctx.log('任务后自动关机 v%s：%s；窗口 %s；静默 %s 分钟；见忙碌才关=%s；飞牛=%s%s'
                     % (PLUGIN_VERSION, '开启' if self._enabled() else '关闭', text,
                        self._c('idle_min', 10),
                        '是' if self._c('require_activity', True) else '否',
                        '开' if self._c('nas_shutdown', False) else '关',
                        '；DRY-RUN' if self._c('dry_run', False) else ''))

    # ------------------------------------------------------------ 取消
    def _stop_paths(self):
        paths = [os.path.join(self.ctx.data_dir, 'stop.flag'),
                 os.path.join(self.ctx.data_dir, '.cancelled')]
        legacy = str(self._c('legacy_stop_dir', DEFAULT_LEGACY_DIR) or '').strip()
        if legacy:
            paths.append(os.path.join(os.path.normpath(legacy), 'stop.flag'))
            paths.append(os.path.join(os.path.normpath(legacy), '.cancelled'))
        return paths

    def _stop_marks(self):
        return [p for p in self._stop_paths() if os.path.exists(p)]

    def _is_current(self, gen):
        with self._lock:
            return self._generation == gen

    def _cancel(self, reason):
        with self._lock:
            if self._phase == 'idle' and self._pending is None:
                return False
            self._generation += 1
            self._sample_seq += 1
            self._pending = None
            self._phase = 'idle'
            self._quiet_since = None
            self._busy_streak = 0
            self._seen_target = False
            self._checking = False
        self.ctx.spawn(_abort_shutdown)
        self.ctx.log('已取消关机（%s），撤销系统关机计划' % reason)
        self.ctx.set_status('任务后关机：已取消（%s）' % reason)
        return True

    # ------------------------------------------------------------ 采样
    def _worker_sample(self, seq, cfg):
        try:
            result = _sample_system(cfg)
        except Exception as exc:
            # 探测失败按「忙」处理：宁可不关，不能误关。
            result = {'busy': True, 'detail': '探测失败：%r' % (exc,)}
        result['seq'] = seq
        result['at'] = time.time()
        with self._lock:
            if seq != self._sample_seq:
                return
            self._result = result
            self._checking = False

    def _reset_gate(self):
        self._quiet_since = None
        self._busy_streak = 0
        self._seen_target = False

    def _begin_shutdown(self, now, span, detail):
        with self._lock:
            if self._pending is not None or self._phase != 'idle':
                return
            self._generation += 1
            gen = self._generation
            self._pending = {'gen': gen, 'at': now}
            self._phase = 'preparing'
        cfg = dict(self._cfg_section())
        self.ctx.log('空闲已持续 %.1f 分钟，判定任务结束，准备关机：%s' % (span / 60.0, detail))
        self.ctx.spawn(self._do_shutdown, gen, cfg)
        self.ctx.set_status('任务后关机：准备中（可写 stop.flag 取消）')

    def _do_shutdown(self, gen, cfg):
        if not self._is_current(gen):
            return
        if bool(cfg.get('nas_shutdown', False)):
            ok, msg = _shutdown_nas(cfg)
            self.ctx.log('飞牛关机：%s（%s）' % ('已下发' if ok else '未成功', msg))
            if ok:
                time.sleep(5)   # 让 SSH 会话收尾，避免和本机断网抢时间
            if not self._is_current(gen):
                self.ctx.log('飞牛阶段收到取消，本机不再关机')
                return
        if bool(cfg.get('dry_run', False)):
            self.ctx.log('DRY-RUN 命中关机条件，未执行关机')
            with self._lock:
                self._pending = None
                self._phase = 'idle'
            return
        grace = max(0, int(cfg.get('grace_sec', 60) or 60))
        reason = str(cfg.get('shutdown_reason') or 'WorkBuddy/Qoder 任务已完成，按计划关机')
        ok, how = _request_shutdown(grace, reason)
        with self._lock:
            if not self._is_current(gen):
                return
            self._pending = None
            self._phase = 'requested' if ok else 'idle'
            if not ok:
                self._quiet_since = time.time()   # 失败不立刻重试
        if ok:
            self.ctx.log('已发起关机：%d 秒后（%s）' % (grace, how))
            self.ctx.set_status('任务后关机：系统关机已发起（%d 秒）' % grace)
        else:
            self.ctx.log('关机请求未被系统接受：%s' % how)
            self.ctx.set_status('任务后关机：发起失败，见 guard.log')

    # ------------------------------------------------------------ 钩子
    def on_load(self):
        self._log_summary()

    def on_tick(self, tick):
        # tick.now 为 0 是合法值（离线测试 / 首轮注入），不能用 `or` 兜底。
        now = float(tick.now) if tick.now is not None else time.time()
        if not self._enabled():
            if self._pending is not None or self._phase != 'idle':
                self._cancel('插件已禁用')
            self.ctx.set_status('任务后关机：已关闭')
            return

        if self._stop_marks():
            if self._cancel('检测到 stop.flag / .cancelled'):
                return
            self.ctx.set_status('任务后关机：取消标记仍在，暂不关机')
            return

        start, end, window_text = _window_bounds(self._cfg_section())
        local = _now_local()
        minute = local.tm_hour * 60 + local.tm_min
        day = time.strftime('%Y-%m-%d', local)
        with self._lock:
            if day != self._window_key:
                self._window_key = day
                self._reset_gate()
            if not _in_window(minute, start, end):
                if self._pending is not None or self._phase != 'idle':
                    self._cancel('已过时间窗')
                self._reset_gate()
                self.ctx.set_status('任务后关机：窗口外待命（%s）' % window_text)
                return

        with self._lock:
            phase = self._phase
            pending = self._pending
        if phase == 'preparing' and pending is not None:
            self.ctx.set_status('任务后关机：准备中（可写 stop.flag 取消）')
            return
        if phase == 'requested':
            self.ctx.set_status('任务后关机：系统关机已发起，可 shutdown /a 撤销')
            return

        poll = max(5.0, float(self._c('poll_sec', 30) or 30))
        with self._lock:
            should_sample = (not self._checking and now - self._last_check >= poll)
            if should_sample:
                self._last_check = now
                self._checking = True
                seq = self._sample_seq + 1
                self._sample_seq = seq
        if should_sample:
            self.ctx.spawn(self._worker_sample, seq, dict(self._cfg_section()))

        with self._lock:
            result = self._result
            if result and result.get('seq', 0) > self._handled_seq:
                self._handled_seq = result['seq']
            else:
                result = None

        if result:
            threshold = max(60.0, float(self._c('idle_min', 10) or 10) * 60.0)
            with self._lock:
                if result.get('busy'):
                    self._busy_streak += 1
                    if self._seen_target is False:
                        self._seen_target = True
                    if self._busy_streak >= 2:
                        if self._quiet_since is not None:
                            self.ctx.log('重新变忙，计时归零：%s' % result.get('detail', ''))
                        self._quiet_since = None
                    if now - self._last_report >= 120:
                        self._last_report = now
                else:
                    self._busy_streak = 0
                    if self._quiet_since is None:
                        self._quiet_since = now
                        self.ctx.log('检测到空闲：%s' % result.get('detail', ''))
                    span = max(0.0, now - self._quiet_since)
                    if span >= threshold:
                        if bool(self._c('require_activity', True)) and not self._seen_target:
                            if now - self._last_report >= 300:
                                self._last_report = now
                                self.ctx.log('暂未见到任务在跑，继续守到 %s' % window_text)
                            self._quiet_since = now
                        else:
                            detail = result.get('detail', '')
                            quiet = self._quiet_since
                            self._begin_shutdown(now, span, detail)
                            self.ctx.set_status('任务后关机：准备中（可写 stop.flag 取消）')
                            return
                if self._quiet_since is None:
                    self.ctx.set_status('任务后关机：检测到任务在忙')
                else:
                    left = threshold - (now - self._quiet_since)
                    if bool(self._c('require_activity', True)) and not self._seen_target:
                        self.ctx.set_status('任务后关机：空闲但未见任务，继续守候')
                    else:
                        self.ctx.set_status('任务后关机：空闲 %.1f/%.0f 分钟'
                                            % ((threshold - max(0.0, left)) / 60.0,
                                               threshold / 60.0))

    def on_exit(self, reason):
        # 契约：不得阻塞超过 2 秒。只撤销，不等线程。
        if self._pending is not None or self._phase != 'idle':
            with self._lock:
                self._generation += 1
                self._pending = None
                self._phase = 'idle'
            try:
                self.ctx.spawn(_abort_shutdown)
            except Exception:
                pass

    def on_self_test(self, out):
        start, end, text = _window_bounds(self._cfg_section())
        out.append('idle_shutdown v%s: window=%s, idle_min=%s, grace=%s, '
                   'require_activity=%s, nas=%s, dry_run=%s, phase=%s'
                   % (PLUGIN_VERSION, text, self._c('idle_min', 10),
                      self._c('grace_sec', 60), self._c('require_activity', True),
                      self._c('nas_shutdown', False), self._c('dry_run', False),
                      self._phase))
