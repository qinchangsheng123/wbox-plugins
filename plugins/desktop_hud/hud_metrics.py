# -*- coding: utf-8 -*-
"""桌面监控插件 - 硬件采样（纯 ctypes + 标准库，无第三方依赖）。

设计约束（见 docs/PLUGIN-SPEC.md）：
  - 模块顶层只放定义：不加载 DLL、不建窗口、不读写文件。面板进程也会 import
    本模块，任何顶层副作用都会在面板里重演一次。
  - 所有原生调用懒加载（第一次用到才 LoadLibrary），失败一律降级为 None，
    绝不把异常抛给调用方。
  - 取数来源与口径（均已在本机 Win10 19045 / i5-12600KF / RX 9070 GRE 实测）：
      CPU 占用   GetSystemTimes 两次差分（kernel 含 idle）。
      CPU 频率   标称基准(注册表 ~MHz) × PDH %Processor Performance / 100。
                 实测 CallNtPowerInformation.CurrentMhz 与 PDH Processor Frequency
                 在本机恒报标称 3700，不反映实时；%Processor Performance 随负载
                 变化（空闲 ~70-87%，单核满载 ~127%），与任务管理器“速度”口径一致。
      内存       GlobalMemoryStatusEx（dwMemoryLoad 即占用百分比）。
      GPU 占用   PDH \\GPU Engine(*)\\Utilization Percentage，按目标适配器的
                 engtype 聚合后取最大（与任务管理器口径接近），钳到 0..100。
      显存占用   PDH \\GPU Adapter Memory(*)\\Dedicated Usage（目标适配器）。
      显存总量   注册表 HardwareInformation.qwMemorySize（WMI AdapterRAM 是
                 32 位截断、4GB 封顶，不可信，弃用）。
      GPU 频率   AMD ADL（atiadlxx.dll，Overdrive5 CurrentActivity 引擎时钟）。
                 非 AMD / 无 ADL 时返回 None（界面显示 “—”）。原生调用有访问
                 违例风险，默认关闭，仅在用户开启且独立验证通过后启用。

PDH 注意：所有 Pdh* 函数 restype 必须设为无符号 DWORD，否则 PDH_MORE_DATA
(0x800007D2) 会被当成负数，通配计数器数组永远取空（踩过）。
x64 下 ctypes.CDLL 与 WinDLL 共用同一套调用约定，ADL 用 CDLL 即可。
"""
import ctypes
import re
import threading
import time
import winreg
from ctypes import wintypes as wt

# ---------------------------------------------------------------- DLL 懒加载
_WINDLL = {}
_CDECL = {}


def _windll(name):
    d = _WINDLL.get(name)
    if d is None:
        d = ctypes.WinDLL(name)
        _WINDLL[name] = d
    return d


def _cdll(name):
    d = _CDECL.get(name)
    if d is None:
        d = ctypes.CDLL(name)
        _CDECL[name] = d
    return d


# ---------------------------------------------------------------- 结构体
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ('dwLength', wt.DWORD),
        ('dwMemoryLoad', wt.DWORD),
        ('ullTotalPhys', ctypes.c_ulonglong),
        ('ullAvailPhys', ctypes.c_ulonglong),
        ('ullTotalPageFile', ctypes.c_ulonglong),
        ('ullAvailPageFile', ctypes.c_ulonglong),
        ('ullTotalVirtual', ctypes.c_ulonglong),
        ('ullAvailVirtual', ctypes.c_ulonglong),
        ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
    ]


class _PDH_VALUE_UNION(ctypes.Union):
    _fields_ = [
        ('longValue', ctypes.c_long),
        ('doubleValue', ctypes.c_double),
        ('largeValue', ctypes.c_longlong),
        ('AnsiStringValue', ctypes.c_char_p),
        ('WideStringValue', ctypes.c_wchar_p),
    ]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [('CStatus', wt.DWORD), ('u', _PDH_VALUE_UNION)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [('szName', wt.LPWSTR), ('FmtValue', PDH_FMT_COUNTERVALUE)]


class ADLPMActivity(ctypes.Structure):
    _fields_ = [
        ('iSize', ctypes.c_int),
        ('iEngineClock', ctypes.c_int),
        ('iMemoryClock', ctypes.c_int),
        ('iVddc', ctypes.c_int),
        ('iActivityPercent', ctypes.c_int),
        ('iCurrentBusSpeed', ctypes.c_int),
        ('iCurrentBusLanes', ctypes.c_int),
        ('iMaximumBusLanes', ctypes.c_int),
        ('iReserved1', ctypes.c_int),
        ('iReserved2', ctypes.c_int),
        ('iReserved3', ctypes.c_int),
        ('iReserved4', ctypes.c_int),
    ]


# PMLog（现代 AMD 驱动，Navi/RDNA 起）：Overdrive5 在 RDNA4 上返回 -1，
# 改走 ADL2_New_QueryPMLogData_Get。注意导出名里 PMLog 与 Data 之间没有下划线。
# 数值为自然单位：频率 MHz、温度 °C、电压 mV、活动 %（不再除 100）。
ADL_PMLOG_MAX_SENSORS = 256
PMLOG_CLK_GFXCLK = 1
PMLOG_CLK_MEMCLK = 2
PMLOG_TEMP_EDGE = 8
PMLOG_FAN_RPM = 14
PMLOG_INFO_ACTIVITY_GFX = 19
PMLOG_GFX_VOLTAGE = 21


class ADLSingleSensorData(ctypes.Structure):
    _fields_ = [('supported', ctypes.c_int), ('value', ctypes.c_int)]


class ADLPMLogDataOutput(ctypes.Structure):
    _fields_ = [('size', ctypes.c_int),
                ('sensors', ADLSingleSensorData * ADL_PMLOG_MAX_SENSORS)]


_PDH_FMT_DOUBLE = 0x200
_PDH_MORE_DATA = 0x800007D2
_ENGINE_RE = re.compile(
    r'luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)_phys_(\d+)_eng_\d+_engtype_(.+)$')
_MEM_RE = re.compile(r'luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)_phys_(\d+)')
_DISPLAY_CLASS = (r'SYSTEM\CurrentControlSet\Control\Class'
                  r'\{4d36e968-e325-11ce-bfc1-08002be10318}')


# ---------------------------------------------------------------- DXGI 适配器
# PDH 的 GPU 计数器实例名只给 LUID（luid_0x00000000_0x0000C8E5_phys_0），
# 注册表里没有 AdapterLuid 字段（本机实测三个显示类键都没有）。要在「显示
# 显卡名 / 按显存选卡」上做文章，只能自己枚举 DXGI 把 LUID 映射到名字。
# 纯 ctypes 调 COM：只取 EnumAdapters1(槽 12) + GetDesc(槽 8) + Release(槽 2)，
# 枚举完立刻释放工厂，不缓存 COM 对象、不跨线程持有（避免套间线程问题）。
class GUID(ctypes.Structure):
    _fields_ = [('Data1', wt.DWORD), ('Data2', wt.WORD), ('Data3', wt.WORD),
                ('Data4', ctypes.c_ubyte * 8)]


class LUID(ctypes.Structure):
    _fields_ = [('LowPart', wt.DWORD), ('HighPart', ctypes.c_long)]


class DXGI_ADAPTER_DESC(ctypes.Structure):
    """IDXGIAdapter::GetDesc 的 DXGI_ADAPTER_DESC（304 字节，无 Flags）。

    用 GetDesc 而不是 IDXGIAdapter1::GetDesc1：本机实测 Desc1（槽 10）
    在 ctypes 下会访问违例，GetDesc 稳定，且 LUID / 显存字段完全够用。
    """
    _fields_ = [('Description', ctypes.c_wchar * 128),
                ('VendorId', wt.UINT), ('DeviceId', wt.UINT),
                ('SubSysId', wt.UINT), ('Revision', wt.UINT),
                ('DedicatedVideoMemory', ctypes.c_size_t),
                ('DedicatedSystemMemory', ctypes.c_size_t),
                ('SharedSystemMemory', ctypes.c_size_t),
                ('AdapterLuid', LUID)]


# {770AAE78-F26F-4DBA-A829-253C83D1B387} 的 bytes_le（IID_IDXGIFactory1）
_IID_IDXGIFACTORY1 = bytes([0x78, 0xAE, 0x0A, 0x77, 0x6F, 0xF2, 0xBA, 0x4D,
                            0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87])
_HRESULT = ctypes.c_long


def luid_key(high, low):
    """PDH 实例名里的 LUID 写法：luid_0x{High}_0x{Low}。"""
    return '0x%08X_0x%08X' % (int(high) & 0xFFFFFFFF, int(low) & 0xFFFFFFFF)


class DxgiAdapters(object):
    """活动显示适配器清单：LUID -> {name, vram}，按显存总量降序。

    只在「选卡 / 右键菜单 / 自检」时调用（实测一次枚举 ~0.12 ms），采样
    热路径不碰。任何失败（无 dxgi、老系统、COM 异常）都返回空表并把原因
    记进 last_error，调用方回退到旧规则（按显存占用最大选卡），绝不抛异常。
    """

    TTL_SEC = 5.0
    MAX_ADAPTERS = 16

    def __init__(self):
        self._dxgi = None
        self._iid = None
        self._cache = None
        self._cache_at = 0.0
        self._lock = threading.Lock()
        self.last_error = None

    def _setup(self):
        if self._dxgi is not None:
            return True
        try:
            dxgi = ctypes.WinDLL('dxgi.dll')
            dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID),
                                                ctypes.POINTER(ctypes.c_void_p)]
            dxgi.CreateDXGIFactory1.restype = _HRESULT
        except (OSError, AttributeError) as e:
            self.last_error = 'dxgi 不可用: %s' % (e,)
            return False
        iid = GUID()
        ctypes.memmove(ctypes.byref(iid), _IID_IDXGIFACTORY1, 16)
        self._dxgi = dxgi
        self._iid = iid
        return True

    def list(self, ttl=None):
        """返回适配器快照（list of dict）。ttl=0 表示强制重枚举（右键菜单用）。"""
        ttl = self.TTL_SEC if ttl is None else float(ttl)
        now = time.time()
        with self._lock:
            if self._cache is not None and (now - self._cache_at) < ttl:
                return self._cache
            self._cache = self._enum()
            self._cache_at = now
            return self._cache

    def name_of(self, luid):
        if not luid:
            return None
        for a in self.list():
            if a.get('luid') == luid:
                return a.get('name')
        return None

    def _enum(self):
        if not self._setup():
            return []
        fac = ctypes.c_void_p()
        rc = int(self._dxgi.CreateDXGIFactory1(ctypes.byref(self._iid),
                                               ctypes.byref(fac)))
        if rc != 0 or not fac.value:
            self.last_error = 'CreateDXGIFactory1 rc=0x%08X' % (rc & 0xFFFFFFFF)
            return []
        out = []
        try:
            fvt = ctypes.cast(
                ctypes.cast(fac, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p))
            enum1 = ctypes.WINFUNCTYPE(
                _HRESULT, ctypes.c_void_p, wt.UINT,
                ctypes.POINTER(ctypes.c_void_p))(fvt[12])
            rel_fac = ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)(fvt[2])
            for i in range(self.MAX_ADAPTERS):
                ad = ctypes.c_void_p()
                if enum1(fac, i, ctypes.byref(ad)) != 0 or not ad.value:
                    break
                try:
                    avt = ctypes.cast(
                        ctypes.cast(ad, ctypes.POINTER(ctypes.c_void_p))[0],
                        ctypes.POINTER(ctypes.c_void_p))
                    rel_ad = ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)(avt[2])
                    get_desc = ctypes.WINFUNCTYPE(
                        _HRESULT, ctypes.c_void_p,
                        ctypes.POINTER(DXGI_ADAPTER_DESC))(avt[8])
                    try:
                        d = DXGI_ADAPTER_DESC()
                        if get_desc(ad, ctypes.byref(d)) == 0:
                            name = (d.Description or '').strip()
                            # 微软基本显示适配器是软件渲染，不该在「目标显卡」
                            # 里让用户选到（它会永远显示 0% / 0GB）
                            if name and not name.startswith('Microsoft Basic'):
                                out.append({
                                    'luid': luid_key(d.AdapterLuid.HighPart,
                                                     d.AdapterLuid.LowPart),
                                    'name': name,
                                    'vram': int(d.DedicatedVideoMemory),
                                    'vendor': int(d.VendorId),
                                })
                    finally:
                        rel_ad(ad)
                except Exception:
                    continue
            self.last_error = None
        except Exception as e:
            self.last_error = 'DXGI 枚举失败: %r' % (e,)
            out = []
        finally:
            try:
                rel_fac(fac)
            except Exception:
                pass
        out.sort(key=lambda a: (-a['vram'], a['luid']))
        return out


# ---------------------------------------------------------------- CPU 占用
class CpuUsage(object):
    def __init__(self):
        self._prev = None
        self._fn = None

    def _setup(self):
        fn = _windll('kernel32.dll').GetSystemTimes
        fn.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)] * 3
        fn.restype = wt.BOOL
        self._fn = fn

    def sample(self):
        try:
            if self._fn is None:
                self._setup()
            idle = ctypes.c_ulonglong()
            kern = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            if not self._fn(ctypes.byref(idle), ctypes.byref(kern),
                            ctypes.byref(user)):
                return None
            cur = (idle.value, kern.value, user.value)
            prev = self._prev
            self._prev = cur
            if prev is None:
                return None
            d_idle = cur[0] - prev[0]
            d_total = (cur[1] + cur[2]) - (prev[1] + prev[2])
            if d_total <= 0:
                return None
            busy = 1.0 - (float(d_idle) / float(d_total))
            return max(0.0, min(100.0, busy * 100.0))
        except Exception:
            return None


# ---------------------------------------------------------------- 内存
class Memory(object):
    def __init__(self):
        self._fn = None

    def _setup(self):
        fn = _windll('kernel32.dll').GlobalMemoryStatusEx
        fn.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
        fn.restype = wt.BOOL
        self._fn = fn

    def sample(self):
        try:
            if self._fn is None:
                self._setup()
            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not self._fn(ctypes.byref(m)):
                return None
            total = int(m.ullTotalPhys)
            avail = int(m.ullAvailPhys)
            return {'used': max(0, total - avail), 'total': total,
                    'percent': float(m.dwMemoryLoad)}
        except Exception:
            return None


# ---------------------------------------------------------------- PDH 计数器
class PdhCounters(object):
    """GPU 占用 + 显存占用 + CPU 性能比，共用一条查询，每轮 collect 一次。

    v1.6.0（稳定性，见 README）：
      - 初始化失败不再永久放弃：RETRY_SEC 退避后重建查询。原来 _failed 一旦
        置位就再也不会重试，开机早期 PDH 还没就绪 = 整个登录期显存/GPU 全是「-」。
      - 目标适配器不再一次定终身：连续 MISS_LIMIT 轮在当前读数里都找不到它
        就作废重选（睡眠恢复 / 驱动重启 / 拔卡都会让 LUID 变）。
      - 选卡规则可见可改：auto = DXGI 显存总量最大的适配器；load = 旧规则
        「显存占用最大」；luid = 用户右键菜单指定的具体卡，卡不在了回落 auto。
      - status() 暴露分项状态与最近错误，界面不再只剩一个「-」。
    """

    RETRY_SEC = 20.0        # 初始化 / 收集失败后的退避间隔
    MISS_LIMIT = 3          # 连续多少轮找不到目标 LUID 才判它消失

    def __init__(self):
        self._query = None
        self._c_engine = None
        self._c_mem = None
        self._c_perf = None
        self._target = None
        self._target_name = None
        self._target_source = None
        self._misses = 0
        self._consec_fail = 0
        self._pdh = None
        self._failed = False
        self._next_retry = 0.0
        self._pref_mode = 'auto'
        self._pref_luid = ''
        self.adapters = DxgiAdapters()
        # ---- 诊断（自检 / 日志用）----
        self.init_rc = None
        self.collect_rc = None
        self.reinit_count = 0
        self.target_rebuilds = 0
        self.last_error = None
        self.last_event = None

    def set_target_pref(self, mode, luid=None):
        """选卡偏好：auto / load / luid。变化时作废缓存的目标，下一轮重选。"""
        mode = mode if mode in ('auto', 'load', 'luid') else 'auto'
        luid = (luid or '').strip()
        if mode != self._pref_mode or luid != self._pref_luid:
            self._pref_mode = mode
            self._pref_luid = luid
            self._target = None
            self._target_name = None
            self._target_source = None
            self._misses = 0
            self.last_event = 'target_pref=%s' % mode

    def _setup(self):
        pdh = _windll('pdh.dll')
        for name in ('PdhOpenQueryW', 'PdhAddEnglishCounterW',
                     'PdhCollectQueryData', 'PdhGetFormattedCounterArrayW',
                     'PdhCloseQuery'):
            getattr(pdh, name).restype = wt.DWORD
        self._pdh = pdh
        q = ctypes.c_void_p()
        rc = int(pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)))
        self.init_rc = rc
        if rc != 0:
            self._fail('PdhOpenQueryW rc=0x%08X' % (rc & 0xFFFFFFFF))
            return

        def add(path):
            c = ctypes.c_void_p()
            rc2 = int(pdh.PdhAddEnglishCounterW(q, path, 0, ctypes.byref(c)))
            if rc2 != 0:
                self.last_error = ('PdhAddEnglishCounterW(%s) rc=0x%08X'
                                   % (path, rc2 & 0xFFFFFFFF))
                return None
            return c

        self._query = q
        self._c_engine = add('\\GPU Engine(*)\\Utilization Percentage')
        self._c_mem = add('\\GPU Adapter Memory(*)\\Dedicated Usage')
        self._c_perf = add('\\Processor Information(_Total)\\% Processor Performance')
        if self._c_engine is None and self._c_mem is None and self._c_perf is None:
            self._fail('所有计数器都添加失败')
            pdh.PdhCloseQuery(q)
            self._query = None
            return
        self._failed = False
        self.last_event = 'open_ok'

    def _fail(self, why):
        self._failed = True
        self.last_error = why
        self._next_retry = time.time() + self.RETRY_SEC

    def _collect_array(self, counter):
        if counter is None:
            return []
        pdh = self._pdh
        size = wt.DWORD(0)
        count = wt.DWORD(0)
        rc = pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_DOUBLE, ctypes.byref(size),
            ctypes.byref(count), None)
        if rc != _PDH_MORE_DATA or size.value == 0:
            return []
        buf = (ctypes.c_ubyte * size.value)()
        rc = pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_DOUBLE, ctypes.byref(size),
            ctypes.byref(count), buf)
        if rc != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        out = []
        for i in range(count.value):
            it = items[i]
            if it.FmtValue.CStatus != 0:
                continue
            out.append((it.szName or '', float(it.FmtValue.u.doubleValue)))
        return out

    @staticmethod
    def _luids(mem_items, eng_items):
        """本轮读数里出现过 LUID 集合（判断「目标卡还在不在」用）。"""
        out = set()
        for name, _val in mem_items or ():
            m = _MEM_RE.search(name)
            if m:
                out.add(m.group(1))
        for name, _val in eng_items or ():
            m = _ENGINE_RE.search(name)
            if m:
                out.add(m.group(1))
        return out

    def _pick_target(self, mem_items, luids):
        """选目标适配器。返回 LUID 字符串或 None，并把来源记进 _target_source。"""
        if self._target is not None:
            return self._target
        target = None
        source = None
        if self._pref_mode == 'luid' and self._pref_luid:
            if self._pref_luid in luids:
                target, source = self._pref_luid, 'luid(指定)'
            else:
                self.last_event = 'luid_gone=%s' % self._pref_luid
        if target is None and self._pref_mode in ('auto', 'luid'):
            # auto：DXGI 显存总量最大的适配器。旧规则「此刻占用最高」在双卡机
            # 上会被浏览器 / 录屏 / 编码器抢走读数，用户看到的不是游戏那张卡。
            for a in self.adapters.list():
                if a.get('vram') and a['luid'] in luids:
                    target, source = a['luid'], 'auto(显存最大)'
                    break
        if target is None:
            best = None
            best_val = -1.0
            for name, val in mem_items:
                m = _MEM_RE.search(name)
                if m and val > best_val:
                    best_val = val
                    best = m.group(1)
            if best is not None:
                target, source = best, 'load(占用最大)'
        self._target = target
        self._target_source = source
        self._target_name = self.adapters.name_of(target) if target else None
        return target

    def sample(self):
        try:
            if self._failed:
                if time.time() < self._next_retry:
                    return None
                self._failed = False      # 退避到期，重建查询
                self.reinit_count += 1
                self.last_event = 'retry_open'
            if self._query is None:
                self._setup()
            if self._query is None:
                return None
            rc = int(self._pdh.PdhCollectQueryData(self._query))
            self.collect_rc = rc
            if rc != 0:
                self._consec_fail += 1
                self.last_error = 'PdhCollectQueryData rc=0x%08X' % (rc & 0xFFFFFFFF)
                if self._consec_fail >= 5:
                    # GPU 计数器提供者重启（驱动升级 / TDR）后旧句柄会一直报错
                    self._consec_fail = 0
                    self.close()
                    self._fail('收集连续失败，重建查询')
                return None
            self._consec_fail = 0
            mem_items = self._collect_array(self._c_mem)
            eng_items = self._collect_array(self._c_engine)
            perf_items = self._collect_array(self._c_perf)
            luids = self._luids(mem_items, eng_items)
            if self._target is not None and self._target not in luids:
                self._misses += 1
                if self._misses >= self.MISS_LIMIT:
                    self.last_event = 'target_lost=%s' % self._target
                    self._target = None
                    self._target_name = None
                    self._target_source = None
                    self._misses = 0
                    self.target_rebuilds += 1
            elif self._target is not None:
                self._misses = 0
            target = self._pick_target(mem_items, luids)

            vram_used = None
            if target is not None:
                for name, val in mem_items:
                    m = _MEM_RE.search(name)
                    if m and m.group(1) == target:
                        vram_used = int(val)
                        break

            gpu_pct = None
            if eng_items:
                per_type = {}
                for name, val in eng_items:
                    m = _ENGINE_RE.search(name)
                    if not m:
                        continue
                    luid, engtype = m.group(1), m.group(3)
                    if target is not None and luid != target:
                        continue
                    per_type[engtype] = per_type.get(engtype, 0.0) + val
                if per_type:
                    gpu_pct = max(0.0, min(100.0, max(per_type.values())))

            cpu_perf = None
            for name, val in perf_items:
                if name == '_Total':
                    cpu_perf = val
                    break
            return {'gpu_percent': gpu_pct, 'vram_used': vram_used,
                    'target': target, 'target_name': self._target_name,
                    'cpu_perf': cpu_perf}
        except Exception:
            return None

    def status(self):
        """分项状态：自检 / 日志用。不触发任何原生调用（adapters 走 5s 缓存）。"""
        return {
            'ok': self._query is not None and not self._failed,
            'init_rc': self.init_rc,
            'collect_rc': self.collect_rc,
            'reinit': self.reinit_count,
            'rebuilds': self.target_rebuilds,
            'misses': self._misses,
            'target': self._target,
            'target_name': self._target_name,
            'target_source': self._target_source,
            'pref': '%s%s' % (self._pref_mode,
                              ('(%s)' % self._pref_luid) if self._pref_luid else ''),
            'error': self.last_error,
            'event': self.last_event,
            'adapters': self.adapters.list(),
            'dxgi_error': self.adapters.last_error,
        }

    def close(self):
        try:
            if self._query is not None and self._pdh is not None:
                self._pdh.PdhCloseQuery(self._query)
        except Exception:
            pass
        self._query = None


# ---------------------------------------------------------------- 注册表信息
def cpu_nominal_mhz():
    """标称基准频率（注册表 ~MHz）。用于把 %Processor Performance 换算成 MHz。"""
    try:
        k = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r'HARDWARE\DESCRIPTION\System\CentralProcessor\0')
        val, _typ = winreg.QueryValueEx(k, '~MHz')
        winreg.CloseKey(k)
        n = int(val)
        return n if 0 < n < 100000 else None
    except OSError:
        return None


def display_adapter():
    """返回 (名称, 显存总量字节)，取各显示适配器中显存最大者（独显）。"""
    best_name = None
    best = 0
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS)
    except OSError:
        return (None, None)
    try:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                k = winreg.OpenKey(root, sub)
                val, typ = winreg.QueryValueEx(
                    k, 'HardwareInformation.qwMemorySize')
                try:
                    name, _ = winreg.QueryValueEx(k, 'DriverDesc')
                except OSError:
                    name = None
                winreg.CloseKey(k)
                if typ == winreg.REG_BINARY:
                    n = int.from_bytes(bytes(val[:8]), 'little')
                elif typ in (winreg.REG_QWORD, winreg.REG_DWORD):
                    n = int(val)
                else:
                    continue
                if n > best:
                    best = n
                    best_name = name
            except OSError:
                continue
    finally:
        winreg.CloseKey(root)
    return (best_name, best or None)


# ---------------------------------------------------------------- GPU 频率 (AMD ADL)
class AdlClock(object):
    """AMD GPU 当前引擎时钟。优先 PMLog（Navi/RDNA 起），回退 Overdrive5（老 GCN）。

    仅在 AMD + atiadlxx.dll 存在时可用。原生调用有访问违例风险：所有入口
    try/except，结构体 size 字段显式置位，分配回调用 msvcrt.malloc（与
    atiadlxx 的 CRT 匹配）。失败/不可用返回 None，但原因记进 last_error 并由
    diagnostics() 暴露——否则编程错误会被宽 except 吞成「传感器暂时没值」，
    v1.3.0 的 _cdecl 大小写笔误就是这样藏住的。
    """

    RETRY_SEC = 60.0    # 初始化失败后的退避重试间隔（驱动更新/睡眠恢复可自愈）

    def __init__(self):
        self._adl = None
        self._ctx = ctypes.c_void_p()
        self._idx = None
        self._malloc_cb = None
        self._ready = False
        self._failed = False
        self._next_retry = 0.0
        self.last_error = None
        self._divisor = 100.0   # 旧路径 iEngineClock 单位 10kHz -> MHz
        self._pmlog = False     # 新路径 PMLog：传感器值已是 MHz，不除
        self._pmlog_buf = None

    def _fail(self, why):
        self._failed = True
        self._ready = False
        self.last_error = why
        self._next_retry = time.time() + self.RETRY_SEC

    def _setup(self):
        try:
            adl = _cdll('atiadlxx.dll')
        except OSError as e:
            self._fail('atiadlxx.dll 不可用: %s' % e)
            return
        self._adl = adl
        msvcrt = _cdll('msvcrt.dll')
        msvcrt.malloc.restype = ctypes.c_void_p
        msvcrt.malloc.argtypes = [ctypes.c_size_t]
        cb_type = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int)

        @cb_type
        def _malloc(size):
            return msvcrt.malloc(size)

        self._malloc_cb = _malloc   # 保持引用，防止被 GC
        adl.ADL2_Main_Control_Create.argtypes = [
            cb_type, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        adl.ADL2_Main_Control_Create.restype = ctypes.c_int
        adl.ADL2_Adapter_NumberOfAdapters_Get.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        adl.ADL2_Adapter_NumberOfAdapters_Get.restype = ctypes.c_int
        adl.ADL2_Adapter_Active_Get.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        adl.ADL2_Adapter_Active_Get.restype = ctypes.c_int
        adl.ADL2_Overdrive5_CurrentActivity_Get.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ADLPMActivity)]
        adl.ADL2_Overdrive5_CurrentActivity_Get.restype = ctypes.c_int
        try:
            adl.ADL2_New_QueryPMLogData_Get.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ADLPMLogDataOutput)]
            adl.ADL2_New_QueryPMLogData_Get.restype = ctypes.c_int
            self._pmlog = True
            self._pmlog_buf = ADLPMLogDataOutput()
            self._pmlog_buf.size = ctypes.sizeof(ADLPMLogDataOutput)
        except AttributeError:
            self._pmlog = False   # 老驱动没有 PMLog，回退 Overdrive5
        adl.ADL2_Main_Control_Destroy.argtypes = [ctypes.c_void_p]
        adl.ADL2_Main_Control_Destroy.restype = ctypes.c_int

        rc = adl.ADL2_Main_Control_Create(_malloc, 1, ctypes.byref(self._ctx))
        if rc != 0:
            self._fail('ADL2_Main_Control_Create rc=%d' % rc)
            return
        num = ctypes.c_int(0)
        rc = adl.ADL2_Adapter_NumberOfAdapters_Get(self._ctx, ctypes.byref(num))
        if rc != 0:
            self._fail('ADL2_Adapter_NumberOfAdapters_Get rc=%d' % rc)
            return
        for idx in range(num.value):
            active = ctypes.c_int(0)
            if adl.ADL2_Adapter_Active_Get(self._ctx, idx,
                                           ctypes.byref(active)) == 0 and active.value:
                self._idx = idx
                break
        if self._idx is None:
            self._fail('没有活动适配器')
            return
        self._ready = True
        self.last_error = None

    def sample(self):
        try:
            if self._failed:
                if time.time() < self._next_retry:
                    return None
                self._failed = False      # 退避到期，重试一次
            if not self._ready:
                self._setup()
            if not self._ready:
                return None
            if self._pmlog:
                buf = self._pmlog_buf
                rc = self._adl.ADL2_New_QueryPMLogData_Get(
                    self._ctx, self._idx, ctypes.byref(buf))
                if rc == 0:
                    s = buf.sensors[PMLOG_CLK_GFXCLK]
                    if s.supported and 0 < s.value < 10000:
                        return float(s.value)   # PMLog 已是 MHz，不除 100
            act = ADLPMActivity()
            act.iSize = ctypes.sizeof(ADLPMActivity)
            rc = self._adl.ADL2_Overdrive5_CurrentActivity_Get(
                self._ctx, self._idx, ctypes.byref(act))
            if rc != 0:
                return None
            mhz = act.iEngineClock / self._divisor
            if 0 < mhz < 10000:
                return float(mhz)
            return None
        except Exception as e:
            # 编程错误（NameError 之类）也走这里：记下来，别假装只是「没值」
            self.last_error = '%s: %s' % (type(e).__name__, e)
            return None

    def close(self):
        try:
            if self._ready and self._adl is not None:
                self._adl.ADL2_Main_Control_Destroy(self._ctx)
        except Exception:
            pass
        self._ready = False


# ---------------------------------------------------------------- 汇总
class Sampler(object):
    """聚合各后端。sample() 返回当前读数 dict，缺失项为 None。"""

    def __init__(self, enable_gpu_clock=False):
        self.cpu = CpuUsage()
        self.mem = Memory()
        self.pdh = PdhCounters()
        self.adl = AdlClock() if enable_gpu_clock else None
        self._enable_gpu_clock = enable_gpu_clock
        self._nominal = None
        self._nominal_tried = False
        self.gpu_name = None
        self._vram_total = None
        self._adapter_tried = False

    def _ensure_static(self):
        if not self._nominal_tried:
            self._nominal = cpu_nominal_mhz()
            self._nominal_tried = True
        if not self._adapter_tried:
            self.gpu_name, self._vram_total = display_adapter()
            self._adapter_tried = True

    def set_gpu_clock_enabled(self, on):
        on = bool(on)
        if on and self.adl is None:
            self.adl = AdlClock()
        elif not on and self.adl is not None:
            self.adl.close()
            self.adl = None
        self._enable_gpu_clock = on

    def set_gpu_target(self, mode, luid=None):
        """选卡偏好（auto / load / luid）。只在采样线程里调用，避免与采样竞争。"""
        self.pdh.set_target_pref(mode, luid)

    def adapters(self, ttl=None):
        """活动适配器清单（右键菜单 / 自检用）。失败返回空表。"""
        try:
            return self.pdh.adapters.list(ttl=ttl)
        except Exception:
            return []

    def sample(self):
        self._ensure_static()
        out = {
            'cpu': None, 'cpu_mhz': None,
            'ram_used': None, 'ram_total': None, 'ram_percent': None,
            'gpu': None, 'gpu_mhz': None,
            'vram_used': None, 'vram_total': self._vram_total,
            'vram_percent': None,
        }
        out['cpu'] = self.cpu.sample()
        m = self.mem.sample()
        if m:
            out['ram_used'] = m['used']
            out['ram_total'] = m['total']
            out['ram_percent'] = m['percent']
        p = self.pdh.sample()
        if p:
            out['gpu'] = p['gpu_percent']
            out['vram_used'] = p['vram_used']
            out['gpu_target'] = p.get('target')
            out['gpu_target_name'] = p.get('target_name')
            if p['cpu_perf'] is not None and self._nominal:
                out['cpu_mhz'] = self._nominal * p['cpu_perf'] / 100.0
        if out['vram_used'] is not None and out['vram_total']:
            out['vram_percent'] = 100.0 * out['vram_used'] / out['vram_total']
        if self.adl is not None:
            out['gpu_mhz'] = self.adl.sample()
        return out

    def diagnostics(self):
        st = self.pdh.status()
        ads = st.get('adapters') or []
        lines = [
            'cpu_backend=GetSystemTimes',
            'cpu_freq=registry~MHz(%s) x PDH %%ProcessorPerformance' % self._nominal,
            'ram_backend=GlobalMemoryStatusEx',
            'gpu_backend=PDH(GPU Engine/Adapter Memory) ok=%s init_rc=%s '
            'collect_rc=%s reinit=%s rebuilds=%s'
            % (st['ok'], st['init_rc'], st['collect_rc'], st['reinit'],
               st['rebuilds']),
            'gpu_target=%s name=%s source=%s pref=%s misses=%s'
            % (st['target'], st['target_name'], st['target_source'],
               st['pref'], st['misses']),
            'gpu_adapters=%d [%s]' % (len(ads), '; '.join(
                '%s %s %.1fG' % (a['luid'], a['name'],
                                 a['vram'] / 1073741824.0) for a in ads)),
            'gpu_name=%s vram_total=%s' % (self.gpu_name, self._vram_total),
            'gpu_error=%s event=%s dxgi_error=%s'
            % (st['error'] or 'none', st['event'] or 'none',
               st['dxgi_error'] or 'none'),
            'gpu_clock_backend=%s' % (
                ('AMD ADL PMLog' if self.adl._pmlog else 'AMD ADL Overdrive5')
                if self.adl is not None else 'disabled'),
            'gpu_clock_error=%s' % (
                (self.adl.last_error or 'none') if self.adl is not None else 'n/a'),
        ]
        return lines

    def close(self):
        self.pdh.close()
        if self.adl is not None:
            self.adl.close()


# ---------------------------------------------------------------- 独立自测
if __name__ == '__main__':
    import sys
    import time
    print('pointer_size=%d (x64=%s)' % (ctypes.sizeof(ctypes.c_void_p),
                                        ctypes.sizeof(ctypes.c_void_p) == 8))
    s = Sampler(enable_gpu_clock='--adl' in sys.argv)
    for i in range(4):
        d = s.sample()
        print('--- sample %d ---' % (i + 1))
        for k in ('cpu', 'cpu_mhz', 'ram_percent', 'ram_used', 'ram_total',
                  'gpu', 'gpu_mhz', 'vram_used', 'vram_total', 'vram_percent'):
            v = d[k]
            if isinstance(v, float):
                v = round(v, 2)
            print('  %-13s = %s' % (k, v))
        time.sleep(1.0)
    for ln in s.diagnostics():
        print(ln)
    s.close()