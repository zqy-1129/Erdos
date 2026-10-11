"""Windows Job Object：进程内存配额观测 + 超限主动终止（W22 · EN-MEM）。

为什么不能只用"限额标志位"：`JOB_OBJECT_LIMIT_PROCESS_MEMORY` 的语义是**后续分配失败**
（子进程拿到 MemoryError 自己崩），而 PRD §6.1 的承诺是"超限强杀"。两者的可观测差别是——
前者与模型代码真实 OOM 完全同形（exit_code≠0 + stderr 一段 traceback），归因会写错；
所以这里限额与**配额观测 + 主动终止**一起做：轮询峰值占用，越过阈值由引擎终止并标记
`memory_exceeded`，让"被沙箱按承诺强杀"与"代码自己算爆内存"是两件事。

非 Windows 平台本模块如实返回不可用（`created=False` + 原因），由调用方上报"限额未生效"，
不得把 subprocess 说成已具备资源隔离（DEC-006）。纯 stdlib ctypes，不引 psutil。
"""

import ctypes
import sys
from dataclasses import dataclass, field
from typing import Any

# JOBOBJECT 信息类别（winnt.h）
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# LimitFlags（winnt.h）
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# OpenProcess 访问权限
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerJobKernelTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> Any:
    """取 kernel32 句柄；非 Windows 返回 None（调用方据此如实上报"限额不可用"）。"""
    if sys.platform != "win32":
        return None
    # ctypes.WinDLL 只在 Windows 的类型 stub 里存在，直接引用会让 Linux CI 报属性不存在
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:
        return None
    return windll("kernel32", use_last_error=True)


TRIGGER_RATIO = 1.0  # 观测峰值达到限额本身才终止：见下方实测依据


@dataclass
class MemoryJob:
    """一次性作业对象：绑定子进程 → 轮询峰值 → 超限终止。

    生命周期与一次 `execute()` 相同（每次执行一个新作业，避免上一次任务的残留状态
    影响下一次归因）。`close()` 必须被调用：设置了 KILL_ON_JOB_CLOSE，关闭句柄即兜底
    杀掉整棵进程树（超时/异常路径共用）。
    """

    limit_bytes: int
    handle: Any = None
    created: bool = False
    reason: str = ""  # 不可用原因（created=False 时必填——DEC-006 不得静默放宽）
    assigned_pids: list[int] = field(default_factory=list)
    _api: Any = None

    def __post_init__(self) -> None:
        api = _kernel32()
        if api is None:
            self.reason = f"非 Windows 平台（{sys.platform}），Job Object 不可用"
            return
        self._api = api
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            self.reason = f"CreateJobObjectW 失败（GetLastError={ctypes.get_last_error()}）"
            return
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info.ProcessMemoryLimit = self.limit_bytes
        ok = api.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            self.reason = (
                f"SetInformationJobObject 失败（GetLastError={ctypes.get_last_error()}，"
                f"结构体长度 {ctypes.sizeof(info)}）"
            )
            api.CloseHandle(handle)
            return
        self.handle = handle
        self.created = True

    # ------------------------------------------------------------------
    def assign(self, pid: int) -> bool:
        """把已创建的子进程绑进作业（可在进程运行后绑定，无需 CREATE_SUSPENDED 重写 spawn）。"""
        if not self.created or self._api is None:
            return False
        process = self._api.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            self.reason = f"OpenProcess(pid={pid}) 失败（GetLastError={ctypes.get_last_error()}）"
            return False
        try:
            ok = self._api.AssignProcessToJobObject(self.handle, process)
        finally:
            self._api.CloseHandle(process)
        if not ok:
            self.reason = f"AssignProcessToJobObject 失败（GetLastError={ctypes.get_last_error()}）"
            return False
        self.assigned_pids.append(pid)
        return True

    def peak_memory_bytes(self) -> int | None:
        """作业内单进程峰值内存字节数；取不到返回 **None**（调用方按"无法观测"处理，不得当 0）。

        通道是 `QueryInformationJobObject(JobObjectExtendedLimitInformation)` 读回的
        `PeakProcessMemoryUsed`——本机实测可用（子进程分配 120MB → 读到 133,062,656）。
        最初用 `JobObjectBasicAccountingInformation` 的结构体猜测在这台机器上返回的是
        个位数垃圾值：**结构体布局必须实测，不能凭文档回忆**，否则观测恒为 0、
        "超限强杀"永远不会触发，而这正是本单要防的"字段已存"式假完成。
        """
        if not self.created or self._api is None:
            return None
        info = _ExtendedLimitInformation()
        ok = self._api.QueryInformationJobObject(
            self.handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        if not ok:
            return None
        return int(info.PeakProcessMemoryUsed) or int(info.PeakJobMemoryUsed) or None

    def terminate(self) -> bool:
        """立即杀掉作业内全部进程（不关闭句柄：还要读峰值与退出码）。"""
        if not self.created or self._api is None:
            return False
        return bool(self._api.TerminateJobObject(self.handle, 0))

    def close(self) -> None:
        """关闭句柄；因 KILL_ON_JOB_CLOSE，作业内残留进程一并被杀（超时/异常兜底）。"""
        if self.created and self.handle and self._api is not None:
            self._api.CloseHandle(self.handle)
        self.handle = None
        self.created = False


def new_memory_job(limit_mb: int) -> MemoryJob:
    """按 MB 建作业对象（调用方只看 `created` 判断是否真的生效）。"""
    return MemoryJob(limit_bytes=max(1, limit_mb) * 1024 * 1024)
