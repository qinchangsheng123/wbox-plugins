# -*- coding: utf-8 -*-
"""afterburner / gamepp 共用：进程优雅关闭 + 目录 DACL 权限检查。

**唯一正文就是本文件（`shared/proc_util.py`）。** 两个插件目录下的
`proc_util.py` 是 `tools/pack.py` 打包前刷新的逐字节副本，不要直接编辑副本：
改这里，再跑一次 `python tools/pack.py`，三份会自动对齐。

为什么不放 `src/`：`ctx.import_core` 只认 `plugin_host.CORE_MODULES` 白名单，
新增内核模块要连带重封 `WBOX.exe`（产品级发版）。放在插件包里可以随插件热部署，
不碰冻结运行时。插件由 plugin_host 以**包**形式加载
（`spec_from_file_location(..., submodule_search_locations=[dir])`），
所以 `from . import proc_util` 在守护与离线测试里都可用。

两块能力：
  1. `dir_write_state()` —— 直接用 advapi32 读 DACL，取代原来的
     `powershell Get-Acl`。原实现有两个毛病：判据是 `'LOOSE' in out`，
     命令失败/超时/模块加载失败时输出里没有 LOOSE，于是**失败被当成安全**；
     且每次冷启动 PowerShell 约 754ms，还卡在开局钩子里。
     现在三态返回 LOOSE / TIGHT / UNKNOWN，微秒级，无外部进程。
  2. `close_processes()` —— 一批目标先发 WM_CLOSE 再统一等待，
     用 WaitForMultipleObjects 感知退出，取代「逐个 sleep 0.4 + 全表快照」。
     保住两条铁律语义：等待中**游戏回归即中止**、pid 被复用即停手。
"""
import ctypes
import os
import time
from ctypes import wintypes as wt

# ---------------------------------------------------------------- 三态
LOOSE = 'LOOSE'        # 普通用户可写：提权启动前无法保证 exe 未被替换
TIGHT = 'TIGHT'        # 已收紧
UNKNOWN = 'UNKNOWN'    # 没查成（API 失败 / 平台不支持）——**不等于安全**

# Everyone / Authenticated Users / BUILTIN\Users
# 与原 PowerShell 判据 'Users|Everyone|Authenticated' 对应的三个 well-known SID。
_RISKY_SIDS = frozenset(('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545'))

# 写类权限：能改文件内容、能删、能改 ACL / 属主，都算「可替换 exe」
_WRITE_MASK = (0x0002 | 0x0004 | 0x0010 | 0x0100 |   # WRITE_DATA/APPEND/WRITE_EA/WRITE_ATTR
               0x00010000 | 0x00040000 | 0x00080000 |  # DELETE / WRITE_DAC / WRITE_OWNER
               0x40000000 | 0x10000000)                # GENERIC_WRITE / GENERIC_ALL

_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 4
_ACCESS_ALLOWED_ACE_TYPE = 0
_AclSizeInformation = 2

_advapi32 = None
_kernel32 = None


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [('AceCount', wt.DWORD),
                ('AclBytesInUse', wt.DWORD),
                ('AclBytesFree', wt.DWORD)]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [('AceType', ctypes.c_ubyte),
                ('AceFlags', ctypes.c_ubyte),
                ('AceSize', wt.WORD)]


class _ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [('Header', _ACE_HEADER),
                ('Mask', wt.DWORD),
                ('SidStart', wt.DWORD)]


def _adv():
    global _advapi32
    if _advapi32 is None:
        d = ctypes.WinDLL('advapi32', use_last_error=True)
        d.GetNamedSecurityInfoW.argtypes = [
            wt.LPCWSTR, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p)]
        d.GetNamedSecurityInfoW.restype = wt.DWORD
        d.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        wt.DWORD, ctypes.c_int]
        d.GetAclInformation.restype = wt.BOOL
        d.GetAce.argtypes = [ctypes.c_void_p, wt.DWORD,
                             ctypes.POINTER(ctypes.c_void_p)]
        d.GetAce.restype = wt.BOOL
        d.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p)]
        d.ConvertSidToStringSidW.restype = wt.BOOL
        _advapi32 = d
    return _advapi32


def _k32():
    global _kernel32
    if _kernel32 is None:
        d = ctypes.WinDLL('kernel32', use_last_error=True)
        d.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        d.OpenProcess.restype = wt.HANDLE
        d.WaitForMultipleObjects.argtypes = [wt.DWORD,
                                             ctypes.POINTER(wt.HANDLE),
                                             wt.BOOL, wt.DWORD]
        d.WaitForMultipleObjects.restype = wt.DWORD
        d.CloseHandle.argtypes = [wt.HANDLE]
        d.CloseHandle.restype = wt.BOOL
        d.LocalFree.argtypes = [ctypes.c_void_p]
        d.LocalFree.restype = ctypes.c_void_p
        # v2.0.0 进程身份：创建时间 + 映像名，用于关闭前复核 PID 是否被复用
        d.GetProcessTimes.argtypes = [wt.HANDLE,
                                      ctypes.POINTER(wt.FILETIME),
                                      ctypes.POINTER(wt.FILETIME),
                                      ctypes.POINTER(wt.FILETIME),
                                      ctypes.POINTER(wt.FILETIME)]
        d.GetProcessTimes.restype = wt.BOOL
        d.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                                 wt.LPWSTR,
                                                 ctypes.POINTER(wt.DWORD)]
        d.QueryFullProcessImageNameW.restype = wt.BOOL
        d.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
        d.TerminateProcess.restype = wt.BOOL
        d.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        d.WaitForSingleObject.restype = wt.DWORD
        _kernel32 = d
    return _kernel32


def _sid_string(sid_ptr):
    """SID -> 'S-1-...'；失败返回空串。ConvertSidToStringSidW 的缓冲要 LocalFree。"""
    adv = _adv()
    buf = ctypes.c_void_p()
    if not adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(buf)):
        return ''
    try:
        return ctypes.wstring_at(buf.value) if buf.value else ''
    finally:
        if buf.value:
            _k32().LocalFree(buf.value)


def dir_write_state(path):
    """目录是否对普通用户开放写权限。返回 (state, detail)。

    state 为 LOOSE / TIGHT / UNKNOWN；detail 是一句可读说明（日志用）。
    任何异常都归 UNKNOWN —— 调用方必须把 UNKNOWN 当「没查成」，不能当安全。

    NULL DACL（完全没有 DACL）等于对所有人全权开放，按 LOOSE 处理。
    继承型 ACE 也算数：目录上的可写权限会被里面的 exe 继承。
    """
    d = os.path.dirname(path) or path
    if not d:
        return UNKNOWN, '路径为空'
    try:
        adv = _adv()
        sd = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        rc = adv.GetNamedSecurityInfoW(
            d, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(sd))
        if rc != 0:
            return UNKNOWN, 'GetNamedSecurityInfoW 失败 rc=%d' % rc
        try:
            if not dacl.value:
                return LOOSE, '目录没有 DACL（NULL DACL = 所有人可写）'
            info = _ACL_SIZE_INFORMATION()
            if not adv.GetAclInformation(dacl, ctypes.byref(info),
                                         ctypes.sizeof(info),
                                         _AclSizeInformation):
                return UNKNOWN, 'GetAclInformation 失败'
            for i in range(info.AceCount):
                ace_ptr = ctypes.c_void_p()
                if not adv.GetAce(dacl, i, ctypes.byref(ace_ptr)):
                    continue
                hdr = ctypes.cast(ace_ptr,
                                  ctypes.POINTER(_ACE_HEADER)).contents
                if hdr.AceType != _ACCESS_ALLOWED_ACE_TYPE:
                    continue        # Deny 不算风险；审计型 ACE 也不看
                ace = ctypes.cast(ace_ptr,
                                  ctypes.POINTER(_ACCESS_ALLOWED_ACE)).contents
                if not (ace.Mask & _WRITE_MASK):
                    continue
                sid = _sid_string(ctypes.c_void_p(
                    ace_ptr.value + _ACCESS_ALLOWED_ACE.SidStart.offset))
                if sid in _RISKY_SIDS:
                    return LOOSE, '%s 拥有写权限（mask=0x%08X）' % (sid, ace.Mask)
            return TIGHT, '未发现普通用户可写的 Allow ACE'
        finally:
            if sd.value:
                _k32().LocalFree(sd.value)
    except Exception as e:
        return UNKNOWN, '%s: %s' % (type(e).__name__, e)


def check_exe_dir(ctx, path, checked):
    """启动前的目录权限告警（只告警不阻断，与原语义一致）。

    `checked` 是调用方持有的 set，按目录去重；UNKNOWN 也会记进去，
    避免每局重复刷同一条日志——但日志文案明确写「未完成」，不谎报安全。
    """
    d = os.path.dirname(path) or path
    key = os.path.normcase(d)
    if key in checked:
        return
    checked.add(key)
    state, detail = dir_write_state(path)
    if state == LOOSE:
        ctx.log('安全提示：%s 对普通用户可写（%s），提权启动前无法保证 exe 未被替换。'
                '需要收紧时执行：icacls "%s" /inheritance:r '
                '/grant:r "Administrators:(OI)(CI)F" /grant:r "SYSTEM:(OI)(CI)F"'
                % (d, detail, d))
    elif state == UNKNOWN:
        ctx.log('权限检查未完成：%s（%s）——本次无法判定目录是否可写，'
                '不代表权限已收紧' % (d, detail))


# ---------------------------------------------------------------- 关闭
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_MAX_HANDLES = 64       # WaitForMultipleObjects 的硬上限


def _has_exited(handle):
    """句柄是否已 signaled（进程已退出）。拿不到结果按"未退出"处理。"""
    try:
        return _k32().WaitForSingleObject(handle, 0) == _WAIT_OBJECT_0
    except Exception:
        return False


# ---------------------------------------------------------------- 进程身份
# v2.0.0：只看「pid 在不在 + 名字对不对」不足以确认身份——进程退出后 PID 会被
# 系统立刻复用，同名进程（尤其 RTSS / 游戏加加这种会自己重启的）在同一个
# pid 上重新出现时，按 pid 强杀就会误关"用户后来自己开的那一份"。
# 这里用**创建时间**（FILETIME 100ns 计数）+ 映像名做稳定身份，并在动手前
# 复核；同时从打开句柄起 PID 就被钉住（进程未退出前系统不会把这个 PID 再分配）。
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001


def _open_proc(pid):
    """打开用于「等待 + 校验身份 + 结束」的句柄；失败返回 None（受保护进程等）。"""
    try:
        h = _k32().OpenProcess(
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
            False, int(pid))
        return h or None
    except Exception:
        return None


def _identity_of(handle):
    """-> (created_ticks, image_basename)；拿不到的那项为 None / ''。"""
    k32 = _k32()
    created = None
    try:
        c, e, kt, ut = (wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME())
        if k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(kt), ctypes.byref(ut)):
            created = (int(c.dwHighDateTime) << 32) | int(c.dwLowDateTime)
    except Exception:
        created = None
    name = ''
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf,
                                          ctypes.byref(size)) and buf.value:
            name = os.path.basename(buf.value)
    except Exception:
        name = ''
    return created, name


def process_identity(pid):
    """进程身份快照：{'pid','created','created_at','name'}；打不开返回 None。

    `created_at` 是换算后的 time.time() 口径，可直接和"我什么时候启动的"比。
    受保护进程可能拿不到创建时间（created=None），调用方必须按"未校验"处理。
    """
    h = _open_proc(pid)
    if not h:
        return None
    try:
        created, name = _identity_of(h)
    finally:
        try:
            _k32().CloseHandle(h)
        except Exception:
            pass
    return {'pid': int(pid), 'created': created,
            'created_at': filetime_to_time(created) if created else None,
            'name': name}


def process_created(pid):
    """进程创建时刻（time.time 口径）；拿不到返回 None。"""
    ident = process_identity(pid)
    return ident['created_at'] if ident else None


def filetime_to_time(ticks):
    """FILETIME(100ns since 1601) -> Unix 秒。"""
    if not ticks:
        return None
    try:
        return (int(ticks) - 116444736000000000) / 10000000.0
    except (TypeError, ValueError):
        return None


def _same_process(pid, name, created):
    """复核 pid 现在还是不是当初那个进程。返回 (ok, detail)。

    created 为 None 表示当初就没拿到创建时间：只能比名字，按"不可强杀"处理
    （调用方用返回的 detail 区分「确认同一进程」与「仅名字相同」）。
    """
    ident = process_identity(pid)
    if ident is None:
        return False, '进程已退出或无法打开'
    if ident['name'] and name and ident['name'].lower() != name.lower():
        return False, 'pid 现在是 %s' % ident['name']
    if created is None or ident['created'] is None:
        return False, '创建时间不可用（仅名字相同）'
    if int(ident['created']) != int(created):
        return False, '创建时间已变（pid 被复用）'
    return True, ''


def close_processes(ctx, core, targets, close_wait, force_close):
    """一批进程：先逐个发 WM_CLOSE，再统一等待，最后按配置强杀。

    targets 按优先级排序（父进程在前），发送顺序保持不变——RTSS 主进程
    仍最先收到关闭请求，才不会重新拉起子组件。

    与旧实现的语义差异只有一处：**等待是并行的**，一批目标共享一个
    close_wait 期限，而不是每个各等 close_wait。其余逐字保留：
      - 每片复核一次游戏回归，回归即中止整批（不再强杀）；
      - 快照失败即停手，交给上层 retry_close；
      - pid 被复用即停手；
      - force_close=False 时只请求不强杀。

    返回 {pid: verdict}，verdict ∈
      exited / aborted / reused / unknown / forced / left / gone / skipped
    """
    result = {}
    targets = [(int(pid), name) for pid, name in targets if name]
    if not targets:
        return result
    procs = ctx.snapshot()
    if procs is None:
        ctx.log('取进程表失败，本轮不处理 %d 个关闭目标' % len(targets))
        return dict((pid, 'unknown') for pid, _n in targets)

    live = []       # [pid, name, handle, created, verified]
    opened = []     # 所有开过的句柄，finally 里统一释放（含已退出目标的）
    k32 = _k32()
    for pid, name in targets:
        cur = procs.get(pid)
        if cur is None:
            result[pid] = 'gone'
            continue
        if cur != name:
            ctx.log('pid %d 已不是 %s（%s），跳过' % (pid, name, cur))
            result[pid] = 'skipped'
            continue
        # 先拿句柄再发关闭请求：句柄一开，PID 在进程退出前不会被复用，
        # 后面整套等待/强杀都钉在这一个进程对象上，而不是一个可能被重发的号。
        h = _open_proc(pid)
        created, real = (None, '')
        if h:
            created, real = _identity_of(h)
            if real and real.lower() != name.lower():
                ctx.log('pid %d 现在是 %s（期望 %s），判定 PID 复用，跳过'
                        % (pid, real, name))
                result[pid] = 'reused'
                try:
                    k32.CloseHandle(h)
                except Exception:
                    pass
                continue
        verified = created is not None
        if not verified:
            ctx.log('pid %d（%s）拿不到创建时间，只请求退出、不强制结束'
                    % (pid, name))
        if h:
            opened.append(h)
        core.run_hidden([core.TASKKILL, '/PID', str(pid)])
        live.append([pid, name, h, created, verified])

    deadline = time.time() + max(0.0, float(close_wait))
    try:
        while live:
            remain = deadline - time.time()
            if remain <= 0:
                break
            wait_ms = max(1, min(200, int(remain * 1000)))
            with_h = [item for item in live if item[2]]
            signaled = False
            if with_h:
                n = min(len(with_h), _MAX_HANDLES)
                arr = (wt.HANDLE * n)(*[item[2] for item in with_h[:n]])
                rc = k32.WaitForMultipleObjects(n, arr, False, wait_ms)
                if _WAIT_OBJECT_0 <= rc < _WAIT_OBJECT_0 + n:
                    item = with_h[rc - _WAIT_OBJECT_0]
                    ctx.log('已优雅退出 %s (pid %d)' % (item[1], item[0]))
                    result[item[0]] = 'exited'
                    live.remove(item)
                    signaled = True
            if not with_h:
                time.sleep(wait_ms / 1000.0)
            if signaled:
                # 刚有进程退出，立刻回到等待；游戏复核在下一片做
                continue
            now_procs = ctx.snapshot()
            if now_procs is None:
                ctx.log('取进程表失败，停止等待剩余 %d 个进程' % len(live))
                for pid, _n, _h, _c, _v in live:
                    result[pid] = 'unknown'
                live = []
                break
            if ctx.games_present(now_procs):
                ctx.log('关闭等待中检测到游戏回归，中止关闭（剩余 %d 个）' % len(live))
                for pid, _n, _h, _c, _v in live:
                    result[pid] = 'aborted'
                live = []
                break
            for item in list(live):
                cur = now_procs.get(item[0])
                if cur is None:
                    ctx.log('已优雅退出 %s (pid %d)' % (item[1], item[0]))
                    result[item[0]] = 'exited'
                    live.remove(item)
                elif cur != item[1]:
                    ctx.log('pid %d 已被复用（现在是 %s），停止处理'
                            % (item[0], cur))
                    result[item[0]] = 'reused'
                    live.remove(item)
        for pid, name, h, created, verified in live:
            if h and _has_exited(h):
                ctx.log('已退出 %s (pid %d)' % (name, pid))
                result[pid] = 'exited'
                continue
            if not force_close:
                ctx.log('%s (pid %d) 未退出，按配置不强制' % (name, pid))
                result[pid] = 'left'
                continue
            # 强杀前最后一次确认身份：创建时间对不上就是另一个进程，宁可漏关。
            ok, why = (False, '无句柄')
            if h:
                ok, why = _same_process(pid, name, created)
            if not ok:
                ctx.log('%s (pid %d) 未退出，但身份未确认（%s），不强制结束'
                        % (name, pid, why))
                result[pid] = 'left'
                continue
            # 有校验过的句柄就走 TerminateProcess：句柄钉住了进程对象，
            # 不存在"taskkill 时 PID 已经换成别人"的窗口。
            try:
                killed = bool(k32.TerminateProcess(h, 1))
            except Exception:
                killed = False
            if killed:
                ctx.log('WM_CLOSE 未退出（通常是关闭到托盘），已强制结束 %s (pid %d)'
                        % (name, pid))
                result[pid] = 'forced'
            else:
                core.run_hidden([core.TASKKILL, '/PID', str(pid), '/F'])
                ctx.log('WM_CLOSE 未退出，TerminateProcess 未成功，改用 taskkill 结束 '
                        '%s (pid %d)' % (name, pid))
                result[pid] = 'forced'
    finally:
        for h in opened:
            try:
                k32.CloseHandle(h)
            except Exception:
                pass
    return result


def close_one(ctx, core, pid, name, close_wait, force_close):
    """单目标关闭：语义与 close_processes 一致，返回 verdict 字符串。"""
    return close_processes(ctx, core, [(pid, name)],
                           close_wait, force_close).get(int(pid), 'gone')