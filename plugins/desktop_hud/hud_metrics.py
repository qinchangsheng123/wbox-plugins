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
    """GPU 占用 + 显存占用 + CPU 性能比，共用一条查询，每轮 collect 一次。"""

    def __init__(self):
        self._query = None
        self._c_engine = None
        self._c_mem = None
        self._c_perf = None
        self._target = None
        self._pdh = None
        self._failed = False

    def _setup(self):
        pdh = _windll('pdh.dll')
        for name in ('PdhOpenQueryW', 'PdhAddEnglishCounterW',
                     'PdhCollectQueryData', 'PdhGetFormattedCounterArrayW',
                     'PdhCloseQuery'):
            getattr(pdh, name).restype = wt.DWORD
        self._pdh = pdh
        q = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
            self._failed = True
            return
        self._query = q

        def add(path):
            c = ctypes.c_void_p()
            rc = pdh.PdhAddEnglishCounterW(q, path, 0, ctypes.byref(c))
            return c if rc == 0 else None

        self._c_engine = add('\\GPU Engine(*)\\Utilization Percentage')
        self._c_mem = add('\\GPU Adapter Memory(*)\\Dedicated Usage')
        self._c_perf = add('\\Processor Information(_Total)\\% Processor Performance')
        if self._c_engine is None and self._c_mem is None and self._c_perf is None:
            self._failed = True
            pdh.PdhCloseQuery(q)
            self._query = None

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

    def _pick_target(self, mem_items):
        if self._target is not None:
            return self._target
        best = None
        best_val = -1.0
        for name, val in mem_items:
            m = _MEM_RE.search(name)
            if m and val > best_val:
                best_val = val
                best = m.group(1)
        self._target = best
        return best

    def sample(self):
        try:
            if self._failed:
                return None
            if self._query is None:
                self._setup()
            if self._query is None:
                return None
            if self._pdh.PdhCollectQueryData(self._query) != 0:
                return None
            mem_items = self._collect_array(self._c_mem)
            eng_items = self._collect_array(self._c_engine)
            perf_items = self._collect_array(self._c_perf)
            target = self._pick_target(mem_items)

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
                    'target': target, 'cpu_perf': cpu_perf}
        except Exception:
            return None

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
            if p['cpu_perf'] is not None and self._nominal:
                out['cpu_mhz'] = self._nominal * p['cpu_perf'] / 100.0
        if out['vram_used'] is not None and out['vram_total']:
            out['vram_percent'] = 100.0 * out['vram_used'] / out['vram_total']
        if self.adl is not None:
            out['gpu_mhz'] = self.adl.sample()
        return out

    def diagnostics(self):
        return [
            'cpu_backend=GetSystemTimes',
            'cpu_freq=registry~MHz(%s) x PDH %%ProcessorPerformance' % self._nominal,
            'ram_backend=GlobalMemoryStatusEx',
            'gpu_backend=PDH(GPU Engine/Adapter Memory) target=%s' % self.pdh._target,
            'gpu_name=%s vram_total=%s' % (self.gpu_name, self._vram_total),
            'gpu_clock_backend=%s' % (
                ('AMD ADL PMLog' if self.adl._pmlog else 'AMD ADL Overdrive5')
                if self.adl is not None else 'disabled'),
            'gpu_clock_error=%s' % (
                (self.adl.last_error or 'none') if self.adl is not None else 'n/a'),
        ]

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