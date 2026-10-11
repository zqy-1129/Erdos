"""Job Object 封装的失败分支（W22）：这些路径在健康机器上永远走不到，但不测就等于没写。

本机没法让 CreateJobObjectW 真失败，所以注入假 kernel32 覆盖：作业建不出来 / 限额设不上 /
绑不上子进程 / 峰值读不到 / 未创建时的 terminate 与 close。每条都断言"原因可读"——
DEC-006 的下半句是不生效时要说得出为什么。
"""

import ctypes

import pytest

from engine.sandbox import win_job
from engine.sandbox.win_job import MemoryJob


class _FakeApi:
    """按需返回失败码的 kernel32 替身。"""

    def __init__(
        self, create_handle=0, set_ok=0, open_process=0, assign_ok=0, query_ok=0, term_ok=0
    ) -> None:
        self.create_handle = create_handle
        self.set_ok = set_ok
        self.open_process = open_process
        self.assign_ok = assign_ok
        self.query_ok = query_ok
        self.term_ok = term_ok
        self.closed: list[int] = []

    def CreateJobObjectW(self, _sa, _name):
        return self.create_handle

    def SetInformationJobObject(self, _h, _cls, _info, _size):
        return self.set_ok

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def OpenProcess(self, _access, _inherit, _pid):
        return self.open_process

    def AssignProcessToJobObject(self, _job, _proc):
        return self.assign_ok

    def QueryInformationJobObject(self, _h, _cls, info, _size, _ret):
        if not self.query_ok:
            return 0
        ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
        return 1

    def TerminateJobObject(self, _h, _code):
        return self.term_ok


@pytest.fixture
def patched(monkeypatch):
    def _apply(api: _FakeApi) -> None:
        monkeypatch.setattr(win_job, "_kernel32", lambda: api)

    return _apply


def test_create_failure_reports_reason(patched) -> None:
    patched(_FakeApi(create_handle=0))
    job = MemoryJob(limit_bytes=1024)
    assert job.created is False
    assert "CreateJobObjectW" in job.reason


def test_set_information_failure_closes_handle(patched) -> None:
    api = _FakeApi(create_handle=1234, set_ok=0)
    patched(api)
    job = MemoryJob(limit_bytes=1024)
    assert job.created is False and "SetInformationJobObject" in job.reason
    assert api.closed == [1234], "限额设置失败后必须关掉句柄，不能漏句柄"


def test_assign_requires_open_process(patched) -> None:
    api = _FakeApi(create_handle=1, set_ok=1, open_process=0)
    patched(api)
    job = MemoryJob(limit_bytes=1024)
    assert job.created is True
    assert job.assign(4321) is False
    assert "OpenProcess" in job.reason


def test_assign_success_records_pid(patched) -> None:
    api = _FakeApi(create_handle=1, set_ok=1, open_process=9, assign_ok=1)
    patched(api)
    job = MemoryJob(limit_bytes=1024)
    assert job.assign(4321) is True
    assert job.assigned_pids == [4321]


def test_peak_unreadable_returns_none_not_zero(patched) -> None:
    """读失败返回 None 而不是 0：0 会被调用方读成"没用内存"，正是谎报的入口。"""
    api = _FakeApi(create_handle=1, set_ok=1)
    patched(api)
    job = MemoryJob(limit_bytes=1024)
    assert job.peak_memory_bytes() is None


def test_terminate_and_close_are_noops_when_uncreated(patched) -> None:
    api = _FakeApi(create_handle=0)
    patched(api)
    job = MemoryJob(limit_bytes=1024)
    assert job.terminate() is False
    job.close()  # 不得抛
    assert job.created is False


def test_terminate_success_and_close_clears(patched) -> None:
    api = _FakeApi(create_handle=77, set_ok=1, term_ok=1)
    patched(api)
    job = MemoryJob(limit_bytes=1024)
    assert job.terminate() is True
    job.close()
    assert job.created is False and job.handle is None
    assert 77 in api.closed


def test_non_windows_platform_is_not_enforced_but_explains(monkeypatch) -> None:
    monkeypatch.setattr(win_job, "_kernel32", lambda: None)
    job = MemoryJob(limit_bytes=1024)
    assert job.created is False
    assert "非 Windows 平台" in job.reason or "Job Object 不可用" in job.reason

