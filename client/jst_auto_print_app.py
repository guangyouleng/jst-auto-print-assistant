#!/usr/bin/env python3
"""聚水潭安全打单桌面助手。

单机模式在本机完成筛选、独立回读与持久任务协调，不需要部署后台服务。
聚水潭只读查询使用本机凭据；业务写入通过已登录的 Chrome/Edge 页面执行。
程序永远不点击预发货/发货。
"""

from __future__ import annotations

import argparse
import calendar
import csv
import ctypes
import getpass
import json
import math
import os
import platform
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape as xml_escape

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - packaging/runtime diagnostic
    tk = None  # type: ignore


APP_NAME = "聚水潭安全打单助手"
APP_VERSION = "0.5.25"
API_SCHEMA_VERSION = 5
PLANNER_SCHEMA_VERSION = 5
JST_HOME_URL = "https://www.erp321.com/epaas?n=打单拣货"
DEFAULT_API_URL = ""
DEFAULT_API_TOKEN = ""
TARGET_CARRIER_ID = "ZTO.1"
TARGET_CARRIER_NAME = "中通速递-山东"
TARGET_WAREHOUSE_ID = "13673110"
TARGET_WAREHOUSE_NAME = "山东汇馨仓库"
PRIVACY_CARRIER_ID = "ZTO"
PRIVACY_CARRIER_NAME = "中通-天猫-隐私面单"
SOURCE_CARRIER_ID = "STO"
SOURCE_CARRIER_NAME = "申通E物流-山东"
WEIGHT_THRESHOLD_KG = 3.0
KEEP_STO_SHOP_TOKENS = ("小红书", "红书", "视频号")
PRINT_PROFILE_CARRIERS = {
    SOURCE_CARRIER_ID: SOURCE_CARRIER_NAME,
    TARGET_CARRIER_ID: TARGET_CARRIER_NAME,
    PRIVACY_CARRIER_ID: PRIVACY_CARRIER_NAME,
}
PRINT_PROFILE_LABELS = {
    SOURCE_CARRIER_ID: "申通普通面单",
    TARGET_CARRIER_ID: "中通普通面单",
    PRIVACY_CARRIER_ID: "中通隐私面单",
}
PRINT_PROFILE_BY_LABEL = {label: key for key, label in PRINT_PROFILE_LABELS.items()}
PRINT_SERVICE_PORTS = (54323, 54325)
APP_DIR = Path.home() / ".jst-auto-print"
CONFIG_FILE = APP_DIR / "config.json"
DATABASE_FILE = APP_DIR / "events.sqlite3"
JSONL_FILE = APP_DIR / "events.jsonl"
PROFILE_ROOT = APP_DIR / "browser-profiles"
WORKSTATION_ID_FILE = APP_DIR / ".workstation-id"
INSTANCE_LOCK_FILE = APP_DIR / ".instance.lock"
DEPLOYMENT_CONFIG_NAME = "jst_operator_config.json"
LOCAL_SETTINGS_SCHEMA_VERSION = 2
JSONL_MAX_BYTES = 5 * 1024 * 1024
JSONL_BACKUP_COUNT = 3
EVENT_ROW_LIMIT = 50_000
EVENT_PRUNE_INTERVAL = 250
PLAN_MAX_AGE_SECONDS = 600
INSPECT_MAX_AGE_SECONDS = 30
FUTURE_CLOCK_SKEW_SECONDS = 30
COMPLETION_REASONS = frozenset(
    {"PRINTED", "TERMINAL", "OPERATOR_SKIPPED", "UNCERTAIN_ACTION"}
)
WORKSTATION_BINDING_MODE = "BEARER_TOKEN_DERIVED_SINGLE_WORKSTATION_V1"
_PRIVATE_PERMISSION_CACHE: set[tuple[str, bool]] = set()
_PRIVATE_PERMISSION_LOCK = threading.Lock()
DOM_LOOKUP_RETRY_DELAYS = (0.75, 1.5, 3.0, 5.0)
DOM_LOOKUP_MAX_ATTEMPTS = len(DOM_LOOKUP_RETRY_DELAYS) + 1
# After one complete DOM retry cycle, re-enter the picking page in the same
# browser tab/session and then try the exact lookup again.  This handles a
# wedged epaas iframe without creating a new browser profile or asking the
# operator to sign in again.  The recovery is bounded so a wrong warehouse or
# a genuinely changed page can never turn into an infinite navigation loop.
DOM_PAGE_RECOVERY_DELAYS = (1.5, 3.0)
DOM_PAGE_RECOVERY_MAX_ATTEMPTS = len(DOM_PAGE_RECOVERY_DELAYS)
# Backend outages are global and transient: keep the engine alive with bounded
# backoff instead of requiring an operator to press Continue after every brief
# timeout.  This backoff never repeats an ERP button; RUNNING jobs remain in
# read-only recovery until the backend is reachable again.
TRANSIENT_API_RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0, 30.0)
# The live picking page may reload up to 2,000 rows after “清空”.  Production
# traces on 2026-08-26 showed that this refresh can exceed the old 12-second
# budget.  Disconnecting at that point left the clear request racing the next
# exact search, so the late unfiltered response replaced the target row and
# surfaced as a false DOM=0.  Keep each readiness gate long enough to settle
# the real grid before issuing the next query.
GRID_IDLE_TIMEOUT_SECONDS = 35.0
GRID_SEARCH_IDLE_TIMEOUT_SECONDS = 35.0
GRID_IDLE_QUIET_SECONDS = 0.8
EXACT_ROW_TIMEOUT_SECONDS = 20.0
BATCH_EXACT_ROWS_TIMEOUT_SECONDS = 25.0
BATCH_PRINT_SIZE = 10
ACTIVE_JOB_STATUSES = ("PENDING", "PAUSED", "PREPARING", "RUNNING")
RETRYABLE_RELEASED_JOB_STATUSES = (
    "RELEASED_EXTERNAL_ORDER",
    "RELEASED_PROFILE_MISMATCH",
    "RELEASED_STATUS_CHANGED",
    "RELEASED_LEASE_LOST",
)
SKIPPABLE_PAUSE_KIND = "PAUSED_JOB_CONFLICT"
SERVER_SYNCABLE_TERMINAL_STATUSES = {
    "COMPLETED",
    "SKIPPED_SHIPPED",
    "SKIPPED_ALREADY_PRINTED",
    "SKIPPED_UNCERTAIN_PRINT",
    "SKIPPED_UNCERTAIN_WRITE",
    "SKIPPED_TERMINAL_STATUS",
    "SKIPPED_DELETED",
    "SKIPPED_OPERATOR",
}
ALLOWLISTED_TERMINAL_STATUS_KINDS = {
    "sent": "SENT",
    "delete": "DELETED",
}
SETTINGS_WARNINGS: list[str] = []
ALLOWED_PLAN_SEQUENCES = {
    ("GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"),
    ("PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"),
    (
        f"RESET_CARRIER_AND_GET_WAYBILL:{SOURCE_CARRIER_ID}:{SOURCE_CARRIER_NAME}",
        "PRINT_EXPRESS",
        "STOP_BEFORE_PRESHIP",
    ),
    (
        f"RESET_CARRIER_AND_GET_WAYBILL:{TARGET_CARRIER_ID}:{TARGET_CARRIER_NAME}",
        "PRINT_EXPRESS",
        "STOP_BEFORE_PRESHIP",
    ),
    (
        f"RESET_CARRIER_AND_GET_WAYBILL:{PRIVACY_CARRIER_ID}:{PRIVACY_CARRIER_NAME}",
        "PRINT_EXPRESS",
        "STOP_BEFORE_PRESHIP",
    ),
}
UI_SEVERITY_LABELS = {
    "INFO": "流程",
    "WARN": "提醒",
    "BLOCKED": "需处理",
    "ERROR": "错误",
}
UI_EVENT_LABELS = {
    "APP_START": "程序启动",
    "ENGINE_START": "自动检查启动",
    "FILTER_SUMMARY": "筛选结果",
    "NO_MATCHING_ORDERS": "本轮未领取到订单",
    "PLAN_SELECTED": "选中待处理单",
    "BATCH_VERIFY_START": "批量勾选核验",
    "BATCH_WAYBILL_VERIFY_START": "批量取号核验",
    "BATCH_WAYBILL_COMMIT": "确认批量取号",
    "BATCH_REBUILD": "重建安全批次",
    "BATCH_COMMIT_READBACK_RECOVERY": "批量提交后只读恢复",
    "BATCH_SEARCH_FALLBACK": "批量查单改为逐单",
    "STEP_VERIFY_START": "页面查单核对",
    "CARRIER_CHANGE_COMMIT": "确认改快递取号",
    "WAYBILL_COMMIT": "确认获取面单",
    "PRINT_COMMIT": "确认打印面单",
    "WAYBILL_OK": "取号成功",
    "CARRIER_RESET_WAYBILL_OK": "改快递并取号成功",
    "PRIVACY_CARRIER_WAYBILL_OK": "隐私面单取号成功",
    "PRINT_OK": "打印已确认",
    "COMPLETED": "本单完成",
    "AUTO_PAUSE": "自动暂停",
    "DOM_ENVIRONMENT_PAUSE": "页面接口环境暂停",
    "PLANNER_BLOCKED": "规则排除",
    "PRIVACY_REVIEW": "隐私单人工核查",
    "RELEASED_EXTERNAL_ORDER": "外部系统订单跳过",
    "SKIPPED_OPERATOR": "人工跳过",
    "SKIPPED_DELETED": "已删除单跳过",
    "SKIPPED_SHIPPED": "已发货单跳过",
    "SKIPPED_ALREADY_PRINTED": "已打印单跳过",
    "SKIPPED_TERMINAL_STATUS": "已完成订单跳过",
    "SKIPPED_UNCERTAIN_PRINT": "打印结果待人工核对",
    "SKIPPED_UNCERTAIN_WRITE": "取号结果待人工核对",
    "DOM_LOOKUP_RETRY": "等待接口订单加载",
    "RUNNING_ACTION_READBACK_RECOVERY": "已提交动作只读恢复",
    "RUNNING_DOM_READBACK_RECOVERY": "页面中断只读恢复",
    "JOB_CONFLICT_DEFERRED": "异常单延后处理",
    "RECOVERED_COMPLETED_STEP": "恢复已完成步骤",
    "REPAIRED_STALE_WAYBILL_PLAN": "修正旧取号状态",
    "TERMINAL_STATE_SYNCED": "同步已完成订单",
    "API_TRANSIENT_EXHAUSTED": "后台网络异常",
    "COMPLETION_PROOF_REQUIRED": "完成证据待核对",
    "UNKNOWN_ORDER_STATUS": "订单状态异常",
    "ENGINE_STOP": "自动检查停止",
}


class SafetyStop(RuntimeError):
    """An ambiguity or failed readback that must pause automation."""


class PermanentJobError(SafetyStop):
    """A job-specific conflict that requires an explicit operator skip."""


class ExternalSystemOrderSkipped(PermanentJobError):
    """Current operator policy excludes this order before a new action."""


class OrderRowNotReady(SafetyStop):
    """The exact order row is absent while the page may still be refreshing."""


class JSTPageMissing(OrderRowNotReady):
    """No JST epaas tab exists in the connected browser context."""


class UnknownOrderStatus(PermanentJobError):
    """A non-allowlisted order status that must never be auto-terminalized."""


class TerminalStatusSKUValidationError(PermanentJobError):
    """A Sent task with local PRINT_OK whose SKU proof is not safely recoverable."""


class OperatorPaused(RuntimeError):
    """The operator paused immediately before a business side effect."""


class OperatorStopped(RuntimeError):
    """The operator stopped immediately before a business side effect."""


class TransientAPIError(RuntimeError):
    """A bounded set of temporary network/server failures was exhausted."""


class BackendSchemaError(RuntimeError):
    """The claimed backend contract was missing or malformed."""


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the bearer credential through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LeaseLostError(SafetyStop):
    """The backend no longer recognizes this workstation's claim."""


class CompletionProofError(SafetyStop):
    """The lease is active, but the backend cannot prove safe completion."""


class ExternalShipmentDetected(RuntimeError):
    """The order was shipped by another actor while a step was being read back."""

    def __init__(self, readback: dict[str, Any]):
        super().__init__("后台已出现预发货/发货动作")
        self.readback = readback


class ExternalPrintDetected(RuntimeError):
    """Another actor printed the order before this assistant clicked print."""

    def __init__(self, readback: dict[str, Any]):
        super().__init__("后台已出现本软件未确认的打印动作")
        self.readback = readback


class ExternalTerminalStatusDetected(RuntimeError):
    """The read-only backend reports an explicitly allowlisted terminal status."""

    def __init__(self, readback: dict[str, Any]):
        super().__init__(f"后台订单状态已终态：{readback.get('status')}")
        self.readback = readback


class BatchCandidateChanged(SafetyStop):
    """One order changed during the final all-orders batch validation."""

    def __init__(self, job: dict[str, Any], reason: str):
        super().__init__(reason)
        self.job = job
        o_id = str(job.get("o_id", ""))
        io_id = str(job.get("io_id", ""))
        if o_id and io_id:
            self._jst_job_identity = (o_id, io_id)


class CommittedBatchUncertain(SafetyStop):
    """A whole batch may have clicked and must remain read-only recoverable."""

    def __init__(self, jobs: list[dict[str, Any]], reason: str):
        identities = tuple(
            (str(job.get("o_id", "")), str(job.get("io_id", "")))
            for job in jobs
        )
        self.identities = tuple(
            identity for identity in identities if identity[0] and identity[1]
        )
        summary = "、".join(f"{o_id}/{io_id}" for o_id, io_id in self.identities)
        super().__init__(
            f"{reason}；整批保持 RUNNING，只允许逐单只读恢复"
            + (f"（{summary}）" if summary else "")
        )


class BatchSearchUnsupported(SafetyStop):
    """The current JST search widget did not return one complete exact batch."""


@dataclass
class SelectedOrder:
    """Proof of the exact live grid row selected for the next browser action."""

    frame: Any
    checkbox: Optional[Any]
    target_index: str
    selection_scope: Any
    grid_kind: str = "easyui"
    o_id: str = ""
    io_id: str = ""

    def __iter__(self):
        # Preserve the historical ``frame, checkbox = select_order(...)`` shape
        # for read-only callers while retaining the identity proof internally.
        yield self.frame
        yield self.checkbox


@dataclass
class SelectedBatch:
    """Proof that the live grid selection equals one approved print batch."""

    frame: Any
    selection_scope: Any
    target_indices: tuple[str, ...]
    identities: tuple[tuple[str, str], ...]
    grid_kind: str = "easyui"


def reset_target(step: str) -> tuple[str, str]:
    prefix = "RESET_CARRIER_AND_GET_WAYBILL:"
    if not step.startswith(prefix):
        raise PermanentJobError(f"不是重设快递步骤：{step}")
    values = step[len(prefix) :].split(":", 1)
    if len(values) != 2:
        raise PermanentJobError(f"重设快递步骤格式错误：{step}")
    carrier_id, carrier_name = values
    allowed = {
        SOURCE_CARRIER_ID: SOURCE_CARRIER_NAME,
        TARGET_CARRIER_ID: TARGET_CARRIER_NAME,
        PRIVACY_CARRIER_ID: PRIVACY_CARRIER_NAME,
    }
    if allowed.get(carrier_id) != carrier_name:
        raise PermanentJobError(f"不允许的目标快递：{carrier_id}/{carrier_name}")
    return carrier_id, carrier_name


def plan_print_profile(plan: dict[str, Any]) -> str:
    """Return the one physical label-paper profile a reviewed plan will print."""

    reset_steps = [
        str(step)
        for step in (plan.get("steps") or [])
        if str(step).startswith("RESET_CARRIER_AND_GET_WAYBILL:")
    ]
    if len(reset_steps) > 1:
        raise PermanentJobError("计划包含多个目标快递，禁止进入面单批次")
    if reset_steps:
        carrier_id, carrier_name = reset_target(reset_steps[0])
    else:
        carrier_id = str(plan.get("current_carrier_id", ""))
        carrier_name = str(plan.get("current_carrier", ""))
        # Local jobs written by versions before multi-profile preservation did
        # not need these fields to determine a no-reset STO plan.
        if not carrier_id and not carrier_name:
            carrier_id, carrier_name = SOURCE_CARRIER_ID, SOURCE_CARRIER_NAME
    if PRINT_PROFILE_CARRIERS.get(carrier_id) != carrier_name:
        raise PermanentJobError("计划最终快递不属于允许的面单类型")
    return carrier_id


def describe_plan_step(step: Any) -> str:
    """Translate internal workflow tokens into operator-facing Chinese."""

    value = str(step)
    if value == "GET_WAYBILL":
        return "获取当前快递面单号"
    if value == "PRINT_EXPRESS":
        return "打印面单"
    if value == "STOP_BEFORE_PRESHIP":
        return "完成（停在预发货前）"
    if value.startswith("RESET_CARRIER_AND_GET_WAYBILL:"):
        carrier_id, _carrier_name = reset_target(value)
        return f"切换为{PRINT_PROFILE_LABELS[carrier_id]}并取号"
    return "未知步骤（已禁止执行）"


def describe_plan_steps(plan: dict[str, Any]) -> str:
    return " → ".join(
        describe_plan_step(step) for step in (plan.get("steps") or [])
    )


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _tighten_private_permissions(path: Path, *, directory: bool = False) -> None:
    """Best-effort owner-only permissions for local credentials and job state."""

    if not path.exists():
        return
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        pass
    if os.name != "nt" or not path.exists():
        return
    try:
        cache_path = str(path.resolve(strict=False))
    except OSError:
        cache_path = str(path.absolute())
    cache_key = (cache_path, bool(directory))
    with _PRIVATE_PERMISSION_LOCK:
        if cache_key in _PRIVATE_PERMISSION_CACHE:
            return
        # ACL updates are intentionally once-per-path. Re-running icacls for
        # every SQLite connection would materially slow the print loop.
        _PRIVATE_PERMISSION_CACHE.add(cache_key)
    username = str(os.environ.get("USERNAME") or getpass.getuser()).strip()
    domain = str(os.environ.get("USERDOMAIN") or "").strip()
    if not username:
        return
    identity = f"{domain}\\{username}" if domain else username
    inheritance = "(OI)(CI)F" if directory else "F"
    try:
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{identity}:{inheritance}",
                f"*S-1-5-18:{inheritance}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _is_jst_https_url(value: Any) -> bool:
    """Accept only HTTPS on the registrable JST domain or a real subdomain."""

    try:
        parsed = urlsplit(str(value or ""))
        host = str(parsed.hostname or "").rstrip(".").lower()
        # Accessing port also rejects malformed values such as ':not-a-port'.
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and (host == "erp321.com" or host.endswith(".erp321.com"))
    )


def _is_jst_frame_path(value: Any, kind: str) -> bool:
    if not _is_jst_https_url(value):
        return False
    path = urlsplit(str(value)).path.lower().rstrip("/")
    basename = path.rsplit("/", 1)[-1]
    if kind == "page":
        return path == "/epaas"
    if kind == "express":
        return bool(re.fullmatch(r"expresssetter(?:\.(?:aspx|html?))?", basename))
    if kind == "carrier":
        return basename == "selectloglstics_company.aspx"
    if kind == "confirmation":
        return basename == "epaas-dialog-frame.html"
    raise ValueError(f"不支持的聚水潭 iframe 类型：{kind}")


def _csv_safe_text(value: Any) -> Any:
    """Keep exported text inert when a CSV is opened by Excel or WPS."""

    if not isinstance(value, str):
        return value
    if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


def _is_ascii_order_id(value: Any) -> bool:
    """Match the backend's canonical JST identity contract exactly."""

    return isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value) is not None


def _natural_sort_key(value: str) -> tuple[tuple[int, object], ...]:
    """Numeric-aware sort key so 'A2' sorts before 'A10'."""

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", str(value))
    )


def load_workstation_id(path: Path = WORKSTATION_ID_FILE) -> str:
    """Return a stable, non-business workstation identity stored outside config."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _tighten_private_permissions(path.parent, directory=True)
    if path.exists():
        _tighten_private_permissions(path)
        value = path.read_text(encoding="ascii").strip()
        try:
            if not value.startswith("ws-"):
                raise ValueError
            uuid.UUID(value[3:])
        except (ValueError, AttributeError) as exc:
            raise RuntimeError("本机工作站身份文件损坏，禁止自动处理") from exc
        return value

    value = f"ws-{uuid.uuid4()}"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_workstation_id(path)
    try:
        os.write(descriptor, (value + "\n").encode("ascii"))
    finally:
        os.close(descriptor)
    _tighten_private_permissions(path)
    if os.name == "nt":  # Best effort: the containing application directory is private too.
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
        except Exception:
            pass
    return value


class SingleInstanceLock:
    """A machine-wide Windows mutex with a file-lock fallback for other systems."""

    def __init__(
        self,
        path: Path = INSTANCE_LOCK_FILE,
        mutex_names: Optional[tuple[str, ...]] = None,
    ):
        self.path = path
        self.mutex_names = mutex_names or (
            "Global\\JSTAutoPrintAssistant",
            "Global\\JSTAutoPrintAssistant_V041",
            "Global\\JSTAutoPrintAssistant_V042",
            "Global\\JSTAutoPrintAssistant_V043",
            "Global\\JSTAutoPrintAssistant_V044",
            "Global\\JSTAutoPrintAssistant_V045",
        )
        self._handles: list[Any] = []
        self._file: Any = None

    def acquire(self) -> bool:
        if self._handles or self._file is not None:
            return True
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            for mutex_name in self.mutex_names:
                handle = kernel32.CreateMutexW(None, False, mutex_name)
                if not handle:
                    self.release()
                    return False
                if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                    kernel32.CloseHandle(handle)
                    self.release()
                    return False
                self._handles.append(handle)
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _tighten_private_permissions(self.path.parent, directory=True)
        handle = self.path.open("a+b")
        _tighten_private_permissions(self.path)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError):
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        if self._handles:
            handles, self._handles = self._handles, []
            for handle in handles:
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
        if self._file is not None:
            try:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            finally:
                self._file.close()
                self._file = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("本机已有聚水潭打单助手（可能是旧版本）正在运行")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def legacy_assistant_window_exists(user32: Any = None) -> bool:
    """Detect pre-mutex assistant windows before creating this version's Tk root."""

    if user32 is None:
        if os.name != "nt":
            return False
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    found = False

    @callback_type(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def inspect_window(hwnd, _lparam):
        nonlocal found
        try:
            length = int(user32.GetWindowTextLengthW(hwnd))
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if APP_NAME in str(buffer.value):
                found = True
                return False
        except Exception:
            # Enumeration failure is handled after EnumWindows returns.
            return False
        return True

    completed = bool(user32.EnumWindows(inspect_window, 0))
    if found:
        return True
    if not completed:
        raise RuntimeError("无法检查是否仍有旧版聚水潭打单助手窗口，已安全拒绝启动")
    return False


@dataclass
class Settings:
    browser_name: str = "Chrome"
    debug_port: int = 9222
    api_url: str = DEFAULT_API_URL
    api_token: str = DEFAULT_API_TOKEN
    backend_mode: str = "auto"
    loop_seconds: int = 5
    allow_write: bool = False
    allow_print: bool = False
    print_profile: str = TARGET_CARRIER_ID
    skip_external_orders: bool = False

    @property
    def local_mode(self) -> bool:
        return self.backend_mode == "local" or (
            self.backend_mode == "auto" and not self.api_url and not self.api_token
        )

    def validate(self) -> None:
        if self.backend_mode not in {"auto", "local", "remote"}:
            raise ValueError("运行模式必须为 local、remote 或 auto")
        if type(self.skip_external_orders) is not bool:
            raise ValueError("跳过外部系统订单设置必须为布尔值")
        if self.browser_name not in {"Chrome", "Edge"}:
            raise ValueError("浏览器只支持 Chrome 或 Edge")
        if not 1024 <= int(self.debug_port) <= 65535:
            raise ValueError("调试端口必须在 1024—65535 之间")
        if not self.local_mode:
            if not re.fullmatch(
                r"https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9_./-]+", self.api_url
            ):
                raise ValueError("后台 API 地址必须是有效的 HTTPS 地址")
            if not isinstance(self.api_token, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{32,128}", self.api_token
            ):
                raise ValueError("后台 API 凭证无效")
        # JSON booleans are integers in Python and int("5") would also accept
        # a string. Keep this preference canonical so malformed local JSON
        # cannot silently alter the worker cadence.
        if type(self.loop_seconds) is not int:
            raise ValueError("轮询间隔必须是整数秒")
        if not 5 <= self.loop_seconds <= 3600:
            raise ValueError("轮询间隔必须在 5—3600 秒之间")
        if self.print_profile not in PRINT_PROFILE_CARRIERS:
            raise ValueError("必须选择申通普通、中通普通或中通隐私面单")


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    _tighten_private_permissions(APP_DIR, directory=True)
    _tighten_private_permissions(PROFILE_ROOT, directory=True)


def deployment_config_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")) / DEPLOYMENT_CONFIG_NAME
    return Path(__file__).resolve().with_name(DEPLOYMENT_CONFIG_NAME)


def load_settings() -> Settings:
    ensure_app_dirs()
    SETTINGS_WARNINGS.clear()
    deployment = deployment_config_path()
    if not deployment.exists():
        raise RuntimeError(f"部署配置不存在：{deployment}")
    # A signed macOS .app must not mutate bundled resources after signing.
    # Source runs can still tighten the local deployment file normally; the
    # build script applies mode 0600 before signing the packaged copy.
    if not getattr(sys, "frozen", False):
        _tighten_private_permissions(deployment)
    try:
        values = json.loads(deployment.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"部署配置无法读取：{deployment}（{exc}）") from exc
    if not isinstance(values, dict):
        raise RuntimeError(f"部署配置根节点必须是对象：{deployment}")

    values = dict(values)
    allowed = set(Settings.__dataclass_fields__)
    try:
        settings = Settings(
            **{key: value for key, value in values.items() if key in allowed}
        )
        settings.validate()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"部署配置无效：{exc}") from exc

    local: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        _tighten_private_permissions(CONFIG_FILE)
        try:
            parsed_local = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            SETTINGS_WARNINGS.append(f"本机偏好配置损坏，已忽略：{exc}")
        else:
            if isinstance(parsed_local, dict):
                local = parsed_local
            else:
                SETTINGS_WARNINGS.append("本机偏好配置根节点不是对象，已忽略")

    # Operators may retain browser preference, but backend coordinates are
    # controlled by the deployment package and are never exposed in UI. A bad
    # local preference never hides or replaces valid deployment credentials.
    for key in ("browser_name", "debug_port", "loop_seconds", "skip_external_orders"):
        if key not in local:
            continue
        # V0.5.19 and earlier wrote the then-default 30-second interval without
        # a preference schema.  Treat that value as a legacy default on first
        # V0.5.20 launch so existing workstations actually receive the new
        # five-second fast check.  A V2 preference remains operator-tunable.
        if (
            key == "loop_seconds"
            and local.get("preference_schema_version")
            != LOCAL_SETTINGS_SCHEMA_VERSION
            and type(local[key]) is int
            and local[key] == 30
        ):
            continue
        candidate = Settings(**asdict(settings))
        setattr(candidate, key, local[key])
        try:
            candidate.validate()
        except (TypeError, ValueError) as exc:
            SETTINGS_WARNINGS.append(f"本机偏好 {key} 无效，已忽略：{exc}")
        else:
            settings = candidate
    return settings


def save_settings(settings: Settings) -> None:
    ensure_app_dirs()
    safe = {
        "preference_schema_version": LOCAL_SETTINGS_SCHEMA_VERSION,
        "browser_name": settings.browser_name,
        "debug_port": settings.debug_port,
        "loop_seconds": settings.loop_seconds,
        "skip_external_orders": settings.skip_external_orders,
    }
    CONFIG_FILE.write_text(
        json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _tighten_private_permissions(CONFIG_FILE)


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_export_date(value: Any) -> date:
    """Require one unambiguous local calendar date for SKU statistics."""

    if not isinstance(value, str):
        raise ValueError("导出日期必须使用 YYYY-MM-DD 格式")
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("导出日期无效，请使用 YYYY-MM-DD 格式") from exc
    if parsed.isoformat() != text:
        raise ValueError("导出日期无效，请使用 YYYY-MM-DD 格式")
    return parsed


def calendar_month_dates(year: int, month: int) -> list[list[date]]:
    """Return a Monday-first calendar grid for the desktop date picker."""

    if type(year) is not int or type(month) is not int:
        raise ValueError("日期选择器年月格式无效")
    if not 1 <= year <= 9999 or not 1 <= month <= 12:
        raise ValueError("日期选择器年月超出范围")
    return calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(
        year, month
    )


def shift_calendar_month(year: int, month: int, offset: int) -> tuple[int, int]:
    """Move a calendar month without relying on locale-specific parsing."""

    if type(year) is not int or type(month) is not int or type(offset) is not int:
        raise ValueError("日期选择器月份参数无效")
    if not 1 <= year <= 9999 or not 1 <= month <= 12:
        raise ValueError("日期选择器年月超出范围")
    absolute = year * 12 + month - 1 + offset
    shifted_year, shifted_month = divmod(absolute, 12)
    if not 1 <= shifted_year <= 9999:
        raise ValueError("日期选择器月份超出支持范围")
    return shifted_year, shifted_month + 1


class EventStore:
    def __init__(self, db_path: Path = DATABASE_FILE, jsonl_path: Path = JSONL_FILE):
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        self.lock = threading.Lock()
        self._event_writes = 0
        self._jsonl_degraded = False
        self.startup_notices: list[dict[str, Any]] = []
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _tighten_private_permissions(self.db_path.parent, directory=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        _tighten_private_permissions(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _primary_key_columns(connection: sqlite3.Connection, table: str) -> list[str]:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return [str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if row[5]]

    @staticmethod
    def _has_unique_index(
        connection: sqlite3.Connection, table: str, columns: tuple[str, ...]
    ) -> bool:
        for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
            if not bool(index[2]):
                continue
            names = tuple(
                str(row[2])
                for row in connection.execute(f"PRAGMA index_info({index[1]})").fetchall()
            )
            if names == columns:
                return True
        return False

    @staticmethod
    def _create_jobs_table(connection: sqlite3.Connection, name: str = "jobs") -> None:
        connection.execute(
            f"""CREATE TABLE {name} (
                o_id TEXT NOT NULL,
                io_id TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                step_index INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                pause_kind TEXT NOT NULL DEFAULT '',
                pause_reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(o_id, io_id)
            )"""
        )

    @staticmethod
    def _create_sku_table(
        connection: sqlite3.Connection, name: str = "sku_outbound"
    ) -> None:
        connection.execute(
            f"""CREATE TABLE {name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                o_id TEXT NOT NULL,
                io_id TEXT NOT NULL DEFAULT '',
                line_key TEXT NOT NULL,
                sku_id TEXT NOT NULL DEFAULT '',
                sku_name TEXT NOT NULL,
                qty REAL NOT NULL,
                unit TEXT NOT NULL DEFAULT '',
                shop_name TEXT NOT NULL DEFAULT '',
                io_date TEXT NOT NULL DEFAULT '',
                carrier_name TEXT NOT NULL DEFAULT '',
                privacy_required INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL,
                UNIQUE(o_id, io_id, line_key)
            )"""
        )

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    o_id TEXT NOT NULL DEFAULT '',
                    io_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS excluded_jobs (
                    o_id TEXT NOT NULL,
                    io_id TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    excluded_at TEXT NOT NULL,
                    PRIMARY KEY(o_id, io_id)
                )"""
            )
            conn.execute("DROP TRIGGER IF EXISTS prevent_terminal_job_reactivation")
            conn.execute("DROP TRIGGER IF EXISTS prevent_terminal_job_reactivation_v2")
            conn.execute("DROP TRIGGER IF EXISTS prevent_terminal_job_reactivation_v3")
            conn.execute("DROP TRIGGER IF EXISTS prevent_terminal_job_reactivation_v4")

            if not self._table_exists(conn, "jobs"):
                self._create_jobs_table(conn)
            elif self._primary_key_columns(conn, "jobs") != ["o_id", "io_id"]:
                conn.execute("DROP TABLE IF EXISTS jobs_v3_migration")
                self._create_jobs_table(conn, "jobs_v3_migration")
                conn.execute(
                    """INSERT OR IGNORE INTO jobs_v3_migration
                    (o_id, io_id, plan_json, step_index, status, updated_at)
                    SELECT CAST(o_id AS TEXT), CAST(io_id AS TEXT), plan_json,
                           step_index,
                           CASE
                             WHEN CAST(io_id AS TEXT) <> ''
                              AND CAST(io_id AS TEXT) NOT GLOB '*[^0-9]*'
                             THEN status
                             ELSE 'SKIPPED_INVALID_LEGACY_ID'
                           END,
                           updated_at FROM jobs"""
                )
                conn.execute("DROP TABLE jobs")
                conn.execute("ALTER TABLE jobs_v3_migration RENAME TO jobs")

            job_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "pause_kind" not in job_columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN pause_kind TEXT NOT NULL DEFAULT ''"
                )
            if "pause_reason" not in job_columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN pause_reason TEXT NOT NULL DEFAULT ''"
                )

            # A pre-release composite schema could still contain an empty or
            # non-numeric legacy io_id. Never leave such a row at the resumable
            # queue head: its browser identity cannot be proven safely.
            active_placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
            conn.execute(
                f"""UPDATE jobs SET status='SKIPPED_INVALID_LEGACY_ID', updated_at=?
                WHERE status IN ({active_placeholders})
                  AND (io_id='' OR io_id GLOB '*[^0-9]*')""",
                (now_text(), *ACTIVE_JOB_STATUSES),
            )

            # V0.4.1—V0.4.4 persisted PAUSED jobs have no claimed-V1 lease.
            # Retire them during migration so a new binary never performs a
            # browser action from stale local state or blocks the queue head.
            legacy_rows = conn.execute(
                f"""SELECT o_id, io_id, plan_json, step_index FROM jobs
                WHERE status IN ({active_placeholders})""",
                ACTIVE_JOB_STATUSES,
            ).fetchall()
            for row in legacy_rows:
                reason = ""
                try:
                    plan = json.loads(str(row[2]))
                    steps = plan.get("steps") if isinstance(plan, dict) else None
                    token = plan.get("claim_token") if isinstance(plan, dict) else None
                    if (
                        not isinstance(plan, dict)
                        or str(plan.get("o_id", "")) != str(row[0])
                        or str(plan.get("io_id", "")) != str(row[1])
                        or not _is_ascii_order_id(str(row[0]))
                        or not _is_ascii_order_id(str(row[1]))
                        or not isinstance(token, str)
                        or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None
                        or not isinstance(steps, list)
                        or tuple(steps) not in ALLOWED_PLAN_SEQUENCES
                        or plan.get("outbound_identity_unique") is not True
                    ):
                        reason = "旧任务缺少 claimed V1 复合身份、租约或批准步骤"
                except (TypeError, ValueError, json.JSONDecodeError):
                    reason = "旧任务计划数据无法解析"
                if not reason:
                    continue
                created_at = now_text()
                conn.execute(
                    """UPDATE jobs SET status='SKIPPED_LEGACY_UNCLAIMED',
                       pause_kind='', pause_reason='', updated_at=?
                       WHERE o_id=? AND io_id=?""",
                    (created_at, str(row[0]), str(row[1])),
                )
                message = reason + "；已安全终态迁移，未执行任何浏览器动作"
                conn.execute(
                    """INSERT INTO events
                    (created_at, severity, event_type, o_id, io_id, message, detail_json)
                    VALUES (?, 'WARN', 'MIGRATED_LEGACY_UNCLAIMED', ?, ?, ?, '{}')""",
                    (created_at, str(row[0]), str(row[1]), message),
                )
                self.startup_notices.append(
                    {
                        "created_at": created_at,
                        "severity": "WARN",
                        "event_type": "MIGRATED_LEGACY_UNCLAIMED",
                        "o_id": str(row[0]),
                        "io_id": str(row[1]),
                        "message": message,
                    }
                )

            if not self._table_exists(conn, "sku_outbound"):
                self._create_sku_table(conn)
            elif not self._has_unique_index(
                conn, "sku_outbound", ("o_id", "io_id", "line_key")
            ):
                conn.execute("DROP TABLE IF EXISTS sku_outbound_v3_migration")
                self._create_sku_table(conn, "sku_outbound_v3_migration")
                conn.execute(
                    """INSERT OR IGNORE INTO sku_outbound_v3_migration
                    (id, o_id, io_id, line_key, sku_id, sku_name, qty, unit,
                     shop_name, io_date, carrier_name, privacy_required, recorded_at)
                    SELECT id, CAST(o_id AS TEXT), CAST(io_id AS TEXT), line_key,
                           sku_id, sku_name, qty, unit, shop_name, io_date,
                           carrier_name, privacy_required, recorded_at
                    FROM sku_outbound"""
                )
                conn.execute("DROP TABLE sku_outbound")
                conn.execute(
                    "ALTER TABLE sku_outbound_v3_migration RENAME TO sku_outbound"
                )

            conn.execute(
                """CREATE TRIGGER prevent_terminal_job_reactivation_v4
                BEFORE UPDATE OF status ON jobs
                WHEN OLD.status NOT IN (
                    'PENDING', 'PAUSED', 'PREPARING', 'RUNNING',
                    'RELEASED_EXTERNAL_ORDER', 'RELEASED_PROFILE_MISMATCH', 'RELEASED_STATUS_CHANGED',
                    'RELEASED_LEASE_LOST'
                )
                  AND NEW.status IN ('PENDING', 'PAUSED', 'PREPARING', 'RUNNING')
                BEGIN
                    SELECT RAISE(IGNORE);
                END
                """
            )
            conn.execute(
                """DELETE FROM events WHERE id IN (
                    SELECT id FROM events ORDER BY id DESC LIMIT -1 OFFSET ?
                )""",
                (EVENT_ROW_LIMIT,),
            )
            conn.commit()

    def _rotate_jsonl_if_needed(self) -> None:
        if not self.jsonl_path.exists() or self.jsonl_path.stat().st_size < JSONL_MAX_BYTES:
            return
        oldest = self.jsonl_path.with_name(
            f"{self.jsonl_path.name}.{JSONL_BACKUP_COUNT}"
        )
        if oldest.exists():
            oldest.unlink()
        for index in range(JSONL_BACKUP_COUNT - 1, 0, -1):
            source = self.jsonl_path.with_name(f"{self.jsonl_path.name}.{index}")
            if source.exists():
                source.replace(
                    self.jsonl_path.with_name(f"{self.jsonl_path.name}.{index + 1}")
                )
        self.jsonl_path.replace(self.jsonl_path.with_name(f"{self.jsonl_path.name}.1"))
        for index in range(1, JSONL_BACKUP_COUNT + 1):
            backup = self.jsonl_path.with_name(f"{self.jsonl_path.name}.{index}")
            if backup.exists():
                _tighten_private_permissions(backup)

    def _record_jsonl_failure(self, message: str) -> None:
        if self._jsonl_degraded:
            return
        self._jsonl_degraded = True
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO events
                (created_at, severity, event_type, o_id, io_id, message, detail_json)
                VALUES (?, 'WARN', 'JSONL_WRITE_FAILED', '', '', ?, '{}')""",
                (now_text(), f"JSONL 日志写入失败，数据库记录仍有效：{message}"),
            )

    def event(
        self,
        severity: str,
        event_type: str,
        message: str,
        *,
        o_id: str = "",
        io_id: str = "",
        detail: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        item = {
            "created_at": now_text(),
            "severity": severity,
            "event_type": event_type,
            "o_id": str(o_id or ""),
            "io_id": str(io_id or ""),
            "message": message,
            "detail": detail or {},
        }
        detail_json = json.dumps(item["detail"], ensure_ascii=False)
        line = json.dumps(item, ensure_ascii=False)
        with self.lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO events
                    (created_at, severity, event_type, o_id, io_id, message, detail_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item["created_at"],
                        severity,
                        event_type,
                        item["o_id"],
                        item["io_id"],
                        message,
                        detail_json,
                    ),
                )
                self._event_writes += 1
                if self._event_writes % EVENT_PRUNE_INTERVAL == 0:
                    conn.execute(
                        """DELETE FROM events WHERE id IN (
                            SELECT id FROM events ORDER BY id DESC LIMIT -1 OFFSET ?
                        )""",
                        (EVENT_ROW_LIMIT,),
                    )
            try:
                self._rotate_jsonl_if_needed()
                with self.jsonl_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                _tighten_private_permissions(self.jsonl_path)
                self._jsonl_degraded = False
            except (OSError, UnicodeError) as exc:
                self._record_jsonl_failure(str(exc))
        return item

    def save_job(self, plan: dict[str, Any]) -> bool:
        o_id = str(plan.get("o_id", ""))
        io_id = str(plan.get("io_id", ""))
        if not _is_ascii_order_id(o_id) or not _is_ascii_order_id(io_id):
            raise ValueError("任务必须同时包含内部订单号和出库单号")
        with self.lock, self._connect() as conn:
            retryable_placeholders = ",".join(
                "?" for _ in RETRYABLE_RELEASED_JOB_STATUSES
            )
            cursor = conn.execute(
                """INSERT INTO jobs
                (o_id, io_id, plan_json, step_index, status, updated_at)
                VALUES (?, ?, ?, 0, 'PENDING', ?)
                ON CONFLICT(o_id, io_id) DO UPDATE SET
                    plan_json=excluded.plan_json,
                    step_index=0,
                    status='PENDING',
                    pause_kind='',
                    pause_reason='',
                    updated_at=excluded.updated_at
                WHERE jobs.status IN ("""
                + retryable_placeholders
                + ")",
                (
                    o_id,
                    io_id,
                    json.dumps(plan, ensure_ascii=False),
                    now_text(),
                    *RETRYABLE_RELEASED_JOB_STATUSES,
                ),
            )
        return cursor.rowcount > 0

    def next_job(self) -> Optional[dict[str, Any]]:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM jobs
                WHERE status IN ('PENDING', 'PAUSED', 'PREPARING', 'RUNNING')
                ORDER BY updated_at ASC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        return result

    def active_jobs(self, limit: int = 200) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("活动任务查询数量无效")
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM jobs
                WHERE status IN ('PENDING', 'PAUSED', 'PREPARING', 'RUNNING')
                ORDER BY updated_at ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["plan"] = json.loads(item.pop("plan_json"))
            result.append(item)
        return result

    def has_runnable_job_except(self, o_id: str, io_id: str) -> bool:
        """Return whether another already-claimed job can safely keep moving."""

        with self.lock, self._connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM jobs
                WHERE status IN ('PENDING', 'PREPARING', 'RUNNING')
                  AND NOT (o_id=? AND io_id=?)
                LIMIT 1""",
                (str(o_id), str(io_id)),
            ).fetchone()
        return row is not None

    def latest_paused_job(self) -> Optional[dict[str, Any]]:
        """Return the actual most-recent pause, never an older pending job."""

        with self.lock, self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM jobs
                WHERE status='PAUSED'
                ORDER BY updated_at DESC, rowid DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        return result

    def latest_problem_job(self) -> Optional[dict[str, Any]]:
        """Find the latest exact anomaly, including submitted RUNNING actions."""

        with self.lock, self._connect() as conn:
            row = conn.execute(
                """SELECT jobs.*,
                   (SELECT message FROM events
                    WHERE events.o_id=jobs.o_id AND events.io_id=jobs.io_id
                      AND severity IN ('BLOCKED', 'ERROR')
                    ORDER BY id DESC LIMIT 1) AS latest_error
                   FROM jobs
                   WHERE status IN ('PENDING', 'PAUSED', 'PREPARING', 'RUNNING')
                     AND (status IN ('PAUSED', 'RUNNING') OR EXISTS (
                         SELECT 1 FROM events
                         WHERE events.o_id=jobs.o_id AND events.io_id=jobs.io_id
                           AND severity IN ('BLOCKED', 'ERROR')))
                   ORDER BY (SELECT MAX(id) FROM events
                             WHERE events.o_id=jobs.o_id AND events.io_id=jobs.io_id
                               AND severity IN ('BLOCKED', 'ERROR')) DESC,
                            updated_at DESC, jobs.rowid DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        latest_error = result.pop("latest_error")
        result["pause_reason"] = result.get("pause_reason") or latest_error or "人工强制跳过"
        return result

    def mark_batch_running(self, jobs: list[dict[str, Any]]) -> bool:
        """Atomically move the exact pending batch to RUNNING before one click."""

        identities = [
            (str(job.get("o_id", "")), str(job.get("io_id", ""))) for job in jobs
        ]
        if not 2 <= len(identities) <= BATCH_PRINT_SIZE or len(set(identities)) != len(
            identities
        ):
            return False
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = 0
            stamp = now_text()
            for o_id, io_id in identities:
                cursor = conn.execute(
                    """UPDATE jobs SET status='RUNNING', pause_kind='',
                       pause_reason='', updated_at=?
                       WHERE o_id=? AND io_id=? AND status='PREPARING'""",
                    (stamp, o_id, io_id),
                )
                changed += cursor.rowcount
            if changed != len(identities):
                conn.rollback()
                return False
            conn.commit()
        return True

    def get_job(self, o_id: str, io_id: str) -> Optional[dict[str, Any]]:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE o_id=? AND io_id=?", (str(o_id), str(io_id))
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        return result

    def update_job(
        self,
        o_id: str,
        io_id: str,
        *,
        step_index: int,
        status: str,
        pause_kind: str = "",
        pause_reason: str = "",
    ) -> bool:
        if status != "PAUSED":
            pause_kind = ""
            pause_reason = ""
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                """UPDATE jobs SET step_index=?, status=?, pause_kind=?,
                   pause_reason=?, updated_at=?
                WHERE o_id=? AND io_id=?""",
                (
                    step_index,
                    status,
                    str(pause_kind),
                    str(pause_reason),
                    now_text(),
                    str(o_id),
                    str(io_id),
                ),
            )
        return cursor.rowcount > 0

    def replace_active_job_plan(
        self,
        o_id: str,
        io_id: str,
        plan: dict[str, Any],
        *,
        step_index: int = 0,
    ) -> bool:
        """Replace a claimed active plan without changing its exact identity."""

        if str(plan.get("o_id", "")) != str(o_id) or str(plan.get("io_id", "")) != str(io_id):
            raise ValueError("替换任务计划的复合身份不一致")
        with self.lock, self._connect() as conn:
            placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
            cursor = conn.execute(
                f"""UPDATE jobs SET plan_json=?, step_index=?, status='PENDING',
                       pause_kind='', pause_reason='', updated_at=?
                    WHERE o_id=? AND io_id=? AND status IN ({placeholders})""",
                (
                    json.dumps(plan, ensure_ascii=False),
                    int(step_index),
                    now_text(),
                    str(o_id),
                    str(io_id),
                    *ACTIVE_JOB_STATUSES,
                ),
            )
        return cursor.rowcount > 0

    def exclude_job(self, o_id: str, io_id: str, reason: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO excluded_jobs (o_id, io_id, reason, excluded_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(o_id, io_id) DO UPDATE SET
                    reason=excluded.reason, excluded_at=excluded.excluded_at""",
                (str(o_id), str(io_id), str(reason), now_text()),
            )

    def excluded_pairs(self) -> list[dict[str, str]]:
        retryable_statuses = ACTIVE_JOB_STATUSES + RETRYABLE_RELEASED_JOB_STATUSES
        retryable_placeholders = ",".join("?" for _ in retryable_statuses)
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                f"""SELECT o_id, io_id, MAX(seen_at) AS last_seen FROM (
                    SELECT o_id, io_id, updated_at AS seen_at
                    FROM jobs WHERE status NOT IN ({retryable_placeholders})
                    UNION ALL
                    SELECT o_id, io_id, excluded_at AS seen_at FROM excluded_jobs
                )
                GROUP BY o_id, io_id
                ORDER BY last_seen DESC
                LIMIT 400""",
                retryable_statuses,
            ).fetchall()
        result: list[dict[str, str]] = []
        for row in rows:
            o_id, io_id = str(row[0]), str(row[1])
            if _is_ascii_order_id(o_id) and _is_ascii_order_id(io_id):
                result.append({"o_id": o_id, "io_id": io_id})
            if len(result) >= 200:
                break
        return result

    def recent_events(self, limit: int = 300) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def has_event(self, event_type: str, o_id: str, io_id: str) -> bool:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM events
                WHERE event_type=? AND o_id=? AND io_id=? LIMIT 1""",
                (str(event_type), str(o_id), str(io_id)),
            ).fetchone()
        return row is not None

    def latest_event_detail(
        self, event_type: str, o_id: str, io_id: str
    ) -> Optional[dict[str, Any]]:
        """Return the newest durable event detail for one exact order pair."""

        with self.lock, self._connect() as conn:
            row = conn.execute(
                """SELECT detail_json FROM events
                WHERE event_type=? AND o_id=? AND io_id=?
                ORDER BY id DESC LIMIT 1""",
                (str(event_type), str(o_id), str(io_id)),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row[0]))
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def export_anomalies(self, target: Path) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT created_at, severity, event_type, o_id, io_id, message
                FROM events WHERE severity IN ('WARN', 'ERROR', 'BLOCKED')
                ORDER BY id ASC"""
            ).fetchall()
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["时间", "级别", "类型", "内部订单号", "出库单号", "说明"])
            writer.writerows(
                [_csv_safe_text(value) for value in row] for row in rows
            )
        _tighten_private_permissions(target)
        return len(rows)

    def record_sku_outbound(self, readback: dict[str, Any]) -> int:
        items = readback.get("items")
        if readback.get("items_complete") is not True:
            raise PermanentJobError("后台未确认 SKU 明细完整，禁止记录")
        if readback.get("item_validation_errors") != []:
            raise PermanentJobError("后台 SKU 明细校验存在异常，禁止记录")
        if readback.get("order_validation_errors") != []:
            raise PermanentJobError("后台订单校验存在异常，禁止记录")
        if not isinstance(items, list) or not items:
            raise PermanentJobError("订单打印完成，但后台未返回 SKU 明细，禁止漏记")
        source_item_count = readback.get("source_item_count")
        if (
            isinstance(source_item_count, bool)
            or not isinstance(source_item_count, int)
            or source_item_count != len(items)
        ):
            raise PermanentJobError("后台 SKU 来源行数与明细不一致，禁止记录")
        o_id = str(readback.get("o_id", ""))
        io_id = str(readback.get("io_id", ""))
        if not _is_ascii_order_id(o_id) or not _is_ascii_order_id(io_id):
            raise PermanentJobError("SKU 明细缺少内部订单号或出库单号，禁止记录")
        if type(readback.get("privacy_required")) is not bool:
            raise PermanentJobError("SKU 明细缺少明确的隐私面单布尔标记")
        for field in ("shop_name", "io_date", "carrier_name"):
            if not isinstance(readback.get(field), str):
                raise PermanentJobError(f"SKU 明细字段 {field} 类型错误")

        shared = (
            str(readback.get("shop_name", "")),
            str(readback.get("io_date", "")),
            str(readback.get("carrier_name", "")),
            1 if readback.get("privacy_required") is True else 0,
        )
        validated: list[tuple[str, str, str, float, str]] = []
        line_keys: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise PermanentJobError("后台 SKU 明细条目格式错误，禁止记录")
            line_key = item.get("line_key")
            sku_id = item.get("sku_id")
            sku_name = item.get("sku_name")
            unit = item.get("unit")
            if (
                not isinstance(line_key, str)
                or not line_key
                or line_key in line_keys
                or not isinstance(sku_id, str)
                or not isinstance(sku_name, str)
                or not sku_name
                or not isinstance(unit, str)
            ):
                raise PermanentJobError("后台 SKU 行身份重复、名称为空或字段类型错误")
            qty = item.get("qty")
            if (
                isinstance(qty, bool)
                or not isinstance(qty, (int, float))
                or not math.isfinite(float(qty))
                or float(qty) <= 0
            ):
                raise PermanentJobError("后台 SKU 数量无效，禁止记录")
            line_keys.add(line_key)
            validated.append((line_key, sku_id, sku_name, float(qty), unit))

        inserted = 0
        recorded_at = now_text()
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_rows = conn.execute(
                """SELECT line_key, sku_id, sku_name, qty, unit, shop_name,
                          io_date, carrier_name, privacy_required
                   FROM sku_outbound WHERE o_id=? AND io_id=?""",
                (o_id, io_id),
            ).fetchall()
            existing = {str(row[0]): row for row in existing_rows}
            validated_keys = {item[0] for item in validated}
            if existing and set(existing) != validated_keys:
                raise PermanentJobError(
                    "同一订单既有 SKU 行集合与当前完整回读不一致，禁止部分保留或补写"
                )

            # Validate the entire batch, including conflicts with prior data,
            # before inserting even one row. This prevents partial SKU records.
            for line_key, sku_id, sku_name, qty, unit in validated:
                row = existing.get(line_key)
                if row is None:
                    continue
                same = (
                    str(row[1]) == sku_id
                    and str(row[2]) == sku_name
                    and math.isclose(float(row[3]), qty, rel_tol=0.0, abs_tol=1e-9)
                    and str(row[4]) == unit
                    and str(row[5]) == shared[0]
                    and str(row[6]) == shared[1]
                    and str(row[7]) == shared[2]
                    and int(row[8]) == shared[3]
                )
                if not same:
                    raise PermanentJobError(
                        f"同一订单 SKU 行 {line_key} 与既有记录冲突，禁止覆盖"
                    )

            for line_key, sku_id, sku_name, qty, unit in validated:
                if line_key in existing:
                    continue
                cursor = conn.execute(
                    """INSERT INTO sku_outbound
                    (o_id, io_id, line_key, sku_id, sku_name, qty, unit,
                     shop_name, io_date, carrier_name, privacy_required, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        o_id,
                        io_id,
                        line_key,
                        sku_id,
                        sku_name,
                        qty,
                        unit,
                        *shared,
                        recorded_at,
                    ),
                )
                inserted += max(cursor.rowcount, 0)
        return inserted

    def sku_outbound_rows(self, selected_date: Optional[str] = None) -> list[dict[str, Any]]:
        parameters: tuple[str, ...] = ()
        where = ""
        if selected_date is not None:
            parsed = parse_export_date(selected_date)
            start = parsed.isoformat() + "T00:00:00"
            end = (parsed + timedelta(days=1)).isoformat() + "T00:00:00"
            where = "WHERE recorded_at>=? AND recorded_at<?"
            parameters = (start, end)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT recorded_at, io_date, o_id, io_id, sku_id, sku_name,
                          qty, unit, shop_name, carrier_name, privacy_required
                   FROM sku_outbound {where}
                   ORDER BY recorded_at ASC, o_id ASC, id ASC""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def export_sku_outbound_xlsx(
        self, target: Path, selected_date: str
    ) -> tuple[int, int]:
        normalized_date = parse_export_date(selected_date).isoformat()
        rows = self.sku_outbound_rows(normalized_date)
        if not rows:
            raise ValueError(
                f"{normalized_date} 没有本软件确认打印完成的 SKU 出库记录"
            )
        write_sku_outbound_xlsx(rows, target, selected_date=normalized_date)
        order_count = len(
            {(str(row["o_id"]), str(row["io_id"])) for row in rows}
        )
        return len(rows), order_count


def _xlsx_col(number: int) -> str:
    value = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _xlsx_cell(
    row: int,
    column: int,
    value: Any = None,
    *,
    style: int = 0,
    formula: str = "",
) -> str:
    reference = f"{_xlsx_col(column)}{row}"
    style_attr = f' s="{style}"' if style else ""
    if formula:
        cached = ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cached = f"<v>{value}</v>"
        return (
            f'<c r="{reference}"{style_attr}>'
            f"<f>{xml_escape(formula)}</f>{cached}</c>"
        )
    if value is None:
        return f'<c r="{reference}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{reference}"{style_attr} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}"{style_attr}><v>{value}</v></c>'
    text_value = xml_escape(str(value))
    return (
        f'<c r="{reference}"{style_attr} t="inlineStr">'
        f'<is><t xml:space="preserve">{text_value}</t></is></c>'
    )


def _xlsx_sheet(
    rows: list[list[tuple[Any, int, str]]],
    *,
    widths: list[float],
    merged_title_to: str,
    autofilter: str,
    freeze_rows: int = 4,
) -> str:
    row_xml = []
    max_column = 1
    for row_number, cells in enumerate(rows, start=1):
        max_column = max(max_column, len(cells))
        rendered = []
        for column, (value, style, formula) in enumerate(cells, start=1):
            rendered.append(
                _xlsx_cell(
                    row_number,
                    column,
                    value,
                    style=style,
                    formula=formula,
                )
            )
        height = ' ht="30" customHeight="1"' if row_number == 1 else ""
        row_xml.append(f'<row r="{row_number}"{height}>{"".join(rendered)}</row>')
    last_row = max(len(rows), 1)
    cols = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    pane = (
        f'<pane ySplit="{freeze_rows}" topLeftCell="A{freeze_rows + 1}" '
        'activePane="bottomLeft" state="frozen"/>'
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:{_xlsx_col(max_column)}{last_row}"/>
  <sheetViews><sheetView workbookViewId="0">{pane}</sheetView></sheetViews>
  <sheetFormatPr defaultRowHeight="18"/>
  <cols>{cols}</cols>
  <sheetData>{''.join(row_xml)}</sheetData>
  <mergeCells count="1"><mergeCell ref="A1:{merged_title_to}1"/></mergeCells>
  <autoFilter ref="{autofilter}"/>
  <pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>
</worksheet>'''


def write_sku_outbound_xlsx(
    rows: list[dict[str, Any]], target: Path, *, selected_date: str = ""
) -> None:
    """Create a dependency-free XLSX that opens in Microsoft Excel and WPS."""
    # 商品名称不是唯一标识：聚水潭中不同商品可能有相同名称。按“编码 + 名称”
    # 汇总，避免把不同 SKU 合并到同一行并将多个编码拼接展示。
    grouped: dict[tuple[str, str], dict[str, set[str]]] = {}
    totals: dict[tuple[str, str], float] = {}
    line_counts: dict[tuple[str, str], int] = {}
    for item in rows:
        name = str(item["sku_name"])
        code = str(item.get("sku_id", ""))
        sku_key = (code, name)
        group = grouped.setdefault(sku_key, {"units": set(), "orders": set()})
        if item.get("unit"):
            group["units"].add(str(item["unit"]))
        group["orders"].add(f"{item['o_id']}\x1f{item['io_id']}")
        totals[sku_key] = totals.get(sku_key, 0.0) + float(item.get("qty") or 0)
        line_counts[sku_key] = line_counts.get(sku_key, 0) + 1

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_note = f"打印完成日期：{selected_date}" if selected_date else "打印完成日期：全部"
    detail_headers = [
        "统计时间",
        "出库单日期",
        "内部订单号",
        "出库单号",
        "SKU编码",
        "SKU名称",
        "出库数量",
        "单位",
        "店铺",
        "快递",
        "隐私面单",
    ]
    detail_rows: list[list[tuple[Any, int, str]]] = [
        [("SKU出库明细", 1, "")] + [(None, 1, "")] * 10,
        [
            (f"导出时间：{generated}", 4, ""),
            (date_note, 4, ""),
            (f"明细行数：{len(rows)}", 4, ""),
        ]
        + [(None, 0, "")] * 8,
        [("统计口径：仅包含所选日期内本软件确认打印成功并完成后台回读的订单；按订单号+明细行去重。", 0, "")]
        + [(None, 0, "")] * 10,
        [(header, 2, "") for header in detail_headers],
    ]
    for item in rows:
        detail_rows.append(
            [
                (item.get("recorded_at", ""), 0, ""),
                (item.get("io_date", ""), 0, ""),
                (str(item.get("o_id", "")), 0, ""),
                (str(item.get("io_id", "")), 0, ""),
                (str(item.get("sku_id", "")), 0, ""),
                (str(item.get("sku_name", "")), 0, ""),
                (float(item.get("qty") or 0), 3, ""),
                (str(item.get("unit", "")), 0, ""),
                (str(item.get("shop_name", "")), 0, ""),
                (str(item.get("carrier_name", "")), 0, ""),
                ("是" if item.get("privacy_required") else "否", 0, ""),
            ]
        )

    detail_end = len(detail_rows)
    summary_headers = ["SKU名称", "SKU编码", "出库数量", "明细行数", "订单数", "单位"]
    summary_rows: list[list[tuple[Any, int, str]]] = [
        [("SKU出库数量汇总", 1, "")] + [(None, 1, "")] * 5,
        [
            (f"导出时间：{generated}", 4, ""),
            (date_note, 4, ""),
            (
                f"订单数：{len({(str(row['o_id']), str(row['io_id'])) for row in rows})}",
                4,
                "",
            ),
            (f"SKU条目数：{len(grouped)}", 4, ""),
        ]
        + [(None, 0, "")] * 2,
        [("按SKU编码和名称精确汇总（区分大小写，保留前导零和特殊符号）；数量引用“出库明细”。", 0, "")]
        + [(None, 0, "")] * 5,
        [(header, 2, "") for header in summary_headers],
    ]
    ordered_skus = sorted(grouped, key=lambda sku_key: (-totals[sku_key], sku_key))
    for row_number, (code, name) in enumerate(ordered_skus, start=5):
        sku_key = (code, name)
        group = grouped[sku_key]
        # EXACT matches Python's text keys: no wildcard/operator interpretation,
        # numeric coercion or case folding. SUMPRODUCT needs no array entry.
        exact_match = (
            f"--EXACT('出库明细'!$F$5:$F${detail_end},A{row_number}),"
            f"--EXACT('出库明细'!$E$5:$E${detail_end},B{row_number})"
        )
        summary_rows.append(
            [
                (name, 0, ""),
                (code, 0, ""),
                (
                    totals[sku_key],
                    3,
                    f"SUMPRODUCT({exact_match},'出库明细'!$G$5:$G${detail_end})",
                ),
                (
                    line_counts[sku_key],
                    0,
                    f"SUMPRODUCT({exact_match})",
                ),
                (len(group["orders"]), 0, ""),
                ("、".join(sorted(group["units"])), 0, ""),
            ]
        )
    total_row = len(summary_rows) + 1
    summary_rows.append(
        [
            ("合计", 4, ""),
            (None, 4, ""),
            (sum(totals.values()), 4, f"SUM(C5:C{total_row - 1})"),
            (len(rows), 4, f"SUM(D5:D{total_row - 1})"),
            (len({str(row["o_id"]) for row in rows}), 4, ""),
            (None, 4, ""),
        ]
    )

    summary_xml = _xlsx_sheet(
        summary_rows,
        widths=[34, 28, 14, 12, 12, 12],
        merged_title_to="F",
        autofilter=f"A4:F{total_row - 1}",
    )
    detail_xml = _xlsx_sheet(
        detail_rows,
        widths=[27, 23, 16, 18, 20, 38, 14, 10, 24, 28, 12],
        merged_title_to="K",
        autofilter=f"A4:K{detail_end}",
    )

    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="SKU汇总" sheetId="1" r:id="rId1"/><sheet name="出库明细" sheetId="2" r:id="rId2"/></sheets>
  <calcPr calcId="191029" fullCalcOnLoad="1" forceFullCalc="1"/>
</workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.###"/></numFmts>
  <fonts count="3"><font><sz val="10"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="14"/><name val="Microsoft YaHei"/></font><font><b/><sz val="10"/><name val="Microsoft YaHei"/></font></fonts>
  <fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17365D"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FF2F75B5"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/></patternFill></fill></fills>
  <borders count="2"><border/><border><left style="thin"><color rgb="FFD9E2F3"/></left><right style="thin"><color rgb="FFD9E2F3"/></right><top style="thin"><color rgb="FFD9E2F3"/></top><bottom style="thin"><color rgb="FFD9E2F3"/></bottom></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="3" borderId="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/><xf numFmtId="164" fontId="2" fillId="4" borderId="1" applyNumberFormat="1"/></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
    timestamp = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    core_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>聚水潭安全打单助手</dc:creator><cp:lastModifiedBy>聚水潭安全打单助手</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified></cp:coreProperties>'''
    app_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>聚水潭安全打单助手</Application></Properties>'''

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles_xml)
        archive.writestr("xl/worksheets/sheet1.xml", summary_xml)
        archive.writestr("xl/worksheets/sheet2.xml", detail_xml)
    _tighten_private_permissions(target)


def parse_json_output(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", output):
        try:
            value, _ = decoder.raw_decode(output[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("远端命令未返回有效 JSON")


class PlannerClient:
    TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, settings: Settings, workstation_id: str):
        settings.validate()
        if not workstation_id.startswith("ws-"):
            raise ValueError("工作站身份无效")
        self.settings = settings
        self.workstation_id = workstation_id
        self._local_store = None
        self._local_source = None
        if settings.local_mode:
            from jst_local_store import LeaseStore, migrate_event_store
            from jst_local_source import LocalSource
            self._local_store = LeaseStore(APP_DIR / "local-reservations.sqlite3")
            migrate_event_store(self._local_store, DATABASE_FILE, workstation_id)
            _tighten_private_permissions(self._local_store.path)
            self._local_source = LocalSource(
                APP_DIR / "jst_openapi_config.json", APP_DIR / "candidate-orders-v1.json"
            )
            return
        parsed_api = urlsplit(settings.api_url)
        if (
            parsed_api.scheme.lower() != "https"
            or not parsed_api.hostname
            or parsed_api.username is not None
            or parsed_api.password is not None
            or parsed_api.query
            or parsed_api.fragment
        ):
            raise ValueError("后台 API 地址必须是无凭证、无查询的 HTTPS 地址")
        self._api_origin = (
            parsed_api.scheme.lower(),
            str(parsed_api.hostname).lower(),
            parsed_api.port or 443,
        )
        self._opener = urllib.request.build_opener(_RejectRedirectHandler())

    @staticmethod
    def _bounded_http_error_code(exc: urllib.error.HTTPError) -> str:
        """Read only a bounded structured code; never expose the response."""

        try:
            raw = exc.read(65_537)
        except Exception:
            return ""
        if len(raw) > 65_536:
            return ""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        code = payload.get("error")
        if (
            not isinstance(code, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None
        ):
            return ""
        return code

    def _post(
        self,
        endpoint: str,
        payload: Optional[dict[str, Any]] = None,
        timeout: int = 120,
        attempts: int = 3,
    ) -> dict[str, Any]:
        if getattr(self, "_local_store", None) is not None:
            from jst_local_coordinator import APIError, process_request
            from jst_local_store import LeaseConflict
            from jst_openapi import JSTQueryError
            try:
                if endpoint == "ping":
                    self._local_source.check()
                return process_request(
                    "/jst-print-api/v1/" + endpoint, payload or {},
                    store=self._local_store, source=self._local_source,
                )
            except LeaseConflict as exc:
                raise LeaseLostError("本机任务预留已失效") from exc
            except APIError as exc:
                if exc.code == "lease_conflict":
                    raise LeaseLostError("本机任务预留已失效") from exc
                if exc.code == "completion_proof_failed":
                    raise CompletionProofError("本机回读未证明订单完成") from exc
                raise BackendSchemaError("本机协调失败：" + exc.code) from exc
            except JSTQueryError as exc:
                if exc.code in {
                    "NETWORK", "TIMEOUT", "199", "505", "500",
                    "HTTP_408", "HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504",
                }:
                    raise TransientAPIError("聚水潭只读查询暂不可用（" + exc.code + "）") from exc
                raise SafetyStop(str(exc)) from exc
        url = f"{self.settings.api_url.rstrip('/')}/{endpoint.lstrip('/')}"
        raw = b""
        last_transient = ""
        for attempt in range(max(1, attempts)):
            request = urllib.request.Request(
                url,
                data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.settings.api_token}",
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": f"JSTAutoPrint/{APP_VERSION}",
                },
            )
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    final_url = str(response.geturl())
                    final = urlsplit(final_url)
                    final_origin = (
                        final.scheme.lower(),
                        str(final.hostname or "").lower(),
                        final.port or 443,
                    )
                    if final_url != url or final_origin != self._api_origin:
                        raise BackendSchemaError(
                            "后台请求发生重定向或跨源，已禁止发送凭证"
                        )
                    raw = response.read(2_000_001)
                break
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400:
                    raise RuntimeError(
                        f"后台服务返回重定向（{exc.code}），已安全拒绝"
                    ) from exc
                if exc.code == 409:
                    error_code = self._bounded_http_error_code(exc)
                    if error_code == "lease_conflict":
                        raise LeaseLostError(
                            "后台租约已丢失，禁止继续当前订单"
                        ) from exc
                    if error_code == "completion_proof_failed":
                        raise CompletionProofError(
                            "后台无法证明订单已安全完成；任务和租约均已保留，需人工核对"
                        ) from exc
                    raise CompletionProofError(
                        "后台返回无法识别的冲突；未终结订单，需人工核对后台状态"
                    ) from exc
                if exc.code not in self.TRANSIENT_HTTP_CODES:
                    # Never copy a raw HTTP response body into UI/event logs: a
                    # reverse proxy can reflect credentials or order details.
                    raise RuntimeError(f"后台服务拒绝请求（{exc.code}）") from exc
                last_transient = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last_transient = str(reason)
            if attempt + 1 >= max(1, attempts):
                raise TransientAPIError(
                    f"后台网络短暂异常，已重试 {max(1, attempts)} 次：{last_transient}"
                )
            time.sleep(0.5 * (2**attempt))
        if len(raw) > 2_000_000:
            raise BackendSchemaError("后台服务返回内容异常过大")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendSchemaError("后台服务未返回有效数据") from exc
        if not isinstance(result, dict):
            raise BackendSchemaError("后台服务返回格式错误")
        return result

    @staticmethod
    def _aware_timestamp(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise BackendSchemaError(f"后台服务缺少有效 {field}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BackendSchemaError(f"后台服务缺少有效 {field}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise BackendSchemaError(f"后台 {field} 必须包含 UTC 时区")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _require_bool(value: dict[str, Any], field: str) -> bool:
        result = value.get(field)
        if type(result) is not bool:
            raise BackendSchemaError(f"后台字段 {field} 必须为布尔值")
        return result

    @staticmethod
    def _require_identity(value: dict[str, Any], *, context: str) -> tuple[str, str]:
        o_id = value.get("o_id")
        io_id = value.get("io_id")
        if (
            isinstance(o_id, bool)
            or isinstance(io_id, bool)
            or not _is_ascii_order_id(o_id)
            or not _is_ascii_order_id(io_id)
        ):
            raise BackendSchemaError(f"{context}缺少有效内部订单号或出库单号")
        return str(o_id), str(io_id)

    @classmethod
    def _validate_items_schema(cls, items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            raise BackendSchemaError("后台 SKU items 必须为数组")
        line_keys: set[str] = set()
        validated: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise BackendSchemaError("后台 SKU 条目必须为对象")
            for field in ("line_key", "sku_id", "sku_name", "unit"):
                if not isinstance(item.get(field), str):
                    raise BackendSchemaError(f"后台 SKU 字段 {field} 必须为字符串")
            # product_id is additive since V0.5.14.  Old already-claimed local
            # jobs may not carry it; when present it must be JST's text i_id.
            if "product_id" in item and not isinstance(item.get("product_id"), str):
                raise BackendSchemaError("后台 SKU 字段 product_id 必须为字符串")
            line_key = item.get("line_key", "")
            if not line_key or line_key in line_keys or not item.get("sku_name"):
                raise BackendSchemaError("后台 SKU 行身份重复或名称为空")
            line_keys.add(line_key)
            qty = item.get("qty")
            if (
                isinstance(qty, bool)
                or not isinstance(qty, (int, float))
                or not math.isfinite(float(qty))
                or float(qty) <= 0
            ):
                raise BackendSchemaError("后台 SKU 数量无效")
            validated.append(item)
        return validated

    @classmethod
    def _validate_plan_item(cls, item: Any) -> None:
        if not isinstance(item, dict):
            raise BackendSchemaError("后台任务候选必须为对象")
        public_candidate_keys = {
            "o_id",
            "io_id",
            "outbound_identity_unique",
            "weight_kg",
            "current_carrier",
            "current_carrier_id",
            "has_waybill",
            "privacy_required",
            "delivery_hold_marked",
            "items_complete",
            "source_item_count",
            "items",
            "state",
            "steps",
            "blockers",
            "claim_token",
            "expires_at",
            "lease_ttl_seconds",
        }
        if set(item) - {"external_system_order"} != public_candidate_keys:
            raise BackendSchemaError("后台任务候选字段不符合当前安全协议")
        if "external_system_order" in item:
            cls._require_bool(item, "external_system_order")
        cls._require_identity(item, context="后台任务候选")
        has_waybill = cls._require_bool(item, "has_waybill")
        privacy_required = cls._require_bool(item, "privacy_required")
        if cls._require_bool(item, "delivery_hold_marked") is not False:
            raise BackendSchemaError("后台候选带有停发标记")
        if cls._require_bool(item, "outbound_identity_unique") is not True:
            raise BackendSchemaError("后台未证明内部订单号对应唯一出库单")
        if item.get("state") != "READY_HYBRID":
            raise BackendSchemaError("后台任务候选状态不是 READY_HYBRID")
        weight = item.get("weight_kg")
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or float(weight) <= 0
        ):
            raise BackendSchemaError("后台任务候选重量无效")
        current_id = item.get("current_carrier_id")
        current_name = item.get("current_carrier")
        if PRINT_PROFILE_CARRIERS.get(current_id) != current_name:
            raise BackendSchemaError("后台任务候选当前快递不在允许范围")
        blockers = item.get("blockers")
        if not isinstance(blockers, list) or blockers:
            raise BackendSchemaError("可执行候选不得携带阻断原因")
        steps = item.get("steps")
        if not isinstance(steps, list) or any(type(step) is not str for step in steps):
            raise BackendSchemaError("后台任务步骤必须为字符串数组")
        sequence = tuple(steps)
        if sequence not in ALLOWED_PLAN_SEQUENCES:
            raise BackendSchemaError("后台任务步骤不符合安全白名单")
        privacy_step = (
            f"RESET_CARRIER_AND_GET_WAYBILL:{PRIVACY_CARRIER_ID}:{PRIVACY_CARRIER_NAME}"
        )
        reset_steps = [
            step for step in sequence if step.startswith("RESET_CARRIER_AND_GET_WAYBILL:")
        ]
        final_carrier_id = (
            reset_target(reset_steps[0])[0] if reset_steps else str(current_id)
        )
        resets_to_privacy = privacy_step in sequence
        if privacy_required and not (
            resets_to_privacy
            or (not any(step.startswith("RESET_CARRIER") for step in sequence)
                and current_id == PRIVACY_CARRIER_ID)
        ):
            raise BackendSchemaError("隐私备注规则与计划目标快递不一致")
        if not privacy_required and resets_to_privacy:
            raise BackendSchemaError("普通订单禁止自动改为隐私快递")
        if (
            not privacy_required
            and float(weight) > WEIGHT_THRESHOLD_KG
            and final_carrier_id != SOURCE_CARRIER_ID
        ):
            raise BackendSchemaError("超过3kg的普通订单计划最终快递必须为申通")
        if has_waybill != (sequence == ("PRINT_EXPRESS", "STOP_BEFORE_PRESHIP")):
            raise BackendSchemaError("候选运单状态与任务步骤不一致")
        if item.get("items_complete") is not True:
            raise BackendSchemaError("后台计划未确认 SKU 明细完整")
        source_item_count = item.get("source_item_count")
        planned_items = cls._validate_items_schema(item.get("items"))
        if (
            isinstance(source_item_count, bool)
            or not isinstance(source_item_count, int)
            or source_item_count <= 0
            or source_item_count != len(planned_items)
        ):
            raise BackendSchemaError("后台计划 SKU 行数与来源不一致")
        token = item.get("claim_token")
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{32,128}", token
        ):
            raise BackendSchemaError("后台任务缺少有效 claim_token")
        ttl = item.get("lease_ttl_seconds")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 5 <= ttl <= 3600:
            raise BackendSchemaError("后台任务租约时长无效")
        expires = cls._aware_timestamp(item.get("expires_at"), "expires_at")
        if expires <= datetime.now(timezone.utc):
            raise BackendSchemaError("后台任务租约已经过期")

    @classmethod
    def _validate_blocked_preview(cls, value: Any) -> None:
        if not isinstance(value, list) or len(value) > 50:
            raise BackendSchemaError("后台 blocked_preview 必须为数组")
        blocked_keys = {
            "o_id",
            "io_id",
            "identity_complete",
            "weight_kg",
            "items_complete",
            "blockers",
        }
        for item in value:
            if not isinstance(item, dict) or set(item) != blocked_keys:
                raise BackendSchemaError("后台阻断候选必须为对象")
            identities: list[str] = []
            for field in ("o_id", "io_id"):
                raw_identity = item[field]
                if not isinstance(raw_identity, str):
                    raise BackendSchemaError(f"后台阻断候选 {field} 格式错误")
                identity = str(raw_identity)
                if identity and not _is_ascii_order_id(identity):
                    raise BackendSchemaError(f"后台阻断候选 {field} 必须为数字")
                identities.append(identity)
            identity_complete = item["identity_complete"]
            if type(identity_complete) is not bool or identity_complete is not all(
                bool(identity) for identity in identities
            ):
                raise BackendSchemaError("后台阻断候选身份完整性不一致")
            weight = item["weight_kg"]
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(float(weight))
                or not 0 <= float(weight) <= 100_000
            ):
                raise BackendSchemaError("后台阻断候选重量无效")
            if type(item["items_complete"]) is not bool:
                raise BackendSchemaError("后台阻断候选 SKU 完整性无效")
            blockers = item.get("blockers")
            if (
                not isinstance(blockers, list)
                or not blockers
                or len(blockers) > 20
                or any(
                    not isinstance(reason, str)
                    or not reason
                    or len(reason) > 200
                    or any(ord(character) < 32 for character in reason)
                    for reason in blockers
                )
            ):
                raise BackendSchemaError("后台阻断原因格式错误")

    @staticmethod
    def _validate_plan_counts(value: Any, selected_count: int) -> None:
        if not isinstance(value, dict):
            raise BackendSchemaError("后台 counts 必须为对象")
        required = (
            "orders_read",
            "in_scope",
            "ready",
            "blocked",
            "profile_ready",
            "selected",
        )
        counts: dict[str, int] = {}
        for field in required:
            count = value.get(field)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise BackendSchemaError(f"后台统计 {field} 必须为非负整数")
            counts[field] = count
        if counts["ready"] + counts["blocked"] != counts["in_scope"]:
            raise BackendSchemaError("后台待审、符合与阻断数量不一致")
        if counts["profile_ready"] > counts["ready"]:
            raise BackendSchemaError("本类符合数量超过全部符合数量")
        if counts["selected"] != selected_count:
            raise BackendSchemaError("后台领取统计与候选数组数量不一致")
        if counts["selected"] > counts["profile_ready"]:
            raise BackendSchemaError("后台领取数量超过本类符合数量")
        for field in (
            "profile_excluded",
            "external_order_excluded",
            "claim_attempted",
            "claim_unavailable",
        ):
            if field not in value:
                continue
            count = value[field]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise BackendSchemaError(f"后台统计 {field} 必须为非负整数")
        if value.get("profile_excluded", 0) > counts["profile_ready"]:
            raise BackendSchemaError("本机排除数量超过本类符合数量")
        if value.get("claim_unavailable", 0) > value.get("claim_attempted", 0):
            raise BackendSchemaError("领取不可用数量超过领取尝试数量")
        diagnostics_present = all(
            field in value for field in ("profile_excluded", "claim_unavailable")
        )
        if diagnostics_present:
            accounted = (
                value["profile_excluded"]
                + value["claim_unavailable"]
                + counts["selected"]
            )
            if accounted > counts["profile_ready"]:
                raise BackendSchemaError("本类排除、不可用与领取数量超过符合数量")
            if counts["selected"] == 0 and accounted != counts["profile_ready"]:
                raise BackendSchemaError("空领取结果没有完整解释全部本类符合订单")

    def plan(
        self,
        exclude: list[dict[str, str]],
        print_profile: str,
        max_candidates: int = BATCH_PRINT_SIZE,
    ) -> dict[str, Any]:
        if not isinstance(exclude, list) or len(exclude) > 200:
            raise ValueError("本地排除订单必须为不超过 200 个的数组")
        if print_profile not in PRINT_PROFILE_CARRIERS:
            raise ValueError("本轮面单类型无效")
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or not 1 <= max_candidates <= BATCH_PRINT_SIZE
        ):
            raise ValueError("本轮候选订单数量限制无效")
        for pair in exclude:
            if (
                not isinstance(pair, dict)
                or not _is_ascii_order_id(pair.get("o_id"))
                or not _is_ascii_order_id(pair.get("io_id"))
            ):
                raise ValueError("本地排除订单缺少有效复合身份")
        result = self._post(
            "plan",
            {
                "workstation_id": self.workstation_id,
                "exclude": exclude,
                "max_candidates": max_candidates,
                "print_profile": print_profile,
                **({"skip_external_orders": True} if self.settings.skip_external_orders else {}),
            },
            # A plan refresh is an expensive bounded full scan. The worker's
            # normal loop is the retry boundary; do not amplify one timeout
            # into three identical long-running scans.
            attempts=1,
        )
        self._validate_live_payload(result, "LIVE_READ_ONLY_CLAIMED_V1")
        if result.get("lease_required") is not True:
            raise BackendSchemaError("后台没有启用强制租约，禁止自动处理")
        if result.get("print_profile") != print_profile:
            raise BackendSchemaError("后台没有严格应用本轮面单类型")
        if self.settings.skip_external_orders and result.get("skip_external_orders") is not True:
            raise BackendSchemaError("后台尚未支持跳过外部系统订单，请更新后台后再启用")
        selected = result.get("selected")
        if not isinstance(selected, list) or len(selected) > max_candidates:
            raise BackendSchemaError("后台任务返回的候选数量不符合安全限制")
        o_ids: set[str] = set()
        io_ids: set[str] = set()
        identities: set[tuple[str, str]] = set()
        claim_tokens: set[str] = set()
        for item in selected:
            self._validate_plan_item(item)
            if self.settings.skip_external_orders and item.get("external_system_order") is not False:
                raise BackendSchemaError("后台未排除外部系统订单或缺少标签识别结果")
            if plan_print_profile(item) != print_profile:
                raise BackendSchemaError("后台返回了其他面单类型的候选订单")
            o_id, io_id = self._require_identity(item, context="后台任务候选")
            token = str(item["claim_token"])
            if (
                o_id in o_ids
                or io_id in io_ids
                or (o_id, io_id) in identities
                or token in claim_tokens
            ):
                raise BackendSchemaError("后台任务候选订单身份或租约令牌重复")
            o_ids.add(o_id)
            io_ids.add(io_id)
            identities.add((o_id, io_id))
            claim_tokens.add(token)
        self._validate_plan_counts(result.get("counts"), len(selected))
        self._validate_blocked_preview(result.get("blocked_preview"))
        return result

    def ping(self) -> bool:
        result = self._post("ping", {}, timeout=8, attempts=2)
        if result.get("ok") is not True:
            raise BackendSchemaError("后台健康检查未返回 ok=true")
        if type(result.get("api_schema_version")) is not int or result.get(
            "api_schema_version"
        ) != API_SCHEMA_VERSION:
            raise BackendSchemaError(
                f"后台 API schema 版本不是 {API_SCHEMA_VERSION}"
            )
        minimum_version = result.get("minimum_client_version")
        if not isinstance(minimum_version, str) or not re.fullmatch(
            r"\d+\.\d+\.\d+", minimum_version
        ):
            raise BackendSchemaError("后台未返回有效最低客户端版本")
        current_parts = tuple(int(part) for part in APP_VERSION.split("."))
        minimum_parts = tuple(int(part) for part in minimum_version.split("."))
        if current_parts < minimum_parts:
            raise BackendSchemaError(
                f"当前客户端 V{APP_VERSION} 低于后台最低版本 V{minimum_version}"
            )
        if result.get("lease_required") is not True:
            raise BackendSchemaError("后台未启用强制租约")
        if result.get("workstation_binding") != WORKSTATION_BINDING_MODE:
            raise BackendSchemaError("后台工作站绑定模式不兼容")
        if result.get("completion_reasons") != sorted(COMPLETION_REASONS):
            raise BackendSchemaError("后台租约完成原因白名单不兼容")
        if result.get("plan_mode") != "LIVE_READ_ONLY_CLAIMED_V1":
            raise BackendSchemaError("后台 plan 模式不兼容")
        if result.get("inspect_mode") != "ORDER_READBACK_CLAIMED_V1":
            raise BackendSchemaError("后台 inspect 模式不兼容")
        if result.get("batch_inspect_mode") != "ORDER_BATCH_READBACK_CLAIMED_V1":
            raise BackendSchemaError("后台批量 inspect 模式不兼容")
        return True

    @staticmethod
    def _claim_payload(
        workstation_id: str, o_id: str, io_id: str, claim_token: str
    ) -> dict[str, str]:
        if (
            not _is_ascii_order_id(o_id)
            or not _is_ascii_order_id(io_id)
            or not isinstance(claim_token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", claim_token)
        ):
            raise ValueError("租约请求缺少有效订单身份或 claim_token")
        return {
            "workstation_id": workstation_id,
            "o_id": str(o_id),
            "io_id": str(io_id),
            "claim_token": str(claim_token),
        }

    @classmethod
    def _validate_inspect_schema(
        cls, result: dict[str, Any], o_id: str, io_id: str
    ) -> None:
        found = cls._require_bool(result, "found")
        claimed_metadata_keys = {
            "mode",
            "api_schema_version",
            "planner_schema_version",
            "lease_required",
            "generated_at",
            "lease_expires_at",
            "lease_ttl_seconds",
        }
        common_keys = claimed_metadata_keys | {"found", "o_id", "io_id"}
        found_keys = common_keys | {
            "warehouse_id",
            "status",
            "weight_kg",
            "io_date",
            "shop_name",
            "carrier_id",
            "carrier_name",
            "has_waybill",
            "waybill_suffix",
            "waybill_fingerprint",
            "is_print_express",
            "action_history_complete",
            "actions",
            "has_print_request",
            "has_print_action",
            "has_ship_action",
            "has_manual_carrier_action",
            "privacy_required",
            "privacy_source",
            "privacy_review_required",
            "delivery_hold_marked",
            "delivery_hold_reasons",
            "items_complete",
            "source_item_count",
            "item_validation_errors",
            "order_validation_errors",
            "items",
            "redaction",
        }
        if "external_system_order" in result:
            cls._require_bool(result, "external_system_order")
        expected_keys = found_keys if found else common_keys
        if set(result) - {"external_system_order"} != expected_keys:
            raise BackendSchemaError("后台回读字段不符合当前安全协议")
        if (
            result.get("mode") != "ORDER_READBACK_CLAIMED_V1"
            or type(result.get("api_schema_version")) is not int
            or result.get("api_schema_version") != API_SCHEMA_VERSION
            or type(result.get("planner_schema_version")) is not int
            or result.get("planner_schema_version") != PLANNER_SCHEMA_VERSION
            or result.get("lease_required") is not True
        ):
            raise BackendSchemaError("后台回读 claimed 协议元数据无效")
        cls._aware_timestamp(result.get("generated_at"), "generated_at")
        lease_expires = cls._aware_timestamp(
            result.get("lease_expires_at"), "lease_expires_at"
        )
        ttl = result.get("lease_ttl_seconds")
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, int)
            or not 5 <= ttl <= 3600
            or lease_expires <= datetime.now(timezone.utc)
        ):
            raise BackendSchemaError("后台回读租约元数据无效")
        actual_o_id, actual_io_id = cls._require_identity(result, context="后台回读")
        if (actual_o_id, actual_io_id) != (str(o_id), str(io_id)):
            raise BackendSchemaError("后台回读订单身份与请求不一致")
        if not found:
            return
        for field in (
            "has_waybill",
            "is_print_express",
            "action_history_complete",
            "has_print_request",
            "has_print_action",
            "has_ship_action",
            "has_manual_carrier_action",
            "privacy_required",
            "privacy_review_required",
            "delivery_hold_marked",
        ):
            cls._require_bool(result, field)
        for field in ("status", "io_date", "shop_name"):
            if not isinstance(result.get(field), str):
                raise BackendSchemaError(f"后台字段 {field} 必须为字符串")
        if not result["status"]:
            raise BackendSchemaError("后台订单状态无效")
        weight = result.get("weight_kg")
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or float(weight) <= 0
        ):
            raise BackendSchemaError("后台回读订单重量无效")
        if result.get("warehouse_id") != TARGET_WAREHOUSE_ID:
            raise BackendSchemaError("后台回读仓库不是山东汇馨仓库")
        for field in (
            "carrier_id",
            "carrier_name",
            "waybill_suffix",
            "waybill_fingerprint",
        ):
            if not isinstance(result.get(field), str):
                raise BackendSchemaError(f"后台字段 {field} 必须为字符串")
        if not result.get("carrier_id") or not result.get("carrier_name"):
            raise BackendSchemaError("后台回读缺少快递身份")
        if len(result.get("waybill_suffix", "")) > 4:
            raise BackendSchemaError("后台不得返回完整运单号")
        if bool(result.get("waybill_suffix")) is not bool(result.get("has_waybill")):
            raise BackendSchemaError("后台运单后缀与运单状态不一致")
        fingerprint = result.get("waybill_fingerprint", "")
        if (
            result.get("has_waybill")
            and re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        ) or (not result.get("has_waybill") and fingerprint != ""):
            raise BackendSchemaError("后台运单指纹与运单状态不一致")
        if result.get("privacy_source") != "remark":
            raise BackendSchemaError("后台隐私判断来源不是订单备注")
        if result.get("redaction") != (
            "buyer, address, phone and full waybill are omitted"
        ):
            raise BackendSchemaError("后台回读脱敏声明无效")
        if result.get("actions") != []:
            raise BackendSchemaError("后台不得向客户端下发原始操作历史")
        items_complete = cls._require_bool(result, "items_complete")
        item_errors = result.get("item_validation_errors")
        order_errors = result.get("order_validation_errors")
        delivery_hold_errors = result.get("delivery_hold_reasons")
        for field, errors in (
            ("item_validation_errors", item_errors),
            ("order_validation_errors", order_errors),
            ("delivery_hold_reasons", delivery_hold_errors),
        ):
            if not isinstance(errors, list) or any(
                not isinstance(error, str) or not error for error in errors
            ):
                raise BackendSchemaError(f"后台回读 {field} 格式错误")
        items = cls._validate_items_schema(result.get("items"))
        source_item_count = result.get("source_item_count")
        if (
            isinstance(source_item_count, bool)
            or not isinstance(source_item_count, int)
            or source_item_count < 0
        ):
            raise BackendSchemaError("后台回读 SKU 来源行数无效")
        if items_complete:
            if (
                item_errors
                or order_errors
                or source_item_count <= 0
                or source_item_count != len(items)
            ):
                raise BackendSchemaError("完整 SKU 回读的行数或校验结果不一致")
        elif source_item_count < len(items) or not (item_errors or order_errors):
            raise BackendSchemaError("不完整 SKU 回读必须携带诊断错误且不得超出来源行数")
        if result.get("delivery_hold_marked") is not bool(delivery_hold_errors):
            raise BackendSchemaError("后台停发标记与诊断原因不一致")

    def inspect(self, o_id: str, io_id: str, claim_token: str) -> dict[str, Any]:
        payload = self._claim_payload(
            self.workstation_id, o_id, io_id, claim_token
        )
        # The server terminates an inspect planner at 35 seconds.  Keep the
        # client just outside that boundary and never start a duplicate
        # subprocess while the first exact readback is still running.
        if self.settings.skip_external_orders:
            payload["include_external_order_status"] = True
        result = self._post("inspect", payload, timeout=42, attempts=1)
        self._validate_live_payload(result, "ORDER_READBACK_CLAIMED_V1")
        self._validate_inspect_schema(result, str(o_id), str(io_id))
        return result

    def inspect_batch(
        self, credentials: list[tuple[str, str, str]]
    ) -> list[dict[str, Any]]:
        """Renew and read back one exact batch with one backend planner run."""

        if not isinstance(credentials, list) or not 1 <= len(credentials) <= BATCH_PRINT_SIZE:
            raise ValueError("批量回读必须包含 1—10 笔订单")
        payload_orders: list[dict[str, str]] = []
        requested_pairs: list[tuple[str, str]] = []
        for credential in credentials:
            if not isinstance(credential, tuple) or len(credential) != 3:
                raise ValueError("批量回读租约格式无效")
            o_id, io_id, claim_token = credential
            claimed = self._claim_payload(
                self.workstation_id, o_id, io_id, claim_token
            )
            pair = (claimed["o_id"], claimed["io_id"])
            if pair in requested_pairs:
                raise ValueError("批量回读订单身份重复")
            requested_pairs.append(pair)
            payload_orders.append(
                {
                    "o_id": claimed["o_id"],
                    "io_id": claimed["io_id"],
                    "claim_token": claimed["claim_token"],
                }
            )
        result = self._post(
            "inspect-batch",
            {
                "workstation_id": self.workstation_id,
                "orders": payload_orders,
                **({"include_external_order_status": True} if self.settings.skip_external_orders else {}),
            },
            timeout=42,
            attempts=1,
        )
        self._validate_live_payload(result, "ORDER_BATCH_READBACK_CLAIMED_V1")
        readbacks = result.get("results")
        if not isinstance(readbacks, list) or len(readbacks) != len(requested_pairs):
            raise BackendSchemaError("后台批量回读数量与请求不一致")
        for expected, readback in zip(requested_pairs, readbacks):
            try:
                if not isinstance(readback, dict):
                    raise BackendSchemaError("后台批量回读条目必须为对象")
                self._validate_live_payload(readback, "ORDER_READBACK_CLAIMED_V1")
                self._validate_inspect_schema(readback, expected[0], expected[1])
            except Exception as exc:
                setattr(exc, "_jst_job_identity", expected)
                raise
        return readbacks

    @staticmethod
    def _validate_lease_identity(
        result: dict[str, Any], o_id: str, io_id: str, claim_token: str
    ) -> None:
        for field, expected in (
            ("o_id", str(o_id)),
            ("io_id", str(io_id)),
            ("claim_token", str(claim_token)),
        ):
            if field not in result or str(result[field]) != expected:
                raise BackendSchemaError(f"租约响应 {field} 与请求不一致")

    @staticmethod
    def _validate_lease_contract(result: dict[str, Any]) -> None:
        if type(result.get("api_schema_version")) is not int or result.get(
            "api_schema_version"
        ) != API_SCHEMA_VERSION:
            raise BackendSchemaError(
                f"租约响应 API schema 版本不是 {API_SCHEMA_VERSION}"
            )
        if result.get("lease_required") is not True:
            raise BackendSchemaError("租约响应未确认 lease_required=true")

    def renew(self, o_id: str, io_id: str, claim_token: str) -> dict[str, Any]:
        payload = self._claim_payload(
            self.workstation_id, o_id, io_id, claim_token
        )
        result = self._post("lease/renew", payload, timeout=12, attempts=2)
        self._validate_lease_contract(result)
        if result.get("ok") is not True or result.get("state") != "ACTIVE":
            raise LeaseLostError("续租未返回 ACTIVE，禁止执行任何按钮")
        self._validate_lease_identity(result, o_id, io_id, claim_token)
        generated = self._aware_timestamp(result.get("generated_at"), "generated_at")
        expires = self._aware_timestamp(result.get("expires_at"), "expires_at")
        if expires <= generated or expires <= datetime.now(timezone.utc):
            raise LeaseLostError("续租返回的 expires_at 已过期，禁止执行任何按钮")
        return result

    def release(self, o_id: str, io_id: str, claim_token: str) -> None:
        payload = self._claim_payload(
            self.workstation_id, o_id, io_id, claim_token
        )
        result = self._post("lease/release", payload, timeout=12, attempts=2)
        self._validate_lease_contract(result)
        if result.get("ok") is not True or result.get("state") != "RELEASED":
            raise BackendSchemaError("后台未确认租约 RELEASED")
        self._validate_lease_identity(result, o_id, io_id, claim_token)

    def complete(
        self,
        o_id: str,
        io_id: str,
        claim_token: str,
        completion_reason: str,
    ) -> None:
        if completion_reason not in COMPLETION_REASONS:
            raise ValueError("租约完成原因不在安全白名单")
        payload = self._claim_payload(
            self.workstation_id, o_id, io_id, claim_token
        )
        payload["completion_reason"] = completion_reason
        result = self._post("lease/complete", payload, timeout=12, attempts=2)
        self._validate_lease_contract(result)
        if result.get("ok") is not True or result.get("state") != "COMPLETED":
            raise BackendSchemaError("后台未确认租约 COMPLETED")
        self._validate_lease_identity(result, o_id, io_id, claim_token)

    def force_skip(self, o_id: str, io_id: str, reason: str) -> None:
        self._require_identity({"o_id": o_id, "io_id": io_id}, context="强制跳过订单")
        result = self._post(
            "order/force-skip",
            {"workstation_id": self.workstation_id, "o_id": o_id,
             "io_id": io_id, "reason": reason[:2000]},
            timeout=12, attempts=2,
        )
        if (
            result.get("ok") is not True
            or type(result.get("api_schema_version")) is not int
            or result.get("api_schema_version") != API_SCHEMA_VERSION
            or self._require_identity(result, context="后台强制跳过响应") != (o_id, io_id)
            or result.get("permanently_excluded") is not True
            or result.get("completion_reason") != "OPERATOR_SKIPPED"
        ):
            raise BackendSchemaError("后台未确认该订单已永久排除，未执行本地跳过")

    @staticmethod
    def _validate_live_payload(result: dict[str, Any], expected_mode: str) -> None:
        if type(result.get("api_schema_version")) is not int or result.get(
            "api_schema_version"
        ) != API_SCHEMA_VERSION:
            raise BackendSchemaError(
                f"后台 API schema 版本不是 {API_SCHEMA_VERSION}"
            )
        if type(result.get("planner_schema_version")) is not int or result.get(
            "planner_schema_version"
        ) != PLANNER_SCHEMA_VERSION:
            raise BackendSchemaError(
                f"后台 planner schema 版本不是 {PLANNER_SCHEMA_VERSION}"
            )
        if result.get("lease_required") is not True:
            raise BackendSchemaError("后台响应未确认 lease_required=true")
        if result.get("mode") != expected_mode:
            raise BackendSchemaError("后台服务未启用 claimed V1 安全模式")
        generated = PlannerClient._aware_timestamp(
            result.get("generated_at"), "generated_at"
        )
        current = datetime.now(timezone.utc)
        age_seconds = (current - generated).total_seconds()
        if age_seconds < -FUTURE_CLOCK_SKEW_SECONDS:
            raise BackendSchemaError("后台数据时间超前，禁止使用")
        max_age_seconds = (
            PLAN_MAX_AGE_SECONDS
            if expected_mode == "LIVE_READ_ONLY_CLAIMED_V1"
            else INSPECT_MAX_AGE_SECONDS
        )
        if age_seconds > max_age_seconds:
            if max_age_seconds == PLAN_MAX_AGE_SECONDS:
                raise BackendSchemaError("后台计划数据超过 10 分钟，禁止使用")
            raise BackendSchemaError("后台实时回读超过 30 秒，禁止继续操作")


def browser_executable(browser_name: str) -> Optional[str]:
    system = platform.system()
    candidates: list[str] = []
    if system == "Darwin":
        candidates = {
            "Chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
            "Edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        }[browser_name]
    elif system == "Windows":
        roots = [
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        tails = {
            "Chrome": [
                "Google/Chrome/Application/chrome.exe",
                "Google/Chrome Beta/Application/chrome.exe",
            ],
            "Edge": ["Microsoft/Edge/Application/msedge.exe"],
        }[browser_name]
        candidates = [str(Path(root) / tail) for root in roots if root for tail in tails]
    else:
        names = {
            "Chrome": ["google-chrome", "google-chrome-stable", "chromium"],
            "Edge": ["microsoft-edge", "microsoft-edge-stable"],
        }[browser_name]
        candidates = [path for name in names if (path := shutil.which(name))]
    return next((path for path in candidates if Path(path).exists()), None)


def _devtools_active_port_paths(browser_name: str) -> list[Path]:
    """Return only this application's dedicated browser endpoint file.

    Reading the normal Chrome/Edge profile's endpoint attaches to an operator's
    everyday browser and causes Chrome's mandatory remote-debugging approval
    dialog.  The application instead owns a persistent, isolated profile under
    ``PROFILE_ROOT`` and launches it with a command-line debugging endpoint.
    """

    return [PROFILE_ROOT / browser_name.lower() / "DevToolsActivePort"]


def _active_port_websocket(port: int, browser_name: str) -> Optional[str]:
    """Resolve a live endpoint published by the dedicated browser profile."""

    for path in _devtools_active_port_paths(browser_name):
        try:
            lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeError):
            continue
        if len(lines) < 2:
            continue
        try:
            published_port = int(lines[0])
        except (TypeError, ValueError):
            continue
        if not 1024 <= published_port <= 65535:
            continue
        websocket_path = lines[1]
        if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", websocket_path):
            continue
        # DevToolsActivePort survives some browser restarts. Prove that the
        # advertised loopback port is live before trusting it.
        try:
            with socket.create_connection(
                ("127.0.0.1", published_port), timeout=0.5
            ):
                pass
        except OSError:
            continue
        return f"ws://127.0.0.1:{published_port}{websocket_path}"
    return None


def cdp_endpoint(port: int, browser_name: str = "Chrome") -> str:
    # A port-zero dedicated launch may publish a randomized endpoint. Prefer
    # that profile-owned endpoint before probing the configured fixed port.
    published_websocket = _active_port_websocket(port, browser_name)
    if published_websocket:
        return published_websocket
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirectHandler()
        )
        with opener.open(url, timeout=0.5) as response:
            if str(response.geturl()) != url:
                raise RuntimeError("浏览器调试端口返回了重定向")
            raw = response.read(65_537)
        if len(raw) > 65_536:
            raise RuntimeError("浏览器调试响应过大")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        websocket = _active_port_websocket(port, browser_name)
        if websocket:
            return websocket
        raise RuntimeError(f"无法连接浏览器调试端口 {port}：{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("浏览器调试端口响应格式错误")
    endpoint = payload.get("webSocketDebuggerUrl")
    if not isinstance(endpoint, str):
        raise RuntimeError("浏览器没有返回 webSocketDebuggerUrl")
    try:
        parsed = urlsplit(endpoint)
        endpoint_port = parsed.port
    except ValueError as exc:
        raise RuntimeError("浏览器 WebSocket 地址无效") from exc
    if (
        parsed.scheme.lower() != "ws"
        or str(parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}
        or endpoint_port != int(port)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", parsed.path)
    ):
        raise RuntimeError("浏览器 WebSocket 不是当前本机专用调试端口")
    return f"ws://127.0.0.1:{port}{parsed.path}"


def _navigate_connected_jst(browser: Any) -> Any:
    contexts = list(browser.contexts)
    pages = [page for context in contexts for page in context.pages]
    print_pages = []
    for page in pages:
        if not _is_jst_frame_path(page.url, "page"):
            continue
        try:
            route_names = parse_qs(urlsplit(str(page.url)).query).get("n", [])
        except Exception:
            route_names = []
        if (
            route_names == ["打单拣货"]
            or JSTBrowser._page_has_visible_express_frame(page)
        ):
            print_pages.append(page)
    if len(print_pages) > 1:
        raise SafetyStop("当前浏览器存在多个打单拣货页面，请只保留一个后重试")
    if print_pages:
        # Reuse the operator's current signed-in tab exactly as-is.  Reloading
        # it here can replace the expresssetter iframe during the first lookup
        # and is one source of misleading DOM=0 results.
        return print_pages[0]
    else:
        if len(contexts) != 1:
            raise SafetyStop("当前浏览器上下文不唯一，无法安全进入打单拣货")
        page = contexts[0].new_page()
    try:
        page.goto(JST_HOME_URL, wait_until="domcontentloaded", timeout=20_000)
        if not _is_jst_frame_path(page.url, "page"):
            raise OrderRowNotReady(
                "同一登录会话新建打单页后被重定向；不会自动登录或填写账号"
            )
        return page
    except Exception:
        try:
            page.close()
        except Exception:
            pass
        raise


def launch_browser(settings: Settings) -> subprocess.Popen[Any] | None:
    settings.validate()
    try:
        cdp_endpoint(settings.debug_port, settings.browser_name)
    except RuntimeError:
        pass
    else:
        # Reuse the already-running dedicated browser and its persisted login.
        return None
    executable = browser_executable(settings.browser_name)
    if not executable:
        raise RuntimeError(f"没有找到 {settings.browser_name}，请先安装浏览器")
    # Chrome 136+ requires a non-default user-data directory for command-line
    # CDP. This persistent application-owned profile avoids the browser's
    # current-profile debugging approval dialog. It also keeps the JST login
    # between application restarts after the operator signs in once.
    profile_dir = PROFILE_ROOT / settings.browser_name.lower()
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _tighten_private_permissions(profile_dir, directory=True)
    kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        [
            executable,
            f"--remote-debugging-port={int(settings.debug_port)}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile_dir.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            JST_HOME_URL,
        ],
        **kwargs,
    )
    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            cdp_endpoint(settings.debug_port, settings.browser_name)
            return process
        except RuntimeError:
            time.sleep(0.25)
    raise RuntimeError(
        "30 秒内未检测到专用浏览器连接；请关闭占用调试端口的其他程序后，"
        "重新点击“进入聚水潭”"
    )


def open_jst_print_page(settings: Settings) -> None:
    """Open and validate the print page in the existing signed-in context."""

    browser: Optional[JSTNativeBrowser] = None
    try:
        browser = JSTNativeBrowser(settings)
        if not browser.is_healthy():
            raise RuntimeError("聚水潭打单页已打开，但原生浏览器连接健康检查失败")
    finally:
        if browser is not None:
            browser.close()


def print_service_online(ports: tuple[int, ...] = PRINT_SERVICE_PORTS) -> bool:
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            continue
    return False


class _LegacyPlaywrightJSTBrowser:
    def __init__(self, settings: Settings):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 playwright，请按说明安装依赖") from exc
        self._playwright = sync_playwright().start()
        try:
            self.browser = self._playwright.chromium.connect_over_cdp(
                cdp_endpoint(settings.debug_port, settings.browser_name)
            )
            try:
                self.page = self._find_jst_page()
            except JSTPageMissing:
                # A closed print tab is recoverable without changing browser,
                # profile, login, or BrowserContext.  The new tab inherits the
                # existing context's cookies/local storage; a login redirect
                # is rejected and never filled automatically.
                opened_page = self._open_jst_page_in_unique_context()
                try:
                    self._prove_page_ready(opened_page)
                    opened_context = opened_page.context
                    self._require_page_commit_ready(
                        opened_page,
                        opened_context,
                    )
                except Exception:
                    try:
                        opened_page.close()
                    except Exception:
                        pass
                    raise
                self.page = opened_page
                self.context = opened_context
            else:
                self.context = self.page.context
                self._require_page_commit_ready(self.page, self.context)
        except Exception:
            # Do not leak a Playwright driver/transport when page discovery
            # fails.  Stopping this client disconnects CDP; it does not close
            # the operator's Chrome/Edge process.
            try:
                self._playwright.stop()
            except Exception:
                pass
            raise

    @staticmethod
    def _visible_express_frames(page: Any) -> list[Any]:
        try:
            frames = list(page.frames)
        except Exception:
            return []
        candidates = []
        for frame in frames:
            try:
                if not _is_jst_frame_path(frame.url, "express"):
                    continue
                if not frame.locator(
                    "#printExpress_Btn, #ResetLc_Btn, #GETExpress_Btn"
                ).count():
                    continue
                if frame == page.main_frame:
                    visible = True
                else:
                    visible = frame.frame_element().is_visible()
                if visible and not frame.is_detached():
                    candidates.append(frame)
            except Exception:
                continue
        unique = []
        for frame in candidates:
            if frame not in unique:
                unique.append(frame)
        return unique

    @classmethod
    def _page_has_visible_express_frame(cls, page: Any) -> bool:
        return bool(cls._visible_express_frames(page))

    def _open_jst_page_in_unique_context(self):
        contexts = list(self.browser.contexts)
        if len(contexts) != 1:
            raise SafetyStop(
                "当前浏览器上下文不唯一，禁止猜测登录会话并新建打单页"
            )
        context = contexts[0]
        bound_context = getattr(self, "context", None)
        if bound_context is not None and context is not bound_context:
            raise SafetyStop(
                "原登录浏览器上下文（BrowserContext）已消失或被替换，"
                "禁止切换到其他会话"
            )
        page = context.new_page()
        try:
            page.goto(JST_HOME_URL, wait_until="domcontentloaded", timeout=20_000)
            if not _is_jst_frame_path(page.url, "page"):
                raise OrderRowNotReady(
                    "同一登录会话新建打单页后被重定向；"
                    "不会自动登录或填写账号"
                )
            return page
        except Exception:
            try:
                page.close()
            except Exception:
                pass
            raise

    def _find_jst_page(self):
        pages = [page for context in self.browser.contexts for page in context.pages]
        matches = [page for page in pages if _is_jst_frame_path(page.url, "page")]
        if not matches:
            raise JSTPageMissing("当前登录会话中没有聚水潭 epaas 页签")
        active = [page for page in matches if self._page_has_visible_express_frame(page)]
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            raise SafetyStop("检测到多个真实打单拣货页面，请只保留一个")
        route_matches = []
        for page in matches:
            try:
                names = parse_qs(urlsplit(str(page.url)).query).get("n", [])
            except Exception:
                names = []
            if names == ["打单拣货"]:
                route_matches.append(page)
        if len(route_matches) == 1:
            return route_matches[0]
        if len(route_matches) > 1:
            raise SafetyStop("检测到多个打单拣货路由页面，请只保留一个")
        raise JSTPageMissing(
            "当前登录会话只有普通聚水潭页面，没有可证明的打单拣货页签"
        )

    def _prove_page_ready(self, page: Any) -> None:
        deadline = time.time() + 12
        while time.time() < deadline:
            frames = self._visible_express_frames(page)
            if len(frames) > 1:
                raise SafetyStop(
                    f"新打单页同时显示 {len(frames)} 个打单拣货 iframe"
                )
            if len(frames) == 1:
                self._verify_warehouse_context(page)
                return
            time.sleep(0.4)
        raise OrderRowNotReady("新打单页未呈现唯一打单拣货 iframe")

    def _prove_replacement_topology(self, old_page: Any, new_page: Any) -> None:
        """Prove the replacement while the recoverable old page is still open."""

        contexts = list(self.browser.contexts)
        bound_context = getattr(self, "context", None)
        if (
            len(contexts) != 1
            or bound_context is None
            or contexts[0] is not bound_context
            or new_page.context is not bound_context
        ):
            raise SafetyStop(
                "页面替换期间原登录浏览器上下文（BrowserContext）已变化，"
                "禁止切换会话"
            )
        self._prove_page_ready(new_page)
        unexpected = []
        for candidate in list(bound_context.pages):
            if candidate is old_page or candidate is new_page:
                continue
            try:
                if candidate.is_closed():
                    continue
            except Exception:
                pass
            try:
                route_names = parse_qs(
                    urlsplit(str(candidate.url)).query
                ).get("n", [])
            except Exception:
                route_names = []
            if (
                route_names == ["打单拣货"]
                or self._page_has_visible_express_frame(candidate)
            ):
                unexpected.append(candidate)
        if unexpected:
            raise SafetyStop(
                "页面替换期间出现额外打单拣货页，旧页保持不动并停止"
            )

    def _rollback_failed_page_recovery(
        self,
        page_to_replace: Any,
        old_page: Any,
        old_context: Any,
    ) -> None:
        """Keep only a live, same-context, proven print page after rollback."""

        # Preserve the original context binding even when that context has
        # disappeared.  This prevents a later retry from silently adopting a
        # different signed-in BrowserContext.
        self.context = old_context
        self.page = None
        if old_context is None:
            return
        try:
            if not self.browser.is_connected():
                return
            contexts = list(self.browser.contexts)
        except Exception:
            return
        if len(contexts) != 1 or contexts[0] is not old_context:
            return
        candidates = (page_to_replace, old_page)
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                if candidate.context is not old_context or candidate.is_closed():
                    continue
            except Exception:
                continue
            # Re-prove every candidate at rollback time.  Even a page found by
            # _find_jst_page may navigate to inventory or login while the new
            # page is opening, so its earlier identity proof is not reusable.
            try:
                route_names = parse_qs(
                    urlsplit(str(candidate.url)).query
                ).get("n", [])
            except Exception:
                route_names = []
            if (
                route_names != ["打单拣货"]
                and not self._page_has_visible_express_frame(candidate)
            ):
                continue
            self.page = candidate
            return

    def _require_page_commit_ready(
        self,
        page: Any,
        expected_context: Any,
    ) -> None:
        """Fail cleanly unless a page is live in the one bound context."""

        ready = False
        try:
            contexts = list(self.browser.contexts)
            ready = bool(
                self.browser.is_connected()
                and expected_context is not None
                and len(contexts) == 1
                and contexts[0] is expected_context
                and page.context is expected_context
                and not page.is_closed()
            )
        except Exception:
            ready = False
        if not ready:
            raise OrderRowNotReady(
                "打单页在恢复提交前已关闭或登录上下文发生变化"
            )

    def recover_print_page(self, *, replace_page: bool = False) -> None:
        """Re-enter 打单拣货 in the current signed-in tab and prove readiness.

        The final bounded recovery may replace only this app's selected tab in
        the exact same BrowserContext. It never creates a context/profile,
        switches browser, or enters login credentials.
        """

        old_page = getattr(self, "page", None)
        old_context = getattr(self, "context", None)
        try:
            page = self._find_jst_page()
        except JSTPageMissing:
            page = None
        if (
            page is not None
            and old_context is not None
            and page.context is not old_context
        ):
            self._rollback_failed_page_recovery(
                page,
                old_page,
                old_context,
            )
            raise SafetyStop(
                "找到的打单页不属于原登录浏览器上下文（BrowserContext），"
                "禁止切换会话"
            )
        if replace_page:
            # Only a page independently proven by _find_jst_page may be
            # replaced.  A cached tab that has since navigated to inventory or
            # another ordinary epaas module is never closed or overwritten.
            page_to_replace = page
            try:
                replacement = self._open_jst_page_in_unique_context()
            except Exception:
                self._rollback_failed_page_recovery(
                    page_to_replace,
                    old_page,
                    old_context,
                )
                raise
            try:
                self._prove_replacement_topology(page_to_replace, replacement)
                self._require_page_commit_ready(
                    replacement,
                    old_context,
                )
            except Exception:
                try:
                    replacement.close()
                except Exception:
                    pass
                self._rollback_failed_page_recovery(
                    page_to_replace,
                    old_page,
                    old_context,
                )
                raise
            if page_to_replace is not None and page_to_replace is not replacement:
                try:
                    already_closed = page_to_replace.is_closed()
                except Exception:
                    already_closed = False
                if not already_closed:
                    try:
                        page_to_replace.close()
                    except Exception as exc:
                        try:
                            closed_after_error = page_to_replace.is_closed()
                        except Exception:
                            closed_after_error = False
                        if not closed_after_error:
                            try:
                                replacement.close()
                            except Exception:
                                pass
                            self._rollback_failed_page_recovery(
                                page_to_replace,
                                old_page,
                                old_context,
                            )
                            raise OrderRowNotReady(
                                "旧打单页无法安全关闭，禁止留下两个真实打单页"
                            ) from exc
            try:
                self._require_page_commit_ready(
                    replacement,
                    old_context,
                )
            except Exception:
                try:
                    replacement.close()
                except Exception:
                    pass
                self._rollback_failed_page_recovery(
                    page_to_replace,
                    old_page,
                    old_context,
                )
                raise
            # Every operation that can fail is complete.  Commit the cached
            # references only after the old page has closed successfully.
            self.page = replacement
            self.context = old_context
            return
        if page is None:
            try:
                replacement = self._open_jst_page_in_unique_context()
            except Exception:
                self._rollback_failed_page_recovery(
                    None,
                    old_page,
                    old_context,
                )
                raise
            try:
                self._prove_replacement_topology(None, replacement)
                self._require_page_commit_ready(
                    replacement,
                    old_context,
                )
                self.page = replacement
                self.context = old_context
                return
            except Exception:
                try:
                    replacement.close()
                except Exception:
                    pass
                self._rollback_failed_page_recovery(
                    None,
                    old_page,
                    old_context,
                )
                raise
        try:
            try:
                page.goto(
                    JST_HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
            except Exception as exc:
                raise OrderRowNotReady(
                    "重新进入打单拣货页超时或页签已关闭"
                ) from exc
            if not _is_jst_frame_path(page.url, "page"):
                raise OrderRowNotReady(
                    "重新进入打单拣货后离开了当前聚水潭 epaas 页面；"
                    "不会自动填写登录信息"
                )
            self.page = page
            self.context = page.context
            # This also proves one visible, attached expresssetter iframe and
            # the exact warehouse header before the worker may retry a job.
            self._express_frame()
            if self.page is not page:
                raise SafetyStop(
                    "页面恢复期间活动打单页发生切换，"
                    "禁止操作新出现的其他页签"
                )
            self._require_page_commit_ready(page, old_context)
        except Exception:
            self._rollback_failed_page_recovery(
                page,
                old_page,
                old_context,
            )
            raise

    def is_healthy(self) -> bool:
        """Return whether the cached CDP transport and page are still usable."""

        try:
            return bool(self.browser.is_connected()) and not self.page.is_closed()
        except Exception:
            return False

    def _verify_warehouse_context(self, page) -> None:
        """Prove that browser actions target the planner's warehouse page."""

        header = re.compile(
            rf"^\s*{re.escape(TARGET_WAREHOUSE_NAME)}\s*\[\s*[^\]]+\s*\]\s*$"
        )
        try:
            matches = self._visible(page.get_by_text(header))
        except Exception as exc:
            raise OrderRowNotReady(
                f"无法读取页面仓库标识，必须先确认{TARGET_WAREHOUSE_NAME}"
            ) from exc
        if len(matches) == 1:
            return
        if len(matches) > 1:
            raise SafetyStop(
                f"页面同时显示 {len(matches)} 个{TARGET_WAREHOUSE_NAME}标识，"
                "无法证明当前活动仓库"
            )
        raise SafetyStop(
            f"当前聚水潭页面未显示唯一的“{TARGET_WAREHOUSE_NAME} [ 操作员 ]”；"
            "已在查单前停止，禁止把错仓库的搜索结果误报为 DOM=0"
        )

    def _express_frame(self):
        deadline = time.time() + 12
        while time.time() < deadline:
            pages = [
                page
                for context in self.browser.contexts
                for page in context.pages
                if _is_jst_frame_path(page.url, "page")
            ]
            active_pages: list[tuple[Any, Any]] = []
            for page in pages:
                unique = self._visible_express_frames(page)
                if len(unique) == 1:
                    active_pages.append((page, unique[0]))
                if len(unique) > 1:
                    raise SafetyStop(
                        f"检测到 {len(unique)} 个可见的打单拣货 iframe，"
                        "无法证明当前活动页面"
                    )
            if len(active_pages) == 1:
                page, frame = active_pages[0]
                self._verify_warehouse_context(page)
                self.page = page
                return frame
            if len(active_pages) > 1:
                raise SafetyStop(
                    f"检测到 {len(active_pages)} 个真实打单拣货页面，"
                    "无法证明应操作哪一个"
                )
            time.sleep(0.4)
        raise OrderRowNotReady(
            "当前登录会话暂未呈现“打单拣货”iframe，需自动重进页面"
        )

    @staticmethod
    def _visible(locator):
        result = []
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    result.append(item)
            except Exception:
                continue
        return result

    def _order_input(self, frame):
        # The live order-picking page exposes the visible exact-search editor
        # with this stable id. Prefer it over descriptive attributes because a
        # hidden JTable column filter can carry the same title/placeholder.
        by_id = self._visible(frame.locator("#o_id"))
        if len(by_id) == 1:
            return by_id[0]
        if len(by_id) > 1:
            raise SafetyStop(
                f"可见的 #o_id 内部订单号搜索框数量为 {len(by_id)}，"
                "页面布局可能已变化"
            )
        direct = frame.locator(
            "input[placeholder*='内部订单号'], input[title*='内部订单号'], "
            "input[data-placeholder*='内部订单号']"
        )
        visible = self._visible(direct)
        if len(visible) == 1:
            return visible[0]
        if not visible:
            # During an epaas iframe replacement the old document can still
            # be addressable for a moment while its search form is already
            # gone.  This is a readiness failure, not a changed business rule
            # and not proof that the requested order is absent.
            raise OrderRowNotReady(
                "活动打单页面暂未呈现“内部订单号”搜索框，需重新定位 iframe"
            )
        raise SafetyStop(
            f"可见的“内部订单号”搜索框数量为 {len(visible)}，页面布局可能已变化"
        )

    def _dismiss_stale_carrier_dialog(self) -> bool:
        """Close one known leftover carrier picker before a new read-only search.

        The live epaas page can retain the reset-carrier iframe after a prior
        manual or interrupted operation.  That modal covers the search/reset
        buttons and can make an otherwise valid order look like DOM=0.  Only
        the close control belonging to one visible, known carrier-picker frame
        is allowed here.  One exactly classified shipped/printed warning may
        be cancelled (never confirmed) so it cannot block the next order.
        Unknown/multiple dialogs or an unprovable close control fail closed.
        """

        carrier_frames = []
        confirmation_frames = []
        for candidate in list(self.page.frames):
            try:
                if candidate == self.page.main_frame or candidate.is_detached():
                    continue
                frame_element = candidate.frame_element()
                if not frame_element.is_visible():
                    continue
                url = str(candidate.url or "").lower()
                if _is_jst_frame_path(url, "carrier"):
                    carrier_frames.append(candidate)
                    continue
                if _is_jst_frame_path(url, "confirmation") and self._visible(
                    candidate.locator("#confirm_confirm")
                ):
                    confirmation_frames.append(candidate)
            except Exception:
                continue
        if len(confirmation_frames) > 1:
            raise SafetyStop(
                f"查单前检测到 {len(confirmation_frames)} 个确认框，禁止猜测处理"
            )
        if confirmation_frames and carrier_frames:
            raise SafetyStop("查单前同时存在重设快递弹窗和确认框，禁止继续")
        if confirmation_frames:
            self._cancel_known_reset_warning(confirmation_frames[0], None)
            return True
        if len(carrier_frames) > 1:
            raise SafetyStop(
                f"查单前检测到 {len(carrier_frames)} 个可见的重设快递弹窗，"
                "无法证明应关闭哪一个"
            )
        if not carrier_frames:
            return False

        carrier_frame = carrier_frames[0]
        try:
            host = carrier_frame.frame_element()
            close_buttons = self._visible(
                host.locator(
                    "xpath=ancestor::div[contains(concat(' ', "
                    "normalize-space(@class), ' '), ' window ')][1]"
                    "//a[contains(concat(' ', normalize-space(@class), ' '), "
                    "' panel-tool-close ')]"
                )
            )
        except Exception as exc:
            raise SafetyStop("无法识别残留重设快递弹窗的关闭控件") from exc
        if len(close_buttons) != 1:
            raise SafetyStop(
                f"残留重设快递弹窗的关闭控件数量为 {len(close_buttons)}，"
                "禁止猜测点击"
            )
        close_button = close_buttons[0]
        self._prove_button_actionable(close_button, "关闭残留重设快递弹窗")
        close_button.click()

        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                if carrier_frame.is_detached() or not carrier_frame.frame_element().is_visible():
                    return True
            except Exception:
                return True
            time.sleep(0.1)
        raise SafetyStop("关闭残留重设快递弹窗后弹窗仍然可见，禁止继续")

    @staticmethod
    def _wait_grid_idle(
        frame, action: str, timeout: float = GRID_IDLE_TIMEOUT_SECONDS
    ) -> None:
        """Wait for EasyUI/jQuery search refreshes to settle before the next query."""

        deadline = time.time() + timeout
        started = time.time()
        quiet_since: Optional[float] = None
        last_state: dict[str, Any] = {"active": -1, "masks": -1}
        while time.time() < deadline:
            try:
                if frame.is_detached():
                    raise OrderRowNotReady(
                        f"{action}期间打单拣货 iframe 已刷新，需重新连接活动页面"
                    )
            except OrderRowNotReady:
                raise
            except Exception as exc:
                raise OrderRowNotReady(
                    f"{action}期间无法确认打单拣货 iframe 状态"
                ) from exc
            try:
                state = frame.evaluate(
                    """
                    () => {
                      const jq = window.jQuery || window.$;
                      const active = jq && Number.isFinite(Number(jq.active))
                        ? Number(jq.active) : 0;
                      const visible = (element) => {
                        if (!element) return false;
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden'
                          && rect.width > 0 && rect.height > 0;
                      };
                      const masks = Array.from(document.querySelectorAll(
                        '.datagrid-mask, .datagrid-mask-msg, .panel-loading, .wait'
                      )).filter(visible).length;
                      return {active, masks};
                    }
                    """
                )
                if not isinstance(state, dict):
                    raise ValueError("EasyUI 加载状态返回值无效")
                last_state = {
                    "active": int(state.get("active", 0) or 0),
                    "masks": int(state.get("masks", 0) or 0),
                }
                busy = last_state["active"] > 0 or last_state["masks"] > 0
            except Exception as exc:
                # A detached/reloading frame is not an idle grid. Treating an
                # evaluation failure as idle used to advance immediately into
                # row matching and report a misleading DOM=0.
                raise OrderRowNotReady(
                    f"{action}期间无法读取 EasyUI 加载状态，页面可能正在刷新"
                ) from exc
            now = time.time()
            if busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            # The click has returned before this loop starts, so a short quiet
            # window is enough to observe EasyUI's ajax/mask transition. Exact
            # row polling below remains the second readiness gate.
            if (
                quiet_since is not None
                and now - quiet_since >= GRID_IDLE_QUIET_SECONDS
                and now - started >= GRID_IDLE_QUIET_SECONDS
            ):
                return
            time.sleep(0.1)
        raise OrderRowNotReady(
            f"{action}后页面列表超过 {timeout:g} 秒仍未稳定"
            f"（未完成请求 {last_state['active']}，可见加载遮罩 "
            f"{last_state['masks']}），需重新连接活动 iframe 后重查"
        )

    def _reset_search_filters(self, frame):
        # SearchReset1/SearchReset is a span on the live page, not a semantic
        # button. Its class and handler remained stable across grid refreshes.
        self._dismiss_stale_carrier_dialog()
        self._wait_grid_idle(frame, "清空筛选前页面稳定")
        clear_button = self._unique_button(
            frame, ".btn_search_reset", "清空筛选"
        )
        self._prove_button_actionable(clear_button, "清空筛选")
        clear_button.click()
        deadline = time.time() + 3
        cleared = False
        while time.time() < deadline:
            search = self._order_input(frame)
            try:
                if not search.input_value().strip():
                    cleared = True
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not cleared:
            raise SafetyStop("清空筛选后内部订单号仍有旧值，禁止继续")
        self._wait_grid_idle(frame, "清空筛选")
        search = self._order_input(frame)
        if search.input_value().strip():
            raise SafetyStop("清空筛选刷新结束后内部订单号又出现旧值，禁止继续")
        return search

    @staticmethod
    def _sidebar_label_state(anchor, activate: bool) -> dict[str, Any]:
        """Resolve the all-orders radio and all carrier shortcuts in one scope."""

        result = anchor.evaluate(
            """
            (textElement, activate) => {
              const candidates = [];
              const add = (element) => {
                if (!element || element.tagName !== 'INPUT'
                    || String(element.type).toLowerCase() !== 'radio') return;
                if (!candidates.includes(element)) candidates.push(element);
              };
              add(textElement);
              const label = textElement.closest('label');
              if (label) {
                if (label.htmlFor) add(document.getElementById(label.htmlFor));
                label.querySelectorAll('input[type="radio"]').forEach(add);
              }
              if (candidates.length !== 1) {
                let node = textElement;
                for (let depth = 0; depth < 3 && node; depth += 1) {
                  if (node.previousElementSibling) add(node.previousElementSibling);
                  if (node.nextElementSibling) add(node.nextElementSibling);
                  if (node.parentElement) {
                    node.parentElement.querySelectorAll(
                      ':scope > input[type="radio"], :scope > label input[type="radio"]'
                    ).forEach(add);
                  }
                  node = node.parentElement;
                  if (candidates.length === 1) break;
                }
              }
              if (candidates.length !== 1) {
                return {ok: false, candidateCount: candidates.length};
              }
              const radio = candidates[0];
              const changed = !radio.checked;
              if (activate && changed) radio.click();
              let scope = radio;
              for (let depth = 0; depth < 12 && scope; depth += 1) {
                scope = scope.parentElement;
                if (!scope) break;
                const scopeText = String(scope.innerText || scope.textContent || '');
                const boxes = Array.from(
                  scope.querySelectorAll('input[type="checkbox"]')
                );
                if (!scopeText.includes('查询池') || boxes.length === 0) continue;
                return {
                  ok: true,
                  candidateCount: 1,
                  changed: Boolean(activate && changed),
                  checked: Boolean(radio.checked),
                  scopeFound: true,
                  checkedShortcutCount: boxes.filter((box) => box.checked).length,
                };
              }
              return {
                ok: true,
                candidateCount: 1,
                changed: Boolean(activate && changed),
                checked: Boolean(radio.checked),
                scopeFound: false,
                checkedShortcutCount: -1,
              };
            }
            """,
            bool(activate),
        )
        return result if isinstance(result, dict) else {"ok": False}

    def _reset_sidebar_scope(self, frame) -> None:
        """Select the page's all-orders scope before an exact order lookup.

        The carrier shortcuts in the left sidebar are independent from the
        top search form's ``清空`` button.  A warehouse operator can therefore
        leave (for example) ``中通速递-山东`` selected while the planner is
        looking for an order that is still under ``申通E物流-山东``.  Searching
        an exact internal order id in that stale scope returns a false zero-row
        result.  Reset the sidebar to the explicit all-orders radio and prove
        that the radio is checked before any order is selected.
        """

        scope_name = re.compile(r"^\s*-*\s*全部打单拣货单据\s*-*\s*$")

        # The live JST page exposes this control as one named radio. Prefer
        # that semantic identity over walking outward from a nearby text node:
        # wrapper changes must not leave a carrier shortcut selected and turn
        # an exact order search into a misleading zero-row result.
        radios = self._visible(frame.locator("#lc_id_1"))
        if not radios:
            radios = self._visible(frame.get_by_role("radio", name=scope_name))
        if len(radios) > 1:
            raise SafetyStop(
                f"可见的“全部打单拣货单据”单选框数量为 {len(radios)}，"
                "无法唯一清除左侧快递筛选"
            )
        if len(radios) == 1:
            radio = radios[0]
            try:
                changed = not radio.is_checked()
                if changed:
                    radio.check(force=True)
                if not radio.is_checked():
                    raise SafetyStop("“全部打单拣货单据”单选框未保持选中")
            except SafetyStop:
                raise
            except Exception as exc:
                raise SafetyStop(
                    "无法直接选中“全部打单拣货单据”，禁止带旧快递筛选查询"
                ) from exc
            if changed:
                self._wait_grid_idle(frame, "清除左侧快递筛选")
            verified = self._visible(frame.locator("#lc_id_1"))
            if not verified:
                verified = self._visible(frame.get_by_role("radio", name=scope_name))
            try:
                checked = len(verified) == 1 and verified[0].is_checked()
            except Exception:
                checked = False
            if not checked:
                raise SafetyStop(
                    "列表刷新后“全部打单拣货单据”不再选中，禁止继续查询"
                )
            try:
                shortcut_state = self._sidebar_label_state(verified[0], False)
            except Exception as exc:
                raise SafetyStop(
                    "无法复核左侧快递快捷筛选，禁止继续订单查询"
                ) from exc
            if not isinstance(shortcut_state, dict) or not shortcut_state.get(
                "scopeFound"
            ):
                raise SafetyStop("无法界定左侧快递筛选区域，禁止继续订单查询")
            if shortcut_state.get("checkedShortcutCount") != 0:
                raise SafetyStop(
                    "选择全部订单后仍有快递快捷筛选处于勾选状态，禁止产生假 DOM=0"
                )
            return

        labels = self._visible(frame.get_by_text(scope_name))
        if len(labels) != 1:
            raise SafetyStop(
                f"可见的“全部打单拣货单据”范围入口数量为 {len(labels)}，"
                "无法清除左侧快递筛选"
            )
        try:
            state = self._sidebar_label_state(labels[0], True)
        except Exception as exc:
            raise SafetyStop("无法复核左侧打单范围，禁止带旧快递筛选查询") from exc
        if not isinstance(state, dict) or not state.get("ok") or not state.get(
            "checked"
        ):
            count = state.get("candidateCount", 0) if isinstance(state, dict) else 0
            raise SafetyStop(
                f"“全部打单拣货单据”未能唯一选中（单选框数量 {count}），禁止继续"
            )
        if state.get("changed"):
            self._wait_grid_idle(frame, "清除左侧快递筛选")
        refreshed = self._visible(frame.get_by_text(scope_name))
        if len(refreshed) != 1:
            raise OrderRowNotReady(
                "清除左侧快递筛选后全部订单入口正在刷新，需重新定位"
            )
        try:
            state = self._sidebar_label_state(refreshed[0], False)
        except Exception as exc:
            raise OrderRowNotReady(
                "清除左侧快递筛选后无法回读当前范围，需重新定位"
            ) from exc
        if not state.get("ok") or not state.get("checked"):
            raise SafetyStop("刷新后全部订单范围未保持选中，禁止继续查询")
        if not state.get("scopeFound"):
            raise SafetyStop("无法界定左侧快递筛选区域，禁止继续订单查询")
        if state.get("checkedShortcutCount") != 0:
            raise SafetyStop(
                "选择全部订单后仍有快递快捷筛选处于勾选状态，禁止产生假 DOM=0"
            )

    @staticmethod
    def _row_identity(row) -> Optional[tuple[str, str]]:
        for attribute in (
            "datagrid-row-index",
            "data-row-index",
            "aria-rowindex",
            "data-index",
        ):
            try:
                value = row.get_attribute(attribute)
            except Exception:
                continue
            if value not in (None, ""):
                return attribute, str(value)
        try:
            class_name = str(row.get_attribute("class") or "")
            index = row.get_attribute("index")
        except Exception:
            class_name, index = "", None
        if "_jt_row" in class_name.split() and index not in (None, ""):
            return "jtable-index", str(index)
        return None

    @staticmethod
    def _logical_row_key(row, fallback_index: int) -> tuple[str, str]:
        try:
            class_name = str(row.get_attribute("class") or "")
            jtable_index = row.get_attribute("index")
        except Exception:
            class_name, jtable_index = "", None
        if "_jt_row" in class_name.split() and jtable_index not in (None, ""):
            return "jtable", str(jtable_index)
        try:
            row_id = str(row.get_attribute("id") or "")
        except Exception:
            row_id = ""
        easyui = re.match(r"^(datagrid-row-[^-]+)-\d+-(\d+)$", row_id)
        if easyui:
            return "easyui", f"{easyui.group(1)}:{easyui.group(2)}"
        return "visible", str(fallback_index)

    def _row_checkbox(self, frame, row, identity: Optional[tuple[str, str]]):
        if identity and identity[0] == "jtable-index":
            boxes = self._visible(
                row.locator(
                    "._jt_cell_checked[data-id='checked'] "
                    "input._jt_cbx[type='checkbox']"
                )
            )
            if len(boxes) != 1:
                raise SafetyStop(
                    f"JTable 目标订单选择框数量为 {len(boxes)}，禁止继续"
                )
            return boxes[0]
        direct = self._visible(row.locator("input[type='checkbox']"))
        if len(direct) == 1:
            return direct[0]
        if len(direct) > 1:
            raise SafetyStop("订单行内出现多个可见勾选框，禁止继续")
        if not identity:
            return None

        attribute, expected = identity
        scopes = []
        try:
            grid = row.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
                "' datagrid-view ')][1]"
            )
            if grid.count():
                scopes.append(grid)
        except Exception:
            pass
        scopes.append(frame)

        for scope in scopes:
            mirrored_checkboxes = []
            try:
                possible_rows = self._visible(scope.locator(f"tr[{attribute}]"))
            except Exception:
                continue
            for possible_row in possible_rows:
                try:
                    if str(possible_row.get_attribute(attribute) or "") != expected:
                        continue
                except Exception:
                    continue
                mirrored_checkboxes.extend(
                    self._visible(possible_row.locator("input[type='checkbox']"))
                )
            if len(mirrored_checkboxes) == 1:
                return mirrored_checkboxes[0]
            if len(mirrored_checkboxes) > 1:
                raise SafetyStop("同一订单行映射到多个可见勾选框，禁止继续")
        return None

    @staticmethod
    def _selection_scope(frame, row):
        try:
            jtable = row.locator("xpath=ancestor::*[@id='_jt_body_list'][1]")
            if jtable.count() == 1:
                return jtable
        except Exception:
            pass
        try:
            grid = row.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
                "' datagrid-view ')][1]"
            )
            if grid.count() == 1:
                return grid
        except Exception:
            pass
        return frame

    def _checked_data_checkboxes(self, scope):
        try:
            checked = scope.locator(
                ".datagrid-body input[type='checkbox']:checked, "
                "tbody tr:not(.datagrid-header-row) input[type='checkbox']:checked, "
                "._jt_row._jt_rh ._jt_cell_checked[data-id='checked'] "
                "input._jt_cbx[type='checkbox']:checked"
            )
            return [checked.nth(index) for index in range(checked.count())]
        except Exception as exc:
            raise SafetyStop("无法核对列表勾选状态，禁止继续") from exc

    @staticmethod
    def _easyui_selection_state(scope, target_index: str, action: str):
        if action not in {"clear", "check", "read"}:
            raise ValueError(f"不支持的列表选择动作：{action}")
        try:
            result = scope.evaluate(
                """
                (view, args) => {
                  const jq = window.jQuery || window.$;
                  if (!jq || !jq.fn || typeof jq.fn.datagrid !== 'function') {
                    return {ok: false, error: 'EasyUI datagrid API unavailable'};
                  }
                  const candidates = Array.from(new Set(
                    document.querySelectorAll('table.datagrid-f, table.easyui-datagrid')
                  ));
                  const matches = [];
                  for (const table of candidates) {
                    try {
                      const panel = jq(table).datagrid('getPanel');
                      const panelNode = panel && panel[0];
                      if (panelNode && panelNode.contains(view)) matches.push(table);
                    } catch (_) {}
                  }
                  if (matches.length !== 1) {
                    return {ok: false, error: `matched ${matches.length} datagrids`};
                  }
                  try {
                    const grid = jq(matches[0]);
                    if (args.action === 'clear') {
                      grid.datagrid('uncheckAll');
                      grid.datagrid('clearChecked');
                      grid.datagrid('clearSelections');
                    } else if (args.action === 'check') {
                      grid.datagrid('checkRow', Number(args.targetIndex));
                    }
                    const rowIndex = (row) => String(grid.datagrid('getRowIndex', row));
                    const checked = (grid.datagrid('getChecked') || []).map(rowIndex);
                    const selected = (grid.datagrid('getSelections') || []).map(rowIndex);
                    return {ok: true, checked_indices: checked, selected_indices: selected};
                  } catch (error) {
                    return {ok: false, error: String(error)};
                  }
                }
                """,
                {"action": action, "targetIndex": str(target_index)},
            )
        except Exception as exc:
            raise SafetyStop("无法读取聚水潭列表真实勾选状态，禁止继续") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            detail = result.get("error") if isinstance(result, dict) else "unknown"
            raise SafetyStop(f"聚水潭列表选择接口不可用（{detail}），禁止继续")
        return {
            "checked_indices": [str(value) for value in result.get("checked_indices") or []],
            "selected_indices": [str(value) for value in result.get("selected_indices") or []],
        }

    @staticmethod
    def _easyui_batch_selection_state(
        scope, target_indices: tuple[str, ...], action: str
    ) -> dict[str, list[str]]:
        if action not in {"clear", "check", "read"}:
            raise ValueError(f"不支持的批量列表选择动作：{action}")
        if any(not str(index).isdigit() for index in target_indices):
            raise SafetyStop("批量打印行索引无效，禁止继续")
        try:
            result = scope.evaluate(
                """
                (view, args) => {
                  const jq = window.jQuery || window.$;
                  if (!jq || !jq.fn || typeof jq.fn.datagrid !== 'function') {
                    return {ok: false, error: 'EasyUI datagrid API unavailable'};
                  }
                  const candidates = Array.from(new Set(
                    document.querySelectorAll('table.datagrid-f, table.easyui-datagrid')
                  ));
                  const matches = [];
                  for (const table of candidates) {
                    try {
                      const panel = jq(table).datagrid('getPanel');
                      const node = panel && panel[0];
                      if (node && node.contains(view)) matches.push(table);
                    } catch (_) {}
                  }
                  if (matches.length !== 1) {
                    return {ok: false, error: `matched ${matches.length} datagrids`};
                  }
                  try {
                    const grid = jq(matches[0]);
                    if (args.action === 'clear' || args.action === 'check') {
                      grid.datagrid('uncheckAll');
                      grid.datagrid('clearChecked');
                      grid.datagrid('clearSelections');
                    }
                    if (args.action === 'check') {
                      for (const index of args.targetIndices) {
                        grid.datagrid('checkRow', Number(index));
                      }
                    }
                    const rowIndex = (row) => String(grid.datagrid('getRowIndex', row));
                    return {
                      ok: true,
                      checked_indices: (grid.datagrid('getChecked') || []).map(rowIndex),
                      selected_indices: (grid.datagrid('getSelections') || []).map(rowIndex),
                    };
                  } catch (error) {
                    return {ok: false, error: String(error)};
                  }
                }
                """,
                {"action": action, "targetIndices": list(target_indices)},
            )
        except Exception as exc:
            raise SafetyStop("无法读取聚水潭批量勾选状态，禁止继续") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            detail = result.get("error") if isinstance(result, dict) else "unknown"
            raise SafetyStop(f"聚水潭批量选择接口不可用（{detail}），禁止继续")
        return {
            "checked_indices": [str(value) for value in result.get("checked_indices") or []],
            "selected_indices": [str(value) for value in result.get("selected_indices") or []],
        }

    def _jtable_selection_state(
        self, scope, target_indices: tuple[str, ...], action: str
    ) -> dict[str, list[str]]:
        """Mutate and prove only JTable's dedicated order-selection boxes."""

        if action not in {"clear", "check", "read"}:
            raise ValueError(f"不支持的 JTable 选择动作：{action}")
        if any(not str(index).isdigit() for index in target_indices):
            raise SafetyStop("JTable 目标行索引无效，禁止继续")
        rows = self._visible(scope.locator("._jt_row._jt_rh[index]"))
        seen_indices: set[str] = set()
        for row in rows:
            identity = self._row_identity(row)
            if not identity or identity[0] != "jtable-index":
                continue
            index = identity[1]
            if index in seen_indices:
                raise SafetyStop(f"JTable 行索引 {index} 重复，禁止继续")
            seen_indices.add(index)

        def current_checkbox(index: str):
            # JTable redraws a row after a checkbox event. Resolve the current
            # node from the stable body on every mutation instead of keeping a
            # locator scoped under the now-detached old row.
            selector = (
                f"._jt_row._jt_rh[index='{index}'] "
                "._jt_cell_checked[data-id='checked'] "
                "input._jt_cbx[type='checkbox']"
            )
            boxes = self._visible(scope.locator(selector))
            if len(boxes) != 1:
                raise SafetyStop(
                    f"JTable 行 {index} 当前选择框数量为 {len(boxes)}，禁止继续"
                )
            return boxes[0]

        if action in {"clear", "check"}:
            for index in sorted(seen_indices, key=int):
                try:
                    checkbox = current_checkbox(index)
                    if checkbox.is_checked():
                        checkbox.uncheck(force=True)
                except Exception as exc:
                    raise SafetyStop("无法清除 JTable 旧订单勾选，禁止继续") from exc
        if action == "check":
            for target_index in target_indices:
                target_index = str(target_index)
                if target_index not in seen_indices:
                    raise SafetyStop(
                        f"JTable 中缺少目标行 {target_index}，禁止继续"
                    )
                try:
                    current_checkbox(target_index).check(force=True)
                except Exception as exc:
                    raise SafetyStop("无法勾选 JTable 目标订单，禁止继续") from exc
        checked_indices: list[str] = []
        for index in sorted(seen_indices, key=int):
            try:
                checkbox = current_checkbox(index)
                if checkbox.is_checked():
                    checked_indices.append(index)
            except Exception as exc:
                raise SafetyStop("无法读取 JTable 真实勾选状态，禁止继续") from exc
        try:
            all_checked = scope.locator(
                "._jt_row._jt_rh ._jt_cell_checked[data-id='checked'] "
                "input._jt_cbx[type='checkbox']:checked"
            ).count()
        except Exception as exc:
            raise SafetyStop("无法读取 JTable 全局勾选集合，禁止继续") from exc
        if all_checked != len(checked_indices):
            raise SafetyStop(
                "JTable 存在隐藏或无法映射的旧勾选，禁止把批次外订单带入操作"
            )
        return {"checked_indices": checked_indices, "selected_indices": []}

    def _grid_selection_state(
        self,
        scope,
        target_indices: tuple[str, ...],
        action: str,
        grid_kind: str,
    ) -> dict[str, list[str]]:
        if grid_kind == "jtable":
            return self._jtable_selection_state(scope, target_indices, action)
        if grid_kind != "easyui":
            raise SafetyStop(f"不认识的聚水潭表格类型：{grid_kind}")
        if len(target_indices) == 1:
            return self._easyui_selection_state(
                scope, target_indices[0], action
            )
        return self._easyui_batch_selection_state(scope, target_indices, action)

    @staticmethod
    def _row_text(row) -> str:
        for method_name in ("inner_text", "text_content"):
            method = getattr(row, method_name, None)
            if callable(method):
                try:
                    return str(method() or "")
                except Exception:
                    continue
        return ""

    def _find_exact_jtable_row(
        self,
        frame,
        o_id: str,
        io_id: str,
        outbound_identity_unique: bool,
    ):
        """Resolve the current JST ``_jt_*`` grid by its two identity cells.

        The live expresssetter page no longer necessarily renders an EasyUI
        datagrid.  Its JTable rows are ``div._jt_row[index]`` elements and the
        displayed internal order can contain a non-numeric badge such as
        ``复``.  Match numeric tokens only inside the dedicated identity cells;
        never scan arbitrary row values for an order number.
        """

        try:
            rows = self._visible(frame.locator("._jt_row._jt_rh[index]"))
        except Exception:
            self._last_jtable_lookup_summary = "JTable 订单行读取失败"
            return None
        o_pattern = re.compile(rf"(?<!\d){re.escape(str(o_id))}(?!\d)")
        io_pattern = re.compile(rf"(?<!\d){re.escape(str(io_id))}(?!\d)")
        internal_matches: list[tuple[Any, Any]] = []
        pair_matches: list[tuple[Any, Any]] = []
        for row in rows:
            try:
                o_cells = self._visible(
                    row.locator("._jt_cell_o_id[data-id='o_id']")
                )
                io_cells = self._visible(
                    row.locator("._jt_cell_io_id[data-id='io_id']")
                )
                if len(o_cells) != 1 or len(io_cells) != 1:
                    continue
                if not o_pattern.search(self._row_text(o_cells[0])):
                    continue
                identity = self._row_identity(row)
                checkbox = self._row_checkbox(frame, row, identity)
                internal_matches.append((row, checkbox))
                if io_pattern.search(self._row_text(io_cells[0])):
                    pair_matches.append((row, checkbox))
            except SafetyStop:
                raise
            except Exception:
                continue
        self._last_jtable_lookup_summary = (
            f"JTable数据行{len(rows)}，内部号命中{len(internal_matches)}，"
            f"双号命中{len(pair_matches)}"
        )
        if len(pair_matches) > 1:
            raise SafetyStop(
                f"内部订单 {o_id} / 出库单 {io_id} 在 JTable 匹配到多行，禁止继续"
            )
        if len(pair_matches) == 1:
            return pair_matches[0]
        if outbound_identity_unique is True and len(internal_matches) == 1:
            return internal_matches[0]
        return None

    def _find_exact_anchor_row(
        self,
        frame,
        o_id: str,
        io_id: str,
        outbound_identity_unique: bool,
    ):
        """Resolve the live row from JST's exact internal-order link.

        The production ``expresssetter`` grid renders the order number in the
        right-hand EasyUI body and the checkbox in a mirrored left-hand row.
        Looking up the exact link first avoids a full-table ``tr`` text scan,
        which can return no logical rows while the order is visibly present.
        The backend uniqueness proof is still required when the page does not
        expose ``io_id`` in the row.
        """

        # JST sometimes wraps the visible order number with whitespace inside
        # the link. Accept only surrounding whitespace; the numeric identity
        # itself must still match exactly.
        exact_pattern = re.compile(rf"^\s*{re.escape(str(o_id))}\s*$")
        try:
            links = self._visible(
                frame.locator(".datagrid-body a").filter(has_text=exact_pattern)
            )
        except Exception:
            self._last_anchor_lookup_summary = "订单号链接读取失败"
            return None

        resolved: dict[tuple[str, str], tuple[Any, Any, str]] = {}
        for fallback_index, link in enumerate(links):
            try:
                if str(link.inner_text() or "").strip() != str(o_id):
                    continue
                parent_rows = self._visible(link.locator("xpath=ancestor::tr[1]"))
            except Exception:
                continue
            if len(parent_rows) != 1:
                continue
            row = parent_rows[0]
            identity = self._row_identity(row)
            if (
                not identity
                or identity[0] != "datagrid-row-index"
                or not identity[1].isdigit()
            ):
                continue
            checkbox = self._row_checkbox(frame, row, identity)
            resolved.setdefault(
                self._logical_row_key(row, fallback_index),
                (row, checkbox, self._row_text(row)),
            )

        self._last_anchor_lookup_summary = (
            f"精确订单号链接{len(links)}个，逻辑行{len(resolved)}个"
        )
        if len(resolved) != 1:
            return None

        row, checkbox, text = next(iter(resolved.values()))
        io_pattern = re.compile(rf"(?<!\d){re.escape(str(io_id))}(?!\d)")
        if io_pattern.search(text) or outbound_identity_unique is True:
            return row, checkbox
        return None

    @staticmethod
    def _easyui_result_indices(view, o_id: str, io_id: str) -> dict[str, Any]:
        """Read the live EasyUI model when its split DOM cannot map text to checkbox."""

        try:
            result = view.evaluate(
                """
                (view, args) => {
                  const jq = window.jQuery || window.$;
                  if (!jq || !jq.fn || typeof jq.fn.datagrid !== 'function') {
                    return {ok: false, error: 'EasyUI datagrid API unavailable'};
                  }
                  const tables = Array.from(new Set(
                    document.querySelectorAll('table.datagrid-f, table.easyui-datagrid')
                  ));
                  const matches = [];
                  for (const table of tables) {
                    try {
                      const panel = jq(table).datagrid('getPanel');
                      const panelNode = panel && panel[0];
                      if (panelNode && panelNode.contains(view)) matches.push(table);
                    } catch (_) {}
                  }
                  if (matches.length !== 1) {
                    return {ok: false, error: `matched ${matches.length} datagrids`};
                  }
                  const grid = jq(matches[0]);
                  const rows = grid.datagrid('getRows') || [];
                  const normalizedKey = (key) => String(key)
                    .replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
                  const plainValue = (value) => {
                    if (value === null || value === undefined) return '';
                    if (typeof value !== 'string' && typeof value !== 'number') return '';
                    const source = String(value).trim();
                    if (!source.includes('<')) return source;
                    // Values are untrusted row-model data. Never send them
                    // through an HTML parser or executable DOM sink.
                    return source.replace(/<[^>]*>/g, '').trim();
                  };
                  const oIndices = [];
                  const pairIndices = [];
                  let oFieldRows = 0;
                  let ioFieldRows = 0;
                  rows.forEach((row, index) => {
                    const oValues = [];
                    const ioValues = [];
                    for (const [rawKey, rawValue] of Object.entries(row || {})) {
                      const key = normalizedKey(rawKey);
                      if (key === 'oid') oValues.push(plainValue(rawValue));
                      if (key === 'ioid') ioValues.push(plainValue(rawValue));
                    }
                    if (oValues.length) oFieldRows += 1;
                    if (ioValues.length) ioFieldRows += 1;
                    const hasOId = oValues.some((value) => value === String(args.oId));
                    const hasIoId = ioValues.some((value) => value === String(args.ioId));
                    if (hasOId) {
                      oIndices.push(String(index));
                      if (hasIoId) pairIndices.push(String(index));
                    }
                  });
                  return {
                    ok: true,
                    row_count: rows.length,
                    o_field_rows: oFieldRows,
                    io_field_rows: ioFieldRows,
                    o_indices: oIndices,
                    pair_indices: pairIndices,
                  };
                }
                """,
                {"oId": str(o_id), "ioId": str(io_id)},
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return result if isinstance(result, dict) else {"ok": False}

    def _find_exact_easyui_row(
        self,
        frame,
        o_id: str,
        io_id: str,
        outbound_identity_unique: bool,
    ):
        """Resolve one result through EasyUI's data model without weakening identity gates."""

        try:
            views = self._visible(frame.locator(".datagrid-view"))
        except Exception:
            self._last_easyui_lookup_summary = "EasyUI 表格视图读取失败"
            return None
        exact: list[tuple[Any, str]] = []
        internal_only: list[tuple[Any, str]] = []
        row_counts: list[int] = []
        errors: list[str] = []
        for view in views:
            state = self._easyui_result_indices(view, o_id, io_id)
            if not state.get("ok"):
                errors.append(str(state.get("error") or "unknown"))
                continue
            try:
                row_counts.append(int(state.get("row_count", 0)))
            except (TypeError, ValueError):
                row_counts.append(0)
            for index in state.get("pair_indices") or []:
                exact.append((view, str(index)))
            for index in state.get("o_indices") or []:
                internal_only.append((view, str(index)))

        self._last_easyui_lookup_summary = (
            f"EasyUI视图{len(views)}个，数据行{max(row_counts, default=0)}，"
            f"内部号命中{len(internal_only)}，双号命中{len(exact)}"
        )
        if errors and not row_counts:
            self._last_easyui_lookup_summary += f"，不可读视图{len(errors)}个"

        matches = exact
        if not matches and outbound_identity_unique is True:
            matches = internal_only
        if len(matches) != 1:
            return None

        view, target_index = matches[0]
        possible_rows = self._visible(
            view.locator(f"tr[datagrid-row-index='{target_index}']")
        )
        resolved: dict[tuple[str, str], tuple[Any, Any]] = {}
        for fallback_index, row in enumerate(possible_rows):
            identity = self._row_identity(row)
            checkbox = self._row_checkbox(frame, row, identity)
            resolved.setdefault(
                self._logical_row_key(row, fallback_index), (row, checkbox)
            )
        if len(resolved) == 1:
            return next(iter(resolved.values()))
        return None

    def _find_exact_row(
        self,
        frame,
        o_id: str,
        io_id: str,
        outbound_identity_unique: bool = False,
    ):
        if not _is_ascii_order_id(o_id) or not _is_ascii_order_id(io_id):
            raise SafetyStop("页面筛选缺少有效内部订单号或出库单号")
        try:
            if frame.is_detached():
                raise OrderRowNotReady("打单拣货 iframe 已刷新，需重新定位活动页面")
        except OrderRowNotReady:
            raise
        except Exception as exc:
            raise OrderRowNotReady("无法确认打单拣货 iframe 是否仍有效") from exc
        jtable_match = self._find_exact_jtable_row(
            frame, o_id, io_id, outbound_identity_unique
        )
        if jtable_match is not None:
            return jtable_match
        anchor_match = self._find_exact_anchor_row(
            frame, o_id, io_id, outbound_identity_unique
        )
        if anchor_match is not None:
            return anchor_match
        pattern = re.compile(rf"(?<!\d){re.escape(o_id)}(?!\d)")
        io_pattern = re.compile(rf"(?<!\d){re.escape(io_id)}(?!\d)")
        try:
            rows = frame.locator(".datagrid-body tr").filter(has_text=pattern)
            candidates = {}
            for index, row in enumerate(self._visible(rows)):
                identity = self._row_identity(row)
                checkbox = self._row_checkbox(frame, row, identity)
                key = self._logical_row_key(row, index)
                candidates.setdefault(key, (row, checkbox, self._row_text(row)))
        except SafetyStop:
            raise
        except Exception as exc:
            # A frame can be replaced between the initial is_detached() check
            # and the locator count/text reads below.  Do not let that race
            # escape as an unexpected exception or a fabricated zero-row
            # result; the worker will reconnect and repeat the read-only query.
            raise OrderRowNotReady(
                "读取聚水潭订单行期间活动 iframe 已刷新或暂不可访问"
            ) from exc
        pair_matches = [
            (row, checkbox)
            for row, checkbox, text in candidates.values()
            if io_pattern.search(text)
        ]
        if len(pair_matches) > 1:
            raise SafetyStop(f"内部订单 {o_id} / 出库单 {io_id} 匹配到多行，禁止继续")
        easyui_match = self._find_exact_easyui_row(
            frame, o_id, io_id, outbound_identity_unique
        )
        if easyui_match is not None:
            return easyui_match
        if len(pair_matches) == 1:
            row, checkbox = pair_matches[0]
            identity = self._row_identity(row)
            if identity and identity[0] == "datagrid-row-index":
                return row, checkbox
        if outbound_identity_unique is True and len(candidates) == 1:
            row, checkbox, _text = next(iter(candidates.values()))
            identity = self._row_identity(row)
            if identity and identity[0] == "datagrid-row-index":
                return row, checkbox
        diagnostic = str(
            getattr(self, "_last_easyui_lookup_summary", "EasyUI表格未返回诊断")
        )
        anchor_diagnostic = str(
            getattr(self, "_last_anchor_lookup_summary", "订单号链接未返回诊断")
        )
        jtable_diagnostic = str(
            getattr(self, "_last_jtable_lookup_summary", "JTable未返回诊断")
        )
        message = (
            f"内部订单 {o_id} 匹配到 {len(candidates)} 个DOM逻辑行，但页面无法精确证明"
            f"出库单 {io_id}（{jtable_diagnostic}；{anchor_diagnostic}；"
            f"{diagnostic}），禁止继续"
        )
        if not candidates:
            raise OrderRowNotReady(message)
        # A non-empty but unprovable page row is a browser/page-environment
        # failure, not an order-level business conflict. It must pause without
        # enabling the operator's permanent global skip action.
        raise SafetyStop(message)

    def _wait_exact_row(
        self,
        frame,
        o_id: str,
        io_id: str,
        outbound_identity_unique: bool = False,
        timeout: float = EXACT_ROW_TIMEOUT_SECONDS,
    ):
        deadline = time.time() + timeout
        last_error: Optional[SafetyStop] = None
        while time.time() < deadline:
            try:
                return self._find_exact_row(
                    frame, o_id, io_id, outbound_identity_unique
                )
            except OrderRowNotReady as exc:
                last_error = exc
            time.sleep(0.35)
        raise last_error or SafetyStop(f"订单 {o_id} 搜索超时")

    @staticmethod
    def _sync_easyui_search_value(search, value: str) -> None:
        """Set both the visible editor and EasyUI's hidden textbox value."""

        search.fill(value)
        try:
            result = search.evaluate(
                """
                (visibleInput, nextValue) => {
                  const jq = window.jQuery || window.$;
                  const wrapper = visibleInput.closest('span.textbox');
                  const original = wrapper && wrapper.previousElementSibling;
                  if (!jq || !original || !jq.fn || typeof jq.fn.textbox !== 'function') {
                    return {used: false};
                  }
                  try {
                    jq(original).textbox('setValue', String(nextValue));
                    return {
                      used: true,
                      hiddenValue: String(jq(original).textbox('getValue') || ''),
                      visibleValue: String(visibleInput.value || ''),
                    };
                  } catch (error) {
                    return {used: false, error: String(error)};
                  }
                }
                """,
                str(value),
            )
        except Exception:
            result = {"used": False}
        if isinstance(result, dict) and result.get("used"):
            if str(result.get("hiddenValue", "")).strip() != str(value):
                raise SafetyStop("内部订单号未同步到聚水潭 EasyUI 搜索值")

    def _submit_order_search(self, frame, search, value: str) -> None:
        self._sync_easyui_search_value(search, value)
        if search.input_value().strip() != value:
            raise SafetyStop("内部订单号未能正确写入搜索框")
        button = self._unique_button(frame, ".btn_search", "搜索订单")
        self._prove_button_actionable(button, "搜索订单")
        button.click()
        self._wait_grid_idle(
            frame, "订单搜索", timeout=GRID_SEARCH_IDLE_TIMEOUT_SECONDS
        )
        if search.input_value().strip() != value:
            raise SafetyStop("订单搜索刷新后内部订单号被页面覆盖，禁止继续")

    def select_order(
        self, o_id: str, io_id: str, outbound_identity_unique: bool = False
    ):
        if type(outbound_identity_unique) is not bool:
            raise SafetyStop("后台出库身份唯一性凭据格式错误")
        frame = self._express_frame()
        self._reset_sidebar_scope(frame)
        # Always narrow the server-backed grid before walking its DOM. The live
        # page can contain 2,000 JTable rows; probing every row over CDP before
        # an exact search made one lookup take tens of seconds and enlarged the
        # iframe-redraw race that surfaced as misleading DOM=0 failures.
        search = self._reset_search_filters(frame)
        self._submit_order_search(frame, search, str(o_id))
        row, checkbox = self._wait_exact_row(
            frame, o_id, io_id, outbound_identity_unique
        )
        identity = self._row_identity(row)
        if (
            not identity
            or identity[0] not in {"datagrid-row-index", "jtable-index"}
            or not identity[1].isdigit()
        ):
            raise SafetyStop("无法取得目标订单的聚水潭列表行索引，禁止继续")
        target_index = identity[1]
        grid_kind = "jtable" if identity[0] == "jtable-index" else "easyui"
        selection_scope = self._selection_scope(frame, row)
        cleared = self._grid_selection_state(
            selection_scope, (target_index,), "clear", grid_kind
        )
        if cleared["checked_indices"] or cleared["selected_indices"]:
            raise SafetyStop("聚水潭列表残留勾选未能清除，禁止继续")
        selected_before = self._checked_data_checkboxes(selection_scope)
        if selected_before:
            raise SafetyStop(
                f"列表中仍有 {len(selected_before)} 个订单被勾选，禁止继续"
            )
        selected_state = self._grid_selection_state(
            selection_scope, (target_index,), "check", grid_kind
        )
        # JTable replaces a row after its checkbox event. `checkbox` belongs to
        # the pre-redraw row and is deliberately not read again; the stable body
        # state above reacquires the current checkbox by row index.
        selected_after = self._checked_data_checkboxes(selection_scope)
        if checkbox is not None and len(selected_after) != 1:
            raise SafetyStop(
                f"勾选目标后列表共有 {len(selected_after)} 个订单被选中，禁止批量操作"
            )
        if checkbox is None and len(selected_after) > 1:
            raise SafetyStop(
                f"自定义选择控件旁仍检测到 {len(selected_after)} 个标准勾选框，禁止批量操作"
            )
        if selected_state["checked_indices"] != [target_index]:
            raise SafetyStop("聚水潭真实勾选行不是当前目标订单，禁止继续")
        if any(
            selected_index != target_index
            for selected_index in selected_state["selected_indices"]
        ):
            raise SafetyStop("聚水潭列表仍选中了其他订单，禁止继续")
        return SelectedOrder(
            frame,
            checkbox,
            target_index,
            selection_scope,
            grid_kind,
            str(o_id),
            str(io_id),
        )

    def _verify_selected_identity(self, selected: SelectedOrder | SelectedBatch) -> None:
        """Prove that every saved index still owns its approved composite identity."""

        if isinstance(selected, SelectedOrder):
            identities = ((selected.o_id, selected.io_id),)
            indices = (selected.target_index,)
        elif isinstance(selected, SelectedBatch):
            identities = selected.identities
            indices = selected.target_indices
        else:
            raise SafetyStop("无法识别列表选择凭据，禁止继续")
        if len(identities) != len(indices):
            raise SafetyStop("列表行索引与订单身份数量不一致")

        is_batch = isinstance(selected, SelectedBatch)
        for target_index, (o_id, io_id) in zip(indices, identities):
            try:
                self._verify_saved_row_identity(
                    selected, str(target_index), str(o_id), str(io_id)
                )
            except SafetyStop as exc:
                if is_batch:
                    raise BatchCandidateChanged(
                        {"o_id": str(o_id), "io_id": str(io_id)}, str(exc)
                    ) from exc
                raise

    def _verify_saved_row_identity(
        self,
        selected: SelectedOrder | SelectedBatch,
        target_index: str,
        o_id: str,
        io_id: str,
    ) -> None:
        if selected.grid_kind == "jtable":
            rows = self._visible(
                selected.selection_scope.locator(
                    f"._jt_row._jt_rh[index='{target_index}']"
                )
            )
            if len(rows) != 1:
                raise OrderRowNotReady(
                    f"JTable 行 {target_index} 在点击前已刷新，需重新查单"
                )
            row = rows[0]
            o_cells = self._visible(
                row.locator("._jt_cell_o_id[data-id='o_id']")
            )
            io_cells = self._visible(
                row.locator("._jt_cell_io_id[data-id='io_id']")
            )
            o_pattern = re.compile(rf"(?<!\d){re.escape(o_id)}(?!\d)")
            io_pattern = re.compile(rf"(?<!\d){re.escape(io_id)}(?!\d)")
            if (
                len(o_cells) != 1
                or len(io_cells) != 1
                or not o_pattern.search(self._row_text(o_cells[0]))
                or not io_pattern.search(self._row_text(io_cells[0]))
            ):
                raise SafetyStop(
                    f"JTable 行 {target_index} 在点击前已不再对应 {o_id}/{io_id}"
                )
            return

        current = self._find_exact_row(
            selected.frame,
            o_id,
            io_id,
            outbound_identity_unique=True,
        )
        identity = self._row_identity(current[0])
        if (
            not identity
            or identity[0] != "datagrid-row-index"
            or identity[1] != target_index
        ):
            raise SafetyStop(
                f"EasyUI 行 {target_index} 在点击前已不再对应 {o_id}/{io_id}"
            )

    def _verify_selected_order(self, selected: SelectedOrder) -> None:
        """Re-read the live grid selection immediately before a button click."""

        if not isinstance(selected, SelectedOrder):
            raise SafetyStop("无法保留目标订单选择凭据，禁止继续")
        self._verify_selected_identity(selected)
        state = self._grid_selection_state(
            selected.selection_scope,
            (selected.target_index,),
            "read",
            selected.grid_kind,
        )
        if state["checked_indices"] != [selected.target_index]:
            raise SafetyStop("点击前目标订单不再是唯一勾选行，禁止继续")
        if any(
            index != selected.target_index
            for index in state["selected_indices"]
        ):
            raise SafetyStop("点击前聚水潭仍选中了其他订单，禁止继续")

    @staticmethod
    def _sorted_indices(values: list[str] | tuple[str, ...]) -> list[str]:
        return sorted((str(value) for value in values), key=lambda value: int(value))

    def select_orders(
        self, identities: list[tuple[str, str]]
    ) -> SelectedBatch:
        """Search, select and prove an exact set of 2-10 outbound orders."""

        normalized = tuple((str(o_id), str(io_id)) for o_id, io_id in identities)
        if not 2 <= len(normalized) <= BATCH_PRINT_SIZE:
            raise SafetyStop(f"批量操作必须包含 2-{BATCH_PRINT_SIZE} 笔订单")
        if len(set(normalized)) != len(normalized) or any(
            not _is_ascii_order_id(o_id) or not _is_ascii_order_id(io_id)
            for o_id, io_id in normalized
        ):
            raise SafetyStop("批量操作订单复合身份重复或无效")

        frame = self._express_frame()
        self._reset_sidebar_scope(frame)
        search = self._reset_search_filters(frame)
        # JST's internal-order control accepts comma-separated exact values.
        # Every returned row is still independently checked against its exact
        # o_id/io_id pair below; a broad/ignored query can never expand the
        # approved selection set.
        query = ",".join(o_id for o_id, _io_id in normalized)
        self._submit_order_search(frame, search, query)

        deadline = time.time() + BATCH_EXACT_ROWS_TIMEOUT_SECONDS
        resolved: list[tuple[Any, Any]] = []
        last_error: Optional[Exception] = None
        while time.time() < deadline:
            resolved = []
            try:
                for o_id, io_id in normalized:
                    resolved.append(
                        self._find_exact_row(
                            frame, o_id, io_id, outbound_identity_unique=True
                        )
                    )
                break
            except OrderRowNotReady as exc:
                last_error = exc
                resolved = []
                time.sleep(0.25)
        if len(resolved) != len(normalized):
            raise BatchSearchUnsupported(
                f"批量查询未完整呈现 {len(normalized)} 笔精确订单：{last_error}"
            )

        indices: list[str] = []
        grid_keys: set[str] = set()
        grid_kinds: set[str] = set()
        selection_scope = None
        for row, _checkbox in resolved:
            identity = self._row_identity(row)
            if not identity or not identity[1].isdigit():
                raise SafetyStop("批量订单行缺少有效表格索引")
            if identity[0] == "jtable-index":
                grid_kind = "jtable"
                body = row.locator("xpath=ancestor::*[@id='_jt_body_list'][1]")
                if body.count() != 1:
                    raise SafetyStop("批量订单行无法唯一映射到 JTable")
                grid_key = "jtable:_jt_body_list"
            elif identity[0] == "datagrid-row-index":
                grid_kind = "easyui"
                row_id = str(row.get_attribute("id") or "")
                grid_match = re.match(r"^(datagrid-row-[^-]+)-\d+-\d+$", row_id)
                if not grid_match:
                    raise SafetyStop("批量订单行无法唯一映射到 EasyUI 表格")
                grid_key = f"easyui:{grid_match.group(1)}"
            else:
                raise SafetyStop("批量订单行表格类型不受支持")
            indices.append(identity[1])
            grid_keys.add(grid_key)
            grid_kinds.add(grid_kind)
            if selection_scope is None:
                selection_scope = self._selection_scope(frame, row)
        if (
            len(grid_keys) != 1
            or len(grid_kinds) != 1
            or len(set(indices)) != len(normalized)
        ):
            raise SafetyStop("批量订单不在同一唯一表格或行索引重复")
        if selection_scope is None:
            raise SafetyStop("批量操作缺少唯一列表作用域")

        targets = tuple(indices)
        grid_kind = next(iter(grid_kinds))
        cleared = self._grid_selection_state(
            selection_scope, targets, "clear", grid_kind
        )
        if cleared["checked_indices"] or cleared["selected_indices"]:
            raise SafetyStop("批量操作前旧勾选未能完全清除")
        checked = self._grid_selection_state(
            selection_scope, targets, "check", grid_kind
        )
        expected = self._sorted_indices(targets)
        if self._sorted_indices(checked["checked_indices"]) != expected:
            raise SafetyStop("聚水潭真实勾选集合与安全批次不一致")
        if any(index not in set(targets) for index in checked["selected_indices"]):
            raise SafetyStop("批量勾选后列表仍选中了批次外订单")
        return SelectedBatch(
            frame=frame,
            selection_scope=selection_scope,
            target_indices=targets,
            identities=normalized,
            grid_kind=grid_kind,
        )

    def _verify_selected_batch(self, selected: SelectedBatch) -> None:
        if not isinstance(selected, SelectedBatch):
            raise SafetyStop("无法保留批量选择凭据，禁止打印")
        self._verify_selected_identity(selected)
        state = self._grid_selection_state(
            selected.selection_scope,
            selected.target_indices,
            "read",
            selected.grid_kind,
        )
        expected = self._sorted_indices(selected.target_indices)
        if self._sorted_indices(state["checked_indices"]) != expected:
            raise SafetyStop("批量打印点击前勾选集合已变化")
        if any(
            index not in set(selected.target_indices)
            for index in state["selected_indices"]
        ):
            raise SafetyStop("批量打印点击前存在批次外选中行")

    def _verify_selected_proof(self, selected: SelectedOrder | SelectedBatch) -> None:
        if isinstance(selected, SelectedOrder):
            self._verify_selected_order(selected)
            return
        if isinstance(selected, SelectedBatch):
            self._verify_selected_batch(selected)
            return
        raise SafetyStop("无法识别订单勾选凭据，禁止继续")

    def _clear_selected_batch(self, selected: SelectedBatch) -> None:
        if not isinstance(selected, SelectedBatch):
            return
        try:
            self._grid_selection_state(
                selected.selection_scope,
                selected.target_indices,
                "clear",
                selected.grid_kind,
            )
        except Exception:
            pass

    def _clear_selected_order(self, selected: SelectedOrder) -> None:
        """Best-effort cleanup for supported live grid selection controls."""

        if not isinstance(selected, SelectedOrder):
            try:
                legacy_checkbox = selected[1]
                if legacy_checkbox is not None:
                    legacy_checkbox.uncheck(force=True)
            except Exception:
                pass
            return
        try:
            state = self._grid_selection_state(
                selected.selection_scope,
                (selected.target_index,),
                "clear",
                selected.grid_kind,
            )
            if not state["checked_indices"] and not state["selected_indices"]:
                return
        except Exception:
            pass
        if selected.checkbox is not None:
            try:
                selected.checkbox.uncheck(force=True)
            except Exception:
                pass

    def _unique_button_locator(self, locator, label: str):
        visible = self._visible(locator)
        if len(visible) != 1:
            raise SafetyStop(f"“{label}”按钮数量为 {len(visible)}，禁止继续")
        target = visible[0]
        try:
            enabled = bool(target.is_enabled())
            disabled_state = target.evaluate(
                """
                (element) => {
                  const classes = String(element.className || '').toLowerCase();
                  const aria = String(element.getAttribute('aria-disabled') || '').toLowerCase();
                  return Boolean(element.disabled)
                    || aria === 'true'
                    || classes.includes('disabled')
                    || classes.includes('l-btn-disabled');
                }
                """
            )
        except Exception as exc:
            raise SafetyStop(f"无法复核“{label}”按钮可用状态") from exc
        if not enabled or disabled_state:
            raise SafetyStop(f"“{label}”按钮当前不可用，禁止提交")
        return target

    def _unique_button(self, frame, selector: str, label: str):
        return self._unique_button_locator(frame.locator(selector), label)

    @staticmethod
    def _prove_button_actionable(button: Any, label: str) -> None:
        """Use Playwright actionability without dispatching a business click."""

        try:
            button.click(trial=True, timeout=5_000)
        except Exception as exc:
            raise SafetyStop(
                f"“{label}”按钮被遮挡、未稳定或不可点击，未进入提交状态"
            ) from exc

    def _click_button(self, frame, selector: str, label: str) -> None:
        button = self._unique_button(frame, selector, label)
        self._prove_button_actionable(button, label)
        button.click()

    def _choose_target_carrier(
        self,
        selected: SelectedOrder | SelectedBatch,
        target_carrier_id: str,
        target_carrier_name: str,
        before_confirm: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        expected_value = f"{target_carrier_id},{target_carrier_name}"
        handled_warning_signatures: set[str] = set()
        deadline = time.time() + 12
        while time.time() < deadline:
            active_frames = []
            warning_frames = []
            for frame in self.page.frames:
                try:
                    if frame.is_detached() or not frame.frame_element().is_visible():
                        continue
                except Exception:
                    continue
                url = str(frame.url or "").lower()
                if _is_jst_frame_path(url, "carrier"):
                    active_frames.append(frame)
                elif _is_jst_frame_path(url, "confirmation"):
                    try:
                        if self._visible(frame.locator("#confirm_confirm")):
                            warning_frames.append(frame)
                    except Exception:
                        continue
            if len(active_frames) > 1:
                raise SafetyStop(
                    f"检测到 {len(active_frames)} 个可见重设快递弹窗，禁止继续"
                )
            if len(warning_frames) > 1:
                raise SafetyStop(
                    f"检测到 {len(warning_frames)} 个可见重设快递确认框，"
                    "禁止继续"
                )
            if active_frames and warning_frames:
                raise SafetyStop("重设快递弹窗与确认框同时可见，禁止继续")
            if warning_frames:
                signature = self._confirm_reset_warning(
                    selected,
                    warning_frames[0],
                    handled_warning_signatures,
                    before_confirm,
                    action_lock,
                    final_guard,
                )
                if signature:
                    handled_warning_signatures.add(signature)
                time.sleep(0.25)
                continue
            if not active_frames:
                time.sleep(0.4)
                continue
            carrier_frame = active_frames[0]
            radios = []
            for radio in self._visible(
                carrier_frame.locator("input[type='radio'][name='lc']")
            ):
                try:
                    if str(radio.get_attribute("value") or "") == expected_value:
                        radios.append(radio)
                except Exception:
                    continue
            if len(radios) != 1:
                raise SafetyStop(
                    f"重设快递弹窗中 {target_carrier_id}/{target_carrier_name} "
                    f"精确单选框数量为 {len(radios)}，禁止继续"
                )
            radio = radios[0]
            try:
                radio.check(force=True)
                if not radio.is_checked():
                    raise SafetyStop("目标快递单选框未保持选中")
            except SafetyStop:
                raise
            except Exception as exc:
                raise SafetyStop("无法选中目标快递单选框，禁止继续") from exc
            with action_lock or nullcontext():
                self._verify_selected_proof(selected)
                if before_confirm is not None:
                    before_confirm()
                # The backend readback can take tens of seconds. Re-prove the
                # exact row/batch after it returns while retaining the same
                # action lock, then resolve fresh controls for the only click.
                self._verify_selected_proof(selected)
                current_radios = []
                for current in self._visible(
                    carrier_frame.locator("input[type='radio'][name='lc']")
                ):
                    try:
                        if str(current.get_attribute("value") or "") == expected_value:
                            current_radios.append(current)
                    except Exception:
                        continue
                if (
                    len(current_radios) != 1
                    or not current_radios[0].is_checked()
                ):
                    raise SafetyStop("目标快递弹窗在最终复核时不再唯一，禁止确认")
                confirm = self._unique_button(
                    carrier_frame,
                    "span.btn_1.big[onclick*='Confirm']",
                    "确定重设快递",
                )
                self._prove_button_actionable(confirm, "确定重设快递")
                if final_guard is not None:
                    final_guard()
                if mark_running is not None:
                    mark_running()
                confirm.click()
            return
        raise SafetyStop(f"重设快递窗口中没有找到“{target_carrier_name}”")

    @staticmethod
    def _reset_warning_kind(
        message: str, o_id: Optional[str]
    ) -> Optional[str]:
        """Whitelist only the two warnings observed in the reset-carrier flow."""

        compact = re.sub(r"\s+", "", str(message or ""))
        expected_order = (
            re.escape(str(o_id)) if o_id is not None else r"[0-9]{1,20}"
        )
        if re.search(
            rf"订单[:：]?{expected_order}已经预发货成功.*"
            r"继续设定面单号.*确定",
            compact,
        ):
            return "PRESHIPPED_ORDER"
        if re.search(
            r"快递单[\(（]?运单[\)）]?号[:：]?[0-9]{6,}"
            r"已经打印过面单.*确认是否重设快递公司",
            compact,
        ):
            return "PRINTED_WAYBILL"
        return None

    def _cancel_known_reset_warning(
        self, dialog_frame: Any, o_id: Optional[str]
    ) -> str:
        """Cancel one known warning without ever touching its confirm control."""

        confirms = self._visible(dialog_frame.locator("#confirm_confirm"))
        cancels = self._visible(dialog_frame.locator("#confirm_close"))
        if len(confirms) != 1 or len(cancels) != 1:
            raise SafetyStop("重设快递确认框的确认/取消按钮不唯一，禁止继续")
        message = ""
        kind: Optional[str] = None
        message_deadline = time.time() + 3.0
        while time.time() < message_deadline:
            prompts = self._visible(dialog_frame.locator("#confirm_top"))
            if len(prompts) > 1:
                raise SafetyStop("重设快递确认提示内容不唯一，禁止继续")
            if len(prompts) == 1:
                try:
                    message = str(prompts[0].inner_text() or "")
                except Exception as exc:
                    raise SafetyStop("无法读取重设快递确认内容，禁止继续") from exc
                if message.strip():
                    kind = self._reset_warning_kind(message, o_id)
                    break
            time.sleep(0.1)
        if not message.strip():
            raise SafetyStop("重设快递确认提示未完整加载，禁止继续")
        if kind is None:
            raise SafetyStop(
                "遇到未授权的确认框，不会自动点击“确认”或“取消”"
            )

        # Re-resolve after reading the asynchronous prompt.  Only the unique
        # cancel control may be clicked; the confirm locator is deliberately
        # never used as an action target.
        cancels = self._visible(dialog_frame.locator("#confirm_close"))
        if len(cancels) != 1:
            raise SafetyStop("已知确认框的取消按钮不再唯一，禁止继续")
        cancel = cancels[0]
        self._prove_button_actionable(cancel, "取消重设快递警告")
        cancel.click()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                if dialog_frame.is_detached() or not dialog_frame.frame_element().is_visible():
                    return kind
            except Exception:
                return kind
            time.sleep(0.1)
        raise SafetyStop("取消已知重设快递警告后确认框仍然可见，禁止继续")

    def _confirm_reset_warning(
        self,
        selected: SelectedOrder | SelectedBatch,
        dialog_frame: Any,
        handled_signatures: set[str],
        before_confirm: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> str:
        """Cancel a known warning, then stop without overriding its evidence."""

        o_id = selected.o_id if isinstance(selected, SelectedOrder) else None
        kind = self._cancel_known_reset_warning(dialog_frame, o_id)
        if kind == "PRESHIPPED_ORDER":
            raise SafetyStop(
                "聚水潭页面提示订单已经预发货；与按钮前回读矛盾，"
                "已只点击取消，不会自动确认重设快递"
            )
        raise SafetyStop(
            "聚水潭页面提示运单已经打印过面单；与按钮前回读矛盾，"
            "已只点击取消，不会自动确认重设快递或产生重复面单"
        )

    def reset_carrier(
        self,
        o_id: str,
        io_id: str,
        target_carrier_id: str,
        target_carrier_name: str,
        outbound_identity_unique: bool = False,
        before_open: Optional[Callable[[], None]] = None,
        before_confirm: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        selected = self.select_order(o_id, io_id, outbound_identity_unique)
        frame, checkbox = selected
        try:
            if before_open is not None:
                before_open()
            with action_lock or nullcontext():
                self._verify_selected_order(selected)
                if final_guard is not None:
                    final_guard()
                self._click_button(frame, "#ResetLc_Btn", "重设快递")
            self._choose_target_carrier(
                selected,
                target_carrier_id,
                target_carrier_name,
                before_confirm,
                mark_running,
                action_lock,
                final_guard,
            )
        finally:
            self._clear_selected_order(selected)

    def reset_carrier_batch(
        self,
        identities: list[tuple[str, str]],
        target_carrier_id: str,
        target_carrier_name: str,
        before_open: Optional[Callable[[], None]] = None,
        before_confirm: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        """Apply one exact carrier change/waybill action to 2-10 orders."""

        selected = self.select_orders(identities)
        try:
            if before_open is not None:
                before_open()
            with action_lock or nullcontext():
                self._verify_selected_batch(selected)
                if final_guard is not None:
                    final_guard()
                self._click_button(selected.frame, "#ResetLc_Btn", "批量重设快递")
            self._choose_target_carrier(
                selected,
                target_carrier_id,
                target_carrier_name,
                before_confirm,
                mark_running,
                action_lock,
                final_guard,
            )
        finally:
            self._clear_selected_batch(selected)

    def get_waybill(
        self,
        o_id: str,
        io_id: str,
        outbound_identity_unique: bool = False,
        before_click: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        selected = self.select_order(o_id, io_id, outbound_identity_unique)
        frame, checkbox = selected
        try:
            with action_lock or nullcontext():
                self._verify_selected_order(selected)
                if before_click is not None:
                    before_click()
                self._verify_selected_order(selected)
                button = self._unique_button(
                    frame, "#GETExpress_Btn", "获取单号"
                )
                self._prove_button_actionable(button, "获取单号")
                if final_guard is not None:
                    final_guard()
                if mark_running is not None:
                    mark_running()
                button.click()
        finally:
            self._clear_selected_order(selected)

    def get_waybill_batch(
        self,
        identities: list[tuple[str, str]],
        before_click: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        """Select 2-10 exact orders and click JST's get-waybill button once."""

        selected = self.select_orders(identities)
        try:
            with action_lock or nullcontext():
                self._verify_selected_batch(selected)
                if before_click is not None:
                    before_click()
                self._verify_selected_batch(selected)
                button = self._unique_button(
                    selected.frame, "#GETExpress_Btn", "批量获取单号"
                )
                self._prove_button_actionable(button, "批量获取单号")
                if final_guard is not None:
                    final_guard()
                if mark_running is not None:
                    mark_running()
                button.click()
        finally:
            self._clear_selected_batch(selected)

    def print_express(
        self,
        o_id: str,
        io_id: str,
        outbound_identity_unique: bool = False,
        before_click: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        selected = self.select_order(o_id, io_id, outbound_identity_unique)
        frame, checkbox = selected
        try:
            with action_lock or nullcontext():
                self._verify_selected_order(selected)
                if before_click is not None:
                    before_click()
                self._verify_selected_order(selected)
                button = self._unique_button(
                    frame, "#printExpress_Btn", "打印快递单"
                )
                self._prove_button_actionable(button, "打印快递单")
                if final_guard is not None:
                    final_guard()
                if mark_running is not None:
                    mark_running()
                button.click()
        finally:
            self._clear_selected_order(selected)

    def print_express_batch(
        self,
        identities: list[tuple[str, str]],
        before_click: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        selected = self.select_orders(identities)
        try:
            with action_lock or nullcontext():
                self._verify_selected_batch(selected)
                if before_click is not None:
                    before_click()
                self._verify_selected_batch(selected)
                button = self._unique_button(
                    selected.frame, "#printExpress_Btn", "批量打印快递单"
                )
                self._prove_button_actionable(button, "批量打印快递单")
                if final_guard is not None:
                    final_guard()
                if mark_running is not None:
                    mark_running()
                button.click()
        finally:
            self._clear_selected_batch(selected)

    def disconnect(self) -> None:
        try:
            self._playwright.stop()
        except Exception:
            pass


_NATIVE_READ_ACTIONS = frozenset(
    {
        "LoadDataToJSON",
        "GetFinishedPresendOids",
        "CheckIoOuterWMS",
        "GetPrintedExpress",
        "CheckHandInsurePrice",
        "CheckIoBeforePrint",
        "CheckAndLoadLCId",
    }
)
_NATIVE_WRITE_ACTIONS = frozenset(
    {
        "SetElids",
        "SetElidsForExpressSetter",
        "SetLogisticsCompaniesV2New",
    }
)
_NATIVE_ACTIONS = _NATIVE_READ_ACTIONS | _NATIVE_WRITE_ACTIONS
_NATIVE_FORBIDDEN_ACTION_TOKENS = (
    "sendby",
    "preship",
    "pre_ship",
    "shipping",
    "shipment",
    "发货",
    "预发货",
)


_JST_NATIVE_HELPER_JS = r"""
if (!globalThis.__JST_NATIVE_API_V2__) {
  const allowedPaths = new Set([
    '/app/wms/express/expresssetter.aspx',
    '/app/wms/express/setelids.aspx'
  ]);
  const safeText = (value) => String(value == null ? '' : value).slice(0, 300);
  const protocol = (text) => {
    if (typeof text !== 'string') throw new Error('响应不是文本');
    const split = text.indexOf('|');
    if (split <= 0 || split >= 6) throw new Error('未知 ASP.NET 回调前缀');
    const skipped = Number(text.slice(0, split));
    if (!Number.isInteger(skipped) || skipped < 0 || skipped > 65536) {
      throw new Error('ASP.NET 回调前缀长度无效');
    }
    let envelope;
    try {
      envelope = JSON.parse(text.slice(split + 1 + skipped));
    } catch (_) {
      throw new Error('ASP.NET 回调信封不是 JSON');
    }
    if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
      throw new Error('ASP.NET 回调信封结构无效');
    }
    return envelope;
  };
  const call = async (options) => {
    if (!options || typeof options !== 'object') throw new Error('调用参数无效');
    const method = String(options.method || '');
    if (!/^[A-Za-z][A-Za-z0-9_]{1,79}$/.test(method)) {
      throw new Error('接口动作名无效');
    }
    if (!window.jQuery || typeof window.jQuery.createPostData !== 'function') {
      throw new Error('页面回调协议尚未就绪');
    }
    const callbackId = options.callbackId === 'ACall1' ? 'ACall1' : 'JTable1';
    const send = {Method: method};
    if (options.callControl !== null) send.CallControl = '{page}';
    if (Array.isArray(options.args) && options.args.length) {
      send.Args = options.args.map((value) => value == null ? null : String(value));
    }
    let body = window.jQuery.createPostData()
      + '__CALLBACKID=' + encodeURIComponent(callbackId)
      + '&__CALLBACKPARAM=' + encodeURIComponent(JSON.stringify(send));
    if (callbackId === 'ACall1') {
      body = body.replace('__VIEWSTATE=', '__VIEWSTATE_DEL=') + '&__VIEWSTATE=';
    }
    const validation = document.forms[0]
      && document.forms[0].elements['__EVENTVALIDATION'];
    if (validation) {
      body += '&__EVENTVALIDATION=' + encodeURIComponent(validation.value);
    }
    const url = new URL(options.url || location.href, location.href);
    if (options.inheritSearch === true) url.search = location.search;
    if (url.origin !== location.origin || !allowedPaths.has(url.pathname.toLowerCase())) {
      throw new Error('接口 URL 不在打单白名单');
    }
    url.searchParams.set('ts___', String(Date.now()));
    url.searchParams.set('am___', method);
    const controller = new AbortController();
    const timeoutMs = Math.max(1000, Math.min(Number(options.timeoutMs) || 45000, 180000));
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response;
    try {
      response = await fetch(url.href, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body,
        signal: controller.signal
      });
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) throw new Error('接口 HTTP ' + response.status);
    const envelope = protocol(await response.text());
    return {
      status: response.status,
      method,
      envelope: {
        IsSuccess: envelope.IsSuccess,
        GotoLogin: envelope.GotoLogin,
        Message: safeText(envelope.Message),
        ExceptionMessage: safeText(envelope.ExceptionMessage),
        ClientScript: safeText(envelope.ClientScript),
        IsReloadPage: envelope.IsReloadPage,
        LocationUrl: safeText(envelope.LocationUrl),
        OpenUrl: safeText(envelope.OpenUrl),
        ReturnValue: envelope.ReturnValue
      }
    };
  };
  const printPreflight = async (ioIds, shopIds) => {
    if (!Array.isArray(ioIds) || !ioIds.length) throw new Error('打印出库单为空');
    const normalizedShops = new Set((shopIds || []).map((value) => String(value)));
    const free = window._FreeCompanyResponse;
    if (free && Array.isArray(free.freeShopLimitInfos)) {
      const blocked = free.freeShopLimitInfos.some((item) => item
        && item.banDeliveryOrder === true
        && normalizedShops.has(String(item.shopId)));
      if (blocked) return {ok: false, reason: 'SHOP_DELIVERY_LIMIT'};
    }
    if (free && free.hasShopSentOrderLimit !== true && free.isContinue === false) {
      return {ok: false, reason: 'COMPANY_DELIVERY_LIMIT'};
    }
    if (window.isBeforeBatchSendVerify === true) {
      const printers = window.top.jstPrinterSettings
        && Array.isArray(window.top.jstPrinterSettings.localPrinters)
        ? window.top.jstPrinterSettings.localPrinters.join(',') : '';
      const checked = await call({
        method: 'CheckIoBeforePrint',
        args: [ioIds.join(','), printers],
        callbackId: 'JTable1',
        timeoutMs: 45000
      });
      const envelope = checked.envelope;
      if (envelope.GotoLogin === true || envelope.IsSuccess !== true) {
        return {ok: false, reason: 'PRINT_PREFLIGHT_PROTOCOL', checked};
      }
      const value = envelope.ReturnValue;
      if (value && typeof value === 'object' && Number(value.show_type || 0) !== 0) {
        return {ok: false, reason: 'PRINT_PREFLIGHT_BLOCKED', showType: Number(value.show_type)};
      }
    }
    if (typeof window.top.PrintAsync !== 'function') {
      return {ok: false, reason: 'PRINT_COMPONENT_API_MISSING'};
    }
    return {ok: true};
  };
  const print = async (form, expectedIoIds) => {
    if (!form || typeof form !== 'object' || Array.isArray(form)) {
      throw new Error('打印表单无效');
    }
    const expected = new Set((expectedIoIds || []).map((value) => String(value)));
    if (!expected.size || typeof window.top.PrintAsync !== 'function') {
      throw new Error('打印组件接口未就绪');
    }
    const safeForm = Object.assign({}, form);
    if (typeof window.__AppendCoid === 'function') window.__AppendCoid(safeForm);
    else if (typeof __AppendCoid === 'function') __AppendCoid(safeForm);
    return await new Promise((resolve, reject) => {
      let finished = false;
      const timer = setTimeout(() => {
        if (!finished) {
          finished = true;
          reject(new Error('打印组件回调超时'));
        }
      }, 120000);
      const done = (state, data, response) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        const callbackIds = [];
        try {
          if (response && response.isJsonPrint === true && response.Maps
              && Array.isArray(response.Maps.printCallbackData)) {
            response.Maps.printCallbackData.forEach((item) => callbackIds.push(String(item.io_id)));
          } else {
            const task = data && data.value && data.value.task;
            if (task && Array.isArray(task.documents)) {
              task.documents.forEach((item) => {
                if (item && item.InoutInfo && item.InoutInfo.io_id != null) {
                  callbackIds.push(String(item.InoutInfo.io_id));
                }
              });
            }
          }
        } catch (_) {}
        const unique = [...new Set(callbackIds)];
        resolve({
          state: String(state == null ? '' : state),
          callbackCount: unique.length,
          callbackMatchesExpected: unique.length === 0
            ? null
            : unique.length === expected.size && unique.every((id) => expected.has(id)),
          isJsonPrint: Boolean(response && response.isJsonPrint === true)
        });
      };
      try {
        window.top.PrintAsync('', safeForm, done, false, false, false, 'printed');
      } catch (error) {
        if (!finished) {
          finished = true;
          clearTimeout(timer);
          reject(error);
        }
      }
    });
  };
  globalThis.__JST_NATIVE_API_V2__ = Object.freeze({call, printPreflight, print});
}
"""


class _NativeCDPConnection:
    """One serialized, long-lived raw CDP WebSocket."""

    def __init__(self, endpoint: str):
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("缺少 websocket-client，无法使用原生浏览器接口") from exc
        self._websocket_module = websocket
        try:
            self._socket = websocket.create_connection(
                endpoint,
                timeout=45,
                suppress_origin=True,
            )
        except Exception as exc:
            raise RuntimeError(
                "专用浏览器连接失败；请关闭占用调试端口的其他程序后重试"
            ) from exc
        self._lock = threading.RLock()
        self._next_id = 1
        self._events: list[dict[str, Any]] = []
        self._closed = False

    def call(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("原生浏览器连接已关闭")
            request_id = self._next_id
            self._next_id += 1
            request: dict[str, Any] = {"id": request_id, "method": method}
            if params:
                request["params"] = params
            if session_id:
                request["sessionId"] = session_id
            try:
                deadline = time.monotonic() + timeout
                self._socket.settimeout(timeout)
                self._socket.send(json.dumps(request, ensure_ascii=False))
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"CDP {method} 超过 {timeout:g} 秒总等待时间")
                    self._socket.settimeout(remaining)
                    incoming = json.loads(self._socket.recv())
                    if not isinstance(incoming, dict):
                        raise RuntimeError(f"CDP {method} 返回结构无效")
                    if incoming.get("id") != request_id:
                        if isinstance(incoming, dict):
                            self._events.append(incoming)
                            if len(self._events) > 2000:
                                del self._events[:1000]
                        continue
                    error = incoming.get("error")
                    if error:
                        raise RuntimeError(f"CDP {method} 失败：{error}")
                    result = incoming.get("result", {})
                    if not isinstance(result, dict):
                        raise RuntimeError(f"CDP {method} 返回结构无效")
                    return result
            except Exception:
                if method == "Browser.getVersion":
                    self.close()
                raise

    def take_events(self, method: str, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            selected = [
                event
                for event in self._events
                if event.get("method") == method
                and event.get("sessionId") == session_id
            ]
            self._events = [event for event in self._events if event not in selected]
            return selected

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._socket.close()
            except Exception:
                pass


class JSTNativeBrowser:
    """JST business actions through CDP Runtime + browser-native fetch only."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._connection = _NativeCDPConnection(
            cdp_endpoint(settings.debug_port, settings.browser_name)
        )
        self._session_id: Optional[str] = None
        self._target_id: Optional[str] = None
        self._browser_context_id: Optional[str] = None
        self._context_id: Optional[int] = None
        self._frame_id: Optional[str] = None
        self._lock = threading.RLock()
        try:
            self._bind_print_target(create_if_missing=True)
        except Exception:
            self._connection.close()
            raise

    @staticmethod
    def _walk_frame_tree(node: dict[str, Any]) -> list[dict[str, Any]]:
        frame = node.get("frame")
        result = [frame] if isinstance(frame, dict) else []
        for child in node.get("childFrames", []) or []:
            if isinstance(child, dict):
                result.extend(JSTNativeBrowser._walk_frame_tree(child))
        return result

    @staticmethod
    def _is_exact_print_target(target: dict[str, Any]) -> bool:
        if target.get("type") != "page":
            return False
        try:
            parsed = urlsplit(str(target.get("url", "")))
        except Exception:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname == "www.erp321.com"
            and parsed.path == "/epaas"
            and parse_qs(parsed.query).get("n") == ["打单拣货"]
        )

    @staticmethod
    def _is_jst_page_target(target: dict[str, Any]) -> bool:
        if target.get("type") != "page":
            return False
        try:
            parsed = urlsplit(str(target.get("url", "")))
        except Exception:
            return False
        return parsed.scheme == "https" and parsed.hostname == "www.erp321.com"

    def _targets(self) -> list[dict[str, Any]]:
        targets = self._connection.call("Target.getTargets").get("targetInfos", [])
        if not isinstance(targets, list):
            raise RuntimeError("CDP 目标列表结构无效")
        return [target for target in targets if isinstance(target, dict)]

    def _create_print_target(self, targets: list[dict[str, Any]]) -> str:
        jst_pages = [target for target in targets if self._is_jst_page_target(target)]
        context_ids = {
            str(target.get("browserContextId"))
            if target.get("browserContextId") else None
            for target in jst_pages
        }
        if not context_ids:
            context_ids = {
                str(target.get("browserContextId"))
                if target.get("browserContextId") else None
                for target in targets
                if target.get("type") == "page"
            }
        if len(context_ids) != 1:
            raise SafetyStop("当前 Chrome 登录上下文不唯一，禁止猜测并新建打单页")
        context_id = next(iter(context_ids))
        params: dict[str, Any] = {"url": JST_HOME_URL}
        # CDP omits browserContextId for the one normal/default Chrome
        # context. Omitting it on createTarget preserves that exact profile,
        # cookies and login; a concrete id is supplied only for incognito.
        if context_id is not None:
            params["browserContextId"] = context_id
        created = self._connection.call(
            "Target.createTarget",
            params,
        )
        target_id = created.get("targetId")
        if not isinstance(target_id, str) or not target_id:
            raise RuntimeError("Chrome 未返回新打单页目标")
        return target_id

    def _attach(self, target: dict[str, Any], *, resolve: bool = True) -> None:
        target_id = str(target.get("targetId", ""))
        if not target_id:
            raise RuntimeError("打单页缺少 targetId")
        attached = self._connection.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = attached.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("无法附加到打单页")
        self._target_id = target_id
        self._session_id = session_id
        context_id = target.get("browserContextId")
        self._browser_context_id = str(context_id) if context_id else None
        self._connection.call("Page.enable", session_id=session_id)
        self._connection.call("Target.activateTarget", {"targetId": target_id})
        if resolve:
            self._resolve_express_context()

    def _bind_print_target(self, *, create_if_missing: bool) -> None:
        deadline = time.monotonic() + 20.0
        created_id: Optional[str] = None
        while time.monotonic() < deadline:
            targets = self._targets()
            print_targets = [target for target in targets if self._is_exact_print_target(target)]
            if len(print_targets) > 1:
                raise SafetyStop("当前 Chrome 存在多个打单拣货页，禁止猜测调用")
            if len(print_targets) == 1:
                try:
                    self._attach(print_targets[0])
                    return
                except OrderRowNotReady:
                    # A newly opened/restored page may exist before its iframe.
                    # Wait on this same target instead of failing startup.
                    time.sleep(0.35)
                    continue
            if create_if_missing and created_id is None:
                created_id = self._create_print_target(targets)
            time.sleep(0.25)
        raise OrderRowNotReady("当前登录会话未能打开唯一的打单拣货页")

    def _resolve_express_context(self) -> int:
        session_id = self._session_id
        if not session_id:
            raise OrderRowNotReady("原生浏览器会话尚未绑定打单页")
        # Re-enable Runtime to get a fresh, complete execution-context snapshot
        # after iframe replacement without touching page or form state.
        # Drop snapshots left by an earlier Runtime.enable. Otherwise a
        # re-enable can leave two valid-looking records for the same default
        # world and make a healthy Chrome 151 page appear ambiguous.
        self._connection.take_events(
            "Runtime.executionContextCreated", session_id
        )
        try:
            self._connection.call("Runtime.disable", session_id=session_id)
        except Exception:
            pass
        self._connection.take_events(
            "Runtime.executionContextCreated", session_id
        )
        self._connection.call("Runtime.enable", session_id=session_id)
        tree = self._connection.call("Page.getFrameTree", session_id=session_id).get(
            "frameTree"
        )
        if not isinstance(tree, dict):
            raise OrderRowNotReady("Chrome 未返回打单页 frame tree")
        frames = []
        for frame in self._walk_frame_tree(tree):
            try:
                parsed = urlsplit(str(frame.get("url", "")))
            except Exception:
                continue
            if (
                parsed.scheme == "https"
                and parsed.hostname == "www.erp321.com"
                and parsed.path.lower() == "/app/wms/express/expresssetter.aspx"
            ):
                frames.append(frame)
        if len(frames) != 1:
            raise OrderRowNotReady(
                f"打单拣货执行上下文数量为 {len(frames)}，需在同一会话内恢复页面"
            )
        frame_id = str(frames[0].get("id", ""))
        events = self._connection.take_events(
            "Runtime.executionContextCreated", session_id
        )
        contexts = []
        for event in events:
            context = (event.get("params") or {}).get("context")
            if not isinstance(context, dict):
                continue
            auxiliary = context.get("auxData") or {}
            if (
                str(auxiliary.get("frameId", "")) == frame_id
                and auxiliary.get("isDefault") is True
            ):
                contexts.append(context)
        if len(contexts) != 1 or type(contexts[0].get("id")) is not int:
            raise OrderRowNotReady("无法唯一解析打单 iframe 的默认 JavaScript 上下文")
        self._frame_id = frame_id
        self._context_id = int(contexts[0]["id"])
        return self._context_id

    def _evaluate(self, expression: str, *, timeout: float = 60.0) -> Any:
        with self._lock:
            context_id = self._resolve_express_context()
            session_id = self._session_id
            if not session_id:
                raise OrderRowNotReady("打单页 CDP session 已失效")
            evaluated = self._connection.call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "contextId": context_id,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": False,
                },
                session_id=session_id,
                timeout=timeout,
            )
        if evaluated.get("exceptionDetails"):
            description = str(
                ((evaluated.get("result") or {}).get("description"))
                or "浏览器 JavaScript 执行失败"
            )
            raise RuntimeError(description[:500])
        result = evaluated.get("result")
        if not isinstance(result, dict) or "value" not in result:
            raise RuntimeError("浏览器 JavaScript 未返回可序列化结果")
        return result.get("value")

    @staticmethod
    def _safe_server_message(value: Any) -> str:
        message = re.sub(r"\s+", " ", str(value or "")).strip()
        return message[:300]

    def _evaluate_readonly(self, expression: str, *, timeout: float) -> Any:
        try:
            return self._evaluate(expression, timeout=timeout)
        except SafetyStop:
            raise
        except Exception as exc:
            raise OrderRowNotReady(
                f"只读页面准备暂不可用，需有界重试或恢复页面：{exc}"
            ) from exc

    def _call_page(
        self,
        method: str,
        args: list[Any],
        *,
        callback_id: str = "JTable1",
        url: Optional[str] = None,
        inherit_search: bool = False,
        call_control: Optional[str] = "{page}",
        timeout: float = 60.0,
    ) -> Any:
        normalized = str(method)
        lowered = normalized.lower()
        if normalized not in _NATIVE_ACTIONS or any(
            token in lowered for token in _NATIVE_FORBIDDEN_ACTION_TOKENS
        ):
            raise SafetyStop(f"接口动作 {normalized!r} 不在打单安全白名单")
        options = {
            "method": normalized,
            "args": args,
            "callbackId": callback_id,
            "url": url,
            "inheritSearch": bool(inherit_search),
            "callControl": call_control,
            "timeoutMs": int(min(max(timeout * 1000, 1000), 180000)),
        }
        expression = (
            "(async()=>{" + _JST_NATIVE_HELPER_JS
            + "\nreturn await globalThis.__JST_NATIVE_API_V2__.call("
            + json.dumps(options, ensure_ascii=False, separators=(",", ":"))
            + ");})()"
        )
        recovery_count = 0
        while True:
            try:
                response = self._evaluate(expression, timeout=timeout + 10)
            except SafetyStop:
                raise
            except Exception as exc:
                if normalized in _NATIVE_READ_ACTIONS:
                    raise OrderRowNotReady(
                        f"只读接口 {normalized} 暂不可用，需有界重试或恢复页面：{exc}"
                    ) from exc
                raise
            if not isinstance(response, dict) or response.get("status") != 200:
                raise RuntimeError(f"{normalized} 未返回 HTTP 200 协议结果")
            envelope = response.get("envelope")
            if not isinstance(envelope, dict):
                raise RuntimeError(f"{normalized} 回调信封结构无效")
            if envelope.get("GotoLogin") is True:
                raise SafetyStop("当前 Chrome 登录态已失效；不会切换浏览器或自动登录")
            if envelope.get("IsSuccess") is not True:
                reason = self._safe_server_message(
                    envelope.get("ExceptionMessage") or envelope.get("Message")
                )
                if (
                    recovery_count < 2
                    and normalized in _NATIVE_READ_ACTIONS
                    and "ErrorCode:120009" in reason
                ):
                    # The server explicitly proves that this callback was
                    # rejected before the read ran. Refresh only the bound
                    # page and retry only a read; write calls are never replayed.
                    self.recover_print_page(
                        replace_page=(recovery_count == 1)
                    )
                    recovery_count += 1
                    continue
                raise SafetyStop(
                    f"{normalized} 业务返回失败：{reason or '无错误详情'}"
                )
            if any(
                envelope.get(key)
                for key in ("ClientScript", "IsReloadPage", "LocationUrl", "OpenUrl")
            ):
                raise SafetyStop(f"{normalized} 要求页面脚本或导航，原生接口拒绝隐式执行")
            message = self._safe_server_message(envelope.get("Message"))
            if message:
                raise SafetyStop(f"{normalized} 返回额外确认信息：{message}")
            return envelope.get("ReturnValue")

    @staticmethod
    def _normalize_id(value: Any) -> str:
        if isinstance(value, bool) or value is None:
            return ""
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    def _exact_rows(
        self, identities: list[tuple[str, str]]
    ) -> list[dict[str, Any]]:
        normalized = [(str(o_id), str(io_id)) for o_id, io_id in identities]
        if not 1 <= len(normalized) <= BATCH_PRINT_SIZE:
            raise SafetyStop(f"原生接口批次必须包含 1-{BATCH_PRINT_SIZE} 笔订单")
        if len(set(normalized)) != len(normalized) or any(
            not _is_ascii_order_id(o_id) or not _is_ascii_order_id(io_id)
            for o_id, io_id in normalized
        ):
            raise SafetyStop("原生接口订单复合身份重复或无效")
        order_ids = [o_id for o_id, _io_id in normalized]
        if len(set(order_ids)) != len(order_ids):
            raise SafetyStop("同一批次内部订单号重复，禁止扩大接口匹配范围")
        filters = [{"k": "o_id", "v": ",".join(order_ids), "c": "@="}]
        returned = self._call_page(
            "LoadDataToJSON",
            ["1", json.dumps(filters, ensure_ascii=False, separators=(",", ":")), "{}"],
            call_control=None,
            timeout=60,
        )
        if not isinstance(returned, str):
            raise OrderRowNotReady("LoadDataToJSON 未返回列表 JSON 文本")
        try:
            payload = json.loads(returned)
        except json.JSONDecodeError as exc:
            raise OrderRowNotReady("LoadDataToJSON 列表 JSON 无法解析") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("datas"), list):
            raise OrderRowNotReady("LoadDataToJSON 列表结构已变化")
        rows = [row for row in payload["datas"] if isinstance(row, dict)]
        data_page = payload.get("dp")
        if not isinstance(data_page, dict):
            raise OrderRowNotReady("LoadDataToJSON 缺少分页证明")
        try:
            total = int(data_page.get("DataCount", -1))
        except (TypeError, ValueError):
            total = -1
        if total < 0 or total > len(rows):
            raise BatchSearchUnsupported(
                "精确接口查询结果超过当前页，禁止在不完整结果上提交"
            )
        expected = set(normalized)
        actual_rows: dict[tuple[str, str], dict[str, Any]] = {}
        unexpected: set[tuple[str, str]] = set()
        for row in rows:
            identity = (
                self._normalize_id(row.get("o_id")),
                self._normalize_id(row.get("io_id")),
            )
            if identity not in expected:
                unexpected.add(identity)
                continue
            if identity in actual_rows:
                raise SafetyStop(f"接口返回重复订单身份 {identity[0]}/{identity[1]}")
            warehouse = self._normalize_id(row.get("wms_co_id"))
            if warehouse != TARGET_WAREHOUSE_ID:
                raise SafetyStop(
                    f"接口订单 {identity[0]}/{identity[1]} 不属于目标仓库"
                )
            key_data = row.get("__KeyData")
            if not isinstance(key_data, str) or not (1 <= len(key_data) <= 1024):
                raise SafetyStop("接口订单缺少有效 __KeyData，禁止提交业务动作")
            actual_rows[identity] = row
        if unexpected:
            raise SafetyStop("精确订单接口返回了未授权的额外出库身份")
        missing = [identity for identity in normalized if identity not in actual_rows]
        if missing:
            raise OrderRowNotReady(
                f"原生接口未完整返回 {len(missing)} 笔精确订单，稍后重试"
            )
        return [actual_rows[identity] for identity in normalized]

    @staticmethod
    def _valid_lid(value: Any) -> bool:
        text = str(value or "").strip()
        return bool(text) and "错误" not in text and not text.lower().startswith("e:")

    @staticmethod
    def _require_callback_empty(value: Any, label: str) -> None:
        if value not in (None, "", []):
            raise SafetyStop(f"{label}返回阻断信息，禁止绕过页面确认")

    def _preflight_waybill_rows(self, rows: list[dict[str, Any]]) -> None:
        if any(self._valid_lid(row.get("l_id")) for row in rows):
            raise SafetyStop("取号前接口已存在有效运单号，禁止重复取号")
        if any(not str(row.get("lc_id") or "").strip() for row in rows):
            raise SafetyStop("取号前接口发现订单未设置快递公司")
        o_ids = ",".join(self._normalize_id(row.get("o_id")) for row in rows)
        self._require_callback_empty(
            self._call_page("GetFinishedPresendOids", [o_ids]),
            "预发货检查",
        )
        hand_insure = self._call_page("CheckHandInsurePrice", [o_ids])
        if hand_insure is True or str(hand_insure).lower() == "true":
            raise SafetyStop("订单需要人工输入保价金额，原生接口不会猜测金额")

    def _preflight_reset_rows(self, rows: list[dict[str, Any]]) -> None:
        o_ids = ",".join(self._normalize_id(row.get("o_id")) for row in rows)
        io_ids = ",".join(self._normalize_id(row.get("io_id")) for row in rows)
        self._require_callback_empty(
            self._call_page("GetFinishedPresendOids", [o_ids]),
            "预发货检查",
        )
        self._require_callback_empty(
            self._call_page("CheckIoOuterWMS", [io_ids]),
            "外部仓检查",
        )
        lids = [str(row.get("l_id") or "").strip() for row in rows]
        active_lids = [lid for lid in lids if self._valid_lid(lid)]
        if active_lids:
            printed = self._call_page("GetPrintedExpress", [",".join(active_lids)])
            self._require_callback_empty(printed, "已打印运单检查")

    @staticmethod
    def _decode_object(value: Any, label: str) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise SafetyStop(f"{label}返回值不是对象") from exc
        if not isinstance(value, dict):
            raise SafetyStop(f"{label}返回值结构无效")
        return value

    def _submit_waybill(
        self, rows: list[dict[str, Any]],
        before_submit: Optional[Callable[[], None]] = None,
    ) -> None:
        keys = [str(row["__KeyData"]) for row in rows]
        use_new = self._evaluate_readonly(
            "Boolean(globalThis._isUseNewWaybillDialog)", timeout=20
        )
        if before_submit is not None:
            before_submit()
        if use_new is True:
            result = self._call_page(
                "SetElidsForExpressSetter",
                [",".join(keys)],
                callback_id="ACall1",
                url="SetELids.aspx",
                inherit_search=True,
                timeout=120,
            )
        else:
            result = self._call_page("SetElids", [",".join(keys)], timeout=120)
        if not isinstance(result, str) or not result.startswith(("L:", "S:")):
            raise SafetyStop("取号接口未返回 L:/S: 结果，提交状态不确定")
        if result.startswith("S:"):
            raise SafetyStop("取号接口返回订单状态变化，禁止继续当前批次")
        try:
            mapping = json.loads(result[2:])
        except json.JSONDecodeError as exc:
            raise SafetyStop("取号接口 L: 结果无法解析") from exc
        if not isinstance(mapping, dict) or any(key not in mapping for key in keys):
            raise SafetyStop("取号接口未完整返回每笔 __KeyData 的结果")
        for key in keys:
            value = mapping[key]
            if isinstance(value, dict) or not self._valid_lid(value):
                raise SafetyStop("至少一笔订单取号失败；不会自动重复提交")

    def _submit_reset(
        self,
        rows: list[dict[str, Any]],
        target_carrier_id: str,
        target_carrier_name: str,
    ) -> None:
        if PRINT_PROFILE_CARRIERS.get(target_carrier_id) != target_carrier_name:
            raise SafetyStop("重设快递接口的目标快递不在安全白名单")
        keys = ",".join(str(row["__KeyData"]) for row in rows)
        o_ids = ",".join(self._normalize_id(row.get("o_id")) for row in rows)
        request = {
            "lcInfo": f"{target_carrier_id},{target_carrier_name}",
            "keyDatas": keys,
            "oids": o_ids,
            "LcType": 1,
        }
        result = self._call_page(
            "SetLogisticsCompaniesV2New",
            [json.dumps(request, ensure_ascii=False, separators=(",", ":"))],
            timeout=150,
        )
        decoded = self._decode_object(result, "重设快递接口")
        ios = decoded.get("Ios")
        if not isinstance(ios, list):
            raise SafetyStop("重设快递接口缺少 Ios 回执")
        expected = {
            self._normalize_id(row.get("io_id"))
            for row in rows
        }
        returned = {
            self._normalize_id(item.get("io_id"))
            for item in ios
            if isinstance(item, dict)
        }
        if not expected.issubset(returned):
            raise SafetyStop("重设快递接口未完整回执安全批次")
        message = self._safe_server_message(decoded.get("Message"))
        if message:
            raise SafetyStop(f"重设快递接口返回需处理信息：{message}")

    def _print_preflight(
        self, rows: list[dict[str, Any]], io_ids: list[str]
    ) -> None:
        shop_ids = [self._normalize_id(row.get("shop_id")) for row in rows]
        expression = (
            "(async()=>{" + _JST_NATIVE_HELPER_JS
            + "\nreturn await globalThis.__JST_NATIVE_API_V2__.printPreflight("
            + json.dumps(io_ids, ensure_ascii=False)
            + ","
            + json.dumps(shop_ids, ensure_ascii=False)
            + ");})()"
        )
        result = self._evaluate_readonly(expression, timeout=70)
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = str((result or {}).get("reason", "UNKNOWN"))
            show_type = (result or {}).get("showType")
            suffix = f"（show_type={show_type}）" if show_type is not None else ""
            raise SafetyStop(f"打印前页面业务校验未通过：{reason}{suffix}")

    def _submit_print(
        self, rows: list[dict[str, Any]],
        before_submit: Optional[Callable[[], None]] = None,
    ) -> None:
        io_ids = [self._normalize_id(row.get("io_id")) for row in rows]
        self._print_preflight(rows, io_ids)
        lc_id = self._call_page("CheckAndLoadLCId", [",".join(io_ids)], timeout=90)
        if not isinstance(lc_id, str) or not lc_id.strip():
            raise SafetyStop("打印前接口未返回唯一快递模板类型")
        page_module = self._evaluate_readonly(
            "String(globalThis.__Moudle || '')", timeout=20
        )
        if not isinstance(page_module, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,80}", page_module
        ):
            raise SafetyStop("打印页未提供有效 __Moudle，禁止猜测打印参数")
        form = {
            "type": "hybrid_print_print_service",
            "sub_type": lc_id.strip(),
            "ids": ",".join(io_ids),
            "moudle": page_module,
            "SinglePrinter": "SinglePrinter",
            "json_print_moudle": "express_ddjh",
            "preview": False,
        }
        expression = (
            "(async()=>{" + _JST_NATIVE_HELPER_JS
            + "\nreturn await globalThis.__JST_NATIVE_API_V2__.print("
            + json.dumps(form, ensure_ascii=False, separators=(",", ":"))
            + ","
            + json.dumps(io_ids, ensure_ascii=False)
            + ");})()"
        )
        if before_submit is not None:
            before_submit()
        result = self._evaluate(expression, timeout=135)
        if not isinstance(result, dict):
            raise SafetyStop("打印组件回调结构无效")
        state = str(result.get("state", "")).upper()
        if not state or state in {"FAILED", "FAIL", "CLOSE", "CLOSED", "ERROR"}:
            raise SafetyStop(f"打印组件未确认成功：{state or 'EMPTY_STATE'}")
        if result.get("callbackMatchesExpected") is False:
            raise SafetyStop("打印组件回调的出库单集合与安全批次不一致")

    def is_healthy(self) -> bool:
        try:
            self._connection.call("Browser.getVersion", timeout=3)
            targets = self._targets()
            return any(
                target.get("targetId") == self._target_id
                for target in targets
            )
        except Exception:
            return False

    def recover_print_page(self, *, replace_page: bool = False) -> None:
        with self._lock:
            old_target = self._target_id
            if not replace_page and self._session_id:
                old_signature: Optional[tuple[str, str]] = None
                try:
                    old_tree = self._connection.call(
                        "Page.getFrameTree", session_id=self._session_id
                    ).get("frameTree")
                    if isinstance(old_tree, dict):
                        old_frames = []
                        for frame in self._walk_frame_tree(old_tree):
                            parsed = urlsplit(str(frame.get("url", "")))
                            if (
                                parsed.hostname == "www.erp321.com"
                                and parsed.path.lower()
                                == "/app/wms/express/expresssetter.aspx"
                            ):
                                old_frames.append(frame)
                        if len(old_frames) == 1:
                            old_signature = (
                                str(old_frames[0].get("id", "")),
                                str(old_frames[0].get("loaderId", "")),
                            )
                except Exception:
                    old_signature = None
                self._connection.call(
                    "Page.reload",
                    {"ignoreCache": True},
                    session_id=self._session_id,
                )
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    try:
                        tree = self._connection.call(
                            "Page.getFrameTree", session_id=self._session_id
                        ).get("frameTree")
                        if not isinstance(tree, dict):
                            raise OrderRowNotReady("打单页 frame tree 尚未就绪")
                        frames = []
                        for frame in self._walk_frame_tree(tree):
                            parsed = urlsplit(str(frame.get("url", "")))
                            if (
                                parsed.hostname == "www.erp321.com"
                                and parsed.path.lower()
                                == "/app/wms/express/expresssetter.aspx"
                            ):
                                frames.append(frame)
                        if len(frames) != 1:
                            raise OrderRowNotReady("打单 iframe 正在重载")
                        signature = (
                            str(frames[0].get("id", "")),
                            str(frames[0].get("loaderId", "")),
                        )
                        if old_signature is not None and signature == old_signature:
                            raise OrderRowNotReady("打单 iframe 尚未切换到新 loader")
                        context_id = self._resolve_express_context()
                        ready = self._connection.call(
                            "Runtime.evaluate",
                            {
                                "expression": (
                                    "document.readyState === 'complete'"
                                    " && Boolean(window.jQuery)"
                                    " && typeof window.jQuery.createPostData === 'function'"
                                    " && typeof window._CallPage === 'function'"
                                ),
                                "contextId": context_id,
                                "returnByValue": True,
                            },
                            session_id=self._session_id,
                        )
                        if ((ready.get("result") or {}).get("value")) is True:
                            return
                    except (OrderRowNotReady, RuntimeError, ValueError):
                        time.sleep(0.35)
                raise OrderRowNotReady("同一打单页强制刷新后接口上下文仍未恢复")
            targets = self._targets()
            new_id = self._create_print_target(targets)
            succeeded = False
            try:
                attached = False
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    current = self._targets()
                    target = next(
                        (item for item in current if item.get("targetId") == new_id), None
                    )
                    if target and self._is_exact_print_target(target):
                        try:
                            if not attached:
                                self._attach(target, resolve=False)
                                attached = True
                            context_id = self._resolve_express_context()
                            ready = self._connection.call(
                                "Runtime.evaluate",
                                {
                                    "expression": (
                                        "document.readyState === 'complete'"
                                        " && Boolean(window.jQuery)"
                                        " && typeof window.jQuery.createPostData === 'function'"
                                    ),
                                    "contextId": context_id,
                                    "returnByValue": True,
                                },
                                session_id=self._session_id,
                            )
                            if ((ready.get("result") or {}).get("value")) is True:
                                if old_target and old_target != new_id:
                                    try:
                                        self._connection.call(
                                            "Target.closeTarget", {"targetId": old_target}
                                        )
                                    except Exception:
                                        pass
                                succeeded = True
                                return
                        except (OrderRowNotReady, RuntimeError, ValueError):
                            pass
                    time.sleep(0.35)
            finally:
                if not succeeded:
                    try:
                        self._connection.call("Target.closeTarget", {"targetId": new_id})
                    except Exception:
                        pass
                    if old_target:
                        try:
                            remaining = self._targets()
                            old = next(
                                (item for item in remaining if item.get("targetId") == old_target),
                                None,
                            )
                            if old is not None:
                                self._attach(old, resolve=False)
                        except Exception:
                            pass
            raise OrderRowNotReady("同一 Chrome 登录上下文的新打单页未就绪")

    def get_waybill(
        self,
        o_id: str,
        io_id: str,
        outbound_identity_unique: bool = False,
        before_click: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        if type(outbound_identity_unique) is not bool:
            raise SafetyStop("后台出库身份唯一性凭据格式错误")
        identities = [(str(o_id), str(io_id))]
        rows = self._exact_rows(identities)
        self._preflight_waybill_rows(rows)
        with action_lock or nullcontext():
            rows = self._exact_rows(identities)
            self._preflight_waybill_rows(rows)
            if before_click is not None:
                before_click()
            rows = self._exact_rows(identities)
            self._preflight_waybill_rows(rows)
            def before_submit() -> None:
                # Read-only preparation has finished. Recheck operator control
                # at the actual submission boundary, then persist RUNNING.
                if final_guard is not None:
                    final_guard()
                if mark_running is not None:
                    mark_running()

            self._submit_waybill(rows, before_submit=before_submit)

    def get_waybill_batch(
        self,
        identities: list[tuple[str, str]],
        before_click: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        rows = self._exact_rows(identities)
        self._preflight_waybill_rows(rows)
        with action_lock or nullcontext():
            rows = self._exact_rows(identities)
            self._preflight_waybill_rows(rows)
            if before_click is not None:
                before_click()
            rows = self._exact_rows(identities)
            self._preflight_waybill_rows(rows)
            def before_submit() -> None:
                # Read-only preparation has finished. Recheck operator control
                # at the actual submission boundary, then persist RUNNING.
                if final_guard is not None:
                    final_guard()
                if mark_running is not None:
                    mark_running()

            self._submit_waybill(rows, before_submit=before_submit)

    def reset_carrier(
        self,
        o_id: str,
        io_id: str,
        target_carrier_id: str,
        target_carrier_name: str,
        outbound_identity_unique: bool = False,
        before_open: Optional[Callable[[], None]] = None,
        before_confirm: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        if type(outbound_identity_unique) is not bool:
            raise SafetyStop("后台出库身份唯一性凭据格式错误")
        if before_open is not None:
            before_open()
        identities = [(str(o_id), str(io_id))]
        rows = self._exact_rows(identities)
        self._preflight_reset_rows(rows)
        with action_lock or nullcontext():
            rows = self._exact_rows(identities)
            self._preflight_reset_rows(rows)
            if before_confirm is not None:
                before_confirm()
            rows = self._exact_rows(identities)
            self._preflight_reset_rows(rows)
            if final_guard is not None:
                final_guard()
            if mark_running is not None:
                mark_running()
            self._submit_reset(rows, target_carrier_id, target_carrier_name)

    def reset_carrier_batch(
        self,
        identities: list[tuple[str, str]],
        target_carrier_id: str,
        target_carrier_name: str,
        before_open: Optional[Callable[[], None]] = None,
        before_confirm: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        if before_open is not None:
            before_open()
        rows = self._exact_rows(identities)
        self._preflight_reset_rows(rows)
        with action_lock or nullcontext():
            rows = self._exact_rows(identities)
            self._preflight_reset_rows(rows)
            if before_confirm is not None:
                before_confirm()
            rows = self._exact_rows(identities)
            self._preflight_reset_rows(rows)
            if final_guard is not None:
                final_guard()
            if mark_running is not None:
                mark_running()
            self._submit_reset(rows, target_carrier_id, target_carrier_name)

    def print_express(
        self,
        o_id: str,
        io_id: str,
        outbound_identity_unique: bool = False,
        before_click: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        if type(outbound_identity_unique) is not bool:
            raise SafetyStop("后台出库身份唯一性凭据格式错误")
        identities = [(str(o_id), str(io_id))]
        rows = self._exact_rows(identities)
        if any(not self._valid_lid(row.get("l_id")) for row in rows):
            raise SafetyStop("打印前原生接口未证明每笔订单都有有效运单号")
        with action_lock or nullcontext():
            rows = self._exact_rows(identities)
            if before_click is not None:
                before_click()
            rows = self._exact_rows(identities)
            if any(not self._valid_lid(row.get("l_id")) for row in rows):
                raise SafetyStop("打印提交前运单号已失效")
            def before_submit() -> None:
                # Read-only preparation has finished. Recheck operator control
                # at the actual submission boundary, then persist RUNNING.
                if final_guard is not None:
                    final_guard()
                if mark_running is not None:
                    mark_running()

            self._submit_print(rows, before_submit=before_submit)

    def print_express_batch(
        self,
        identities: list[tuple[str, str]],
        before_click: Optional[Callable[[], None]] = None,
        mark_running: Optional[Callable[[], None]] = None,
        action_lock: Optional[threading.RLock] = None,
        final_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        rows = self._exact_rows(identities)
        if any(not self._valid_lid(row.get("l_id")) for row in rows):
            raise SafetyStop("批量打印前至少一笔订单缺少有效运单号")
        with action_lock or nullcontext():
            rows = self._exact_rows(identities)
            if before_click is not None:
                before_click()
            rows = self._exact_rows(identities)
            if any(not self._valid_lid(row.get("l_id")) for row in rows):
                raise SafetyStop("批量打印提交前至少一笔运单号已失效")
            def before_submit() -> None:
                # Read-only preparation has finished. Recheck operator control
                # at the actual submission boundary, then persist RUNNING.
                if final_guard is not None:
                    final_guard()
                if mark_running is not None:
                    mark_running()

            self._submit_print(rows, before_submit=before_submit)

    def disconnect(self) -> None:
        self._connection.close()

    def close(self) -> None:
        """Compatibility-safe, idempotent shutdown for callers and probes."""
        self.disconnect()


# Main-flow compatibility name.  The worker now resolves to the native CDP /
# fetch implementation above; the legacy Playwright class is never instantiated.
JSTBrowser = JSTNativeBrowser


class AutomationEngine:
    def __init__(
        self,
        settings: Settings,
        store: EventStore,
        notify: Callable[[dict[str, Any]], None],
        status: Callable[[str], None],
        workstation_id: str = "ws-00000000-0000-0000-0000-000000000000",
    ):
        self.settings = settings
        self.store = store
        self.notify = notify
        self.set_status = status
        self.workstation_id = workstation_id
        self.stop_event = threading.Event()
        self.shutdown_event = threading.Event()
        self.run_event = threading.Event()
        self.settings_lock = threading.Lock()
        self.thread_lock = threading.Lock()
        self.commit_lock = threading.RLock()
        self.operator_skip_lock = threading.Lock()
        self.force_skip_in_progress = False
        # Compatibility name for browser/test call sites; this is deliberately
        # the short commit lock, never a lock around remote inspect/renew.
        self.action_lock = self.commit_lock
        self.closing_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.seen_blocks: set[str] = set()
        self.last_filter_summary = ""
        self.no_order_notice_key: Optional[str] = None
        self.dom_lookup_retries: dict[tuple[str, str], int] = {}
        self.dom_page_recoveries: dict[tuple[str, str], int] = {}
        self.transient_api_failures = 0
        self._browser_session: Optional[JSTBrowser] = None
        self._browser_session_key: Optional[tuple[str, int]] = None

    def update_settings(self, settings: Settings) -> None:
        settings.validate()
        with self.settings_lock:
            if (
                self.thread
                and self.thread.is_alive()
                and settings.print_profile != self.settings.print_profile
            ):
                raise RuntimeError("运行中禁止切换面单类型；请先停止当前任务")
            self.settings = settings

    def _settings(self) -> Settings:
        with self.settings_lock:
            return Settings(**asdict(self.settings))

    def _browser_for(self, settings: Settings) -> JSTBrowser:
        """Reuse one raw-CDP/native-fetch session on the worker thread."""

        key = (settings.browser_name, settings.debug_port)
        if self._browser_session is not None and self._browser_session_key == key:
            if self._browser_session.is_healthy():
                return self._browser_session
            self._disconnect_browser()
        self._disconnect_browser()
        browser = JSTBrowser(settings)
        self._browser_session = browser
        self._browser_session_key = key
        return browser

    def _disconnect_browser(self) -> None:
        browser = self._browser_session
        self._browser_session = None
        self._browser_session_key = None
        if browser is not None:
            browser.disconnect()

    def _event(self, *args: Any, **kwargs: Any) -> None:
        self.notify(self.store.event(*args, **kwargs))

    def start(self) -> None:
        if getattr(self, "force_skip_in_progress", False):
            raise RuntimeError("正在保存强制跳过记录，请等待完成")
        if self.closing_event.is_set() or self.shutdown_event.is_set():
            raise RuntimeError("自动化引擎已永久关闭，禁止重新启动")
        previous: Optional[threading.Thread] = None
        with self.thread_lock:
            if getattr(self, "force_skip_in_progress", False):
                raise RuntimeError("正在保存强制跳过记录，请等待完成")
            if self.thread and self.thread.is_alive():
                if not self.stop_event.is_set():
                    self.run_event.set()
                    return
                previous = self.thread
        if previous and previous is not threading.current_thread():
            previous.join()
        with self.thread_lock:
            if getattr(self, "force_skip_in_progress", False):
                raise RuntimeError("正在保存强制跳过记录，请等待完成")
            if self.closing_event.is_set() or self.shutdown_event.is_set():
                raise RuntimeError("自动化引擎已永久关闭，禁止重新启动")
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.run_event.set()
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()

    def pause(self, reason: str = "用户暂停") -> None:
        # Linearize pause against the final renew/inspect, DOM proof and click
        # as one commit section. The RLock keeps same-thread callbacks safe;
        # pause may wait for the bounded readback but cannot deadlock it.
        with self.commit_lock:
            self.run_event.clear()
        self.set_status(f"已暂停：{reason}")

    def resume(self) -> None:
        with self.commit_lock:
            if getattr(self, "force_skip_in_progress", False):
                raise RuntimeError("正在保存强制跳过记录，请等待完成")
            if self.closing_event.is_set() or self.shutdown_event.is_set():
                raise RuntimeError("自动化引擎已永久关闭，禁止继续")
            needs_start = self.stop_event.is_set() or not (self.thread and self.thread.is_alive())
            if not needs_start:
                self.run_event.set()
                self.set_status("运行中")
        if needs_start:
            self.start()

    def stop(self, *, wait: bool = False) -> None:
        with self.commit_lock:
            self.stop_event.set()
            self.run_event.set()
        self.set_status("正在停止")
        thread = self.thread
        if wait and thread and thread is not threading.current_thread():
            thread.join()

    def request_shutdown(self) -> None:
        """Linearize the irreversible close request against an actual click."""

        self.closing_event.set()
        with self.commit_lock:
            self.shutdown_event.set()
            self.stop_event.set()
            self.run_event.set()

    def latch_shutdown(self) -> None:
        """Prevent lifecycle restart immediately while UI closes asynchronously."""

        self.closing_event.set()

    def shutdown(self, *, wait: bool = False) -> None:
        """Permanently stop this engine; start/resume/skip can never revive it."""

        self.request_shutdown()
        self.set_status("正在安全关闭")
        thread = self.thread
        if wait and thread and thread is not threading.current_thread():
            thread.join()
        if not thread or not thread.is_alive():
            self._disconnect_browser()

    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _require_operator_permission(self) -> None:
        if self.closing_event.is_set():
            raise OperatorStopped("程序关闭闩锁已设置，未执行后续按钮")
        if self.shutdown_event.is_set():
            raise OperatorStopped("程序正在关闭，未执行后续按钮")
        if self.stop_event.is_set():
            raise OperatorStopped("操作员已停止，未执行后续按钮")
        if not self.run_event.is_set():
            raise OperatorPaused("操作员已暂停，未执行后续按钮")

    @staticmethod
    def _claim(plan: dict[str, Any]) -> tuple[str, str, str]:
        o_id = str(plan.get("o_id", ""))
        io_id = str(plan.get("io_id", ""))
        claim_token = str(plan.get("claim_token", ""))
        if (
            not _is_ascii_order_id(o_id)
            or not _is_ascii_order_id(io_id)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", claim_token)
        ):
            raise PermanentJobError("本地任务缺少 claimed V1 租约身份，禁止执行")
        return o_id, io_id, claim_token

    def _renew_step(self, planner: PlannerClient, plan: dict[str, Any]) -> None:
        self._require_operator_permission()
        o_id, io_id, claim_token = self._claim(plan)
        planner.renew(o_id, io_id, claim_token)
        self._require_operator_permission()

    def _inspect(self, planner: PlannerClient, plan: dict[str, Any]) -> dict[str, Any]:
        o_id, io_id, claim_token = self._claim(plan)
        return planner.inspect(o_id, io_id, claim_token)

    def _complete_claim(
        self,
        planner: PlannerClient,
        plan: dict[str, Any],
        completion_reason: str,
    ) -> None:
        o_id, io_id, claim_token = self._claim(plan)
        planner.complete(o_id, io_id, claim_token, completion_reason)

    def _release_retryable_job(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        release_status: str,
        reason: str,
    ) -> None:
        """Release a proven pre-click job without permanently excluding it.

        A different paper profile or an upstream status change must not stop
        the whole workstation, but it also must never be printed under the
        operator's current paper choice.  The local row remains as an audit
        record and may be atomically replaced by a newly claimed plan later.
        """

        if release_status not in RETRYABLE_RELEASED_JOB_STATUSES:
            raise ValueError("可重领释放状态无效")
        if str(job.get("status", "")) == "RUNNING":
            raise SafetyStop("已提交任务只能只读恢复，禁止释放为可重领状态")
        plan = job.get("plan")
        if not isinstance(plan, dict):
            raise PermanentJobError("本地任务计划无效，禁止释放租约")
        o_id, io_id, claim_token = self._claim(plan)
        final_status = release_status
        final_reason = str(reason)
        try:
            planner.release(o_id, io_id, claim_token)
        except LeaseLostError:
            # The server no longer accepts this token, so there is no remote
            # action to repeat.  Keep the pair retryable instead of placing it
            # on the permanent local exclusion list.
            final_status = "RELEASED_LEASE_LOST"
            final_reason += "；原租约已失效，等待后台重新领取"
        if not self.store.update_job(
            o_id,
            io_id,
            step_index=int(job.get("step_index", 0)),
            status=final_status,
        ):
            raise SafetyStop("租约已释放，但本地可重领状态写入失败")
        self._event(
            "WARN",
            final_status,
            final_reason + "；未执行任何取号、改快递或打印动作，继续下一单",
            o_id=o_id,
            io_id=io_id,
        )

    def _persisted_terminal_completion_reason(
        self,
        planner: PlannerClient,
        plan: dict[str, Any],
        local_status: str,
    ) -> str:
        """Re-prove evidence-bearing terminal reasons before server sync."""

        if local_status == "SKIPPED_OPERATOR":
            return "OPERATOR_SKIPPED"
        if local_status in {
            "SKIPPED_UNCERTAIN_PRINT",
            "SKIPPED_UNCERTAIN_WRITE",
        }:
            return "UNCERTAIN_ACTION"
        latest = self._inspect(planner, plan)
        o_id, io_id, _token = self._claim(plan)
        self._require_job_identity(latest, o_id, io_id)
        if local_status in {"COMPLETED", "SKIPPED_ALREADY_PRINTED"}:
            if latest.get("has_print_action") is not True:
                raise SafetyStop(
                    "本地记录为已打印，但后台实时回读未证明打印动作，禁止同步终态"
                )
            return "PRINTED"
        if local_status in {
            "SKIPPED_SHIPPED",
            "SKIPPED_TERMINAL_STATUS",
            "SKIPPED_DELETED",
        }:
            if not self._is_allowlisted_terminal_status(latest):
                raise SafetyStop(
                    "本地记录为已发货/终态，但后台当前状态不是 Sent/Delete，禁止同步终态"
                )
            return "TERMINAL"
        raise SafetyStop("本地任务终态无对应的后台完成原因")

    def _auto_pause(self, reason: str, plan: Optional[dict[str, Any]] = None) -> None:
        self.run_event.clear()
        self._event(
            "BLOCKED",
            "AUTO_PAUSE",
            reason,
            o_id=str((plan or {}).get("o_id", "")),
            io_id=str((plan or {}).get("io_id", "")),
        )
        self.set_status(f"自动暂停：{reason}")

    def _wait(self, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.stop_event.is_set() or self.shutdown_event.is_set():
                return False
            if not self.run_event.is_set():
                return True
            time.sleep(min(0.25, deadline - time.time()))
        return True

    def _wait_until_running(self) -> bool:
        while not self.stop_event.is_set() and not self.shutdown_event.is_set():
            if self.run_event.wait(timeout=0.5):
                return True
        return False

    def _dom_page_recovery_state(self) -> dict[tuple[str, str], int]:
        """Return the recovery counter, including migrated/test engine objects."""

        state = getattr(self, "dom_page_recoveries", None)
        if not isinstance(state, dict):
            state = {}
            self.dom_page_recoveries = state
        return state

    def _clear_dom_recovery(self, identity: tuple[str, str]) -> None:
        self.dom_lookup_retries.pop(identity, None)
        self._dom_page_recovery_state().pop(identity, None)

    def _retry_missing_dom_row(self, job: dict[str, Any], reason: str) -> bool:
        """Return a pre-commit native-query miss to PENDING for bounded retry.

        A page reload can briefly make LoadDataToJSON unavailable. No browser
        business write has happened while the local job is PREPARING, so it
        is safe to renew/read back and retry the exact API lookup. This is not
        a skippable business conflict: a transient page-interface failure must
        never permanently exclude a valid order from every workstation.
        """

        if str(job.get("status", "")) == "RUNNING":
            raise SafetyStop("接口订单在业务动作提交后消失，禁止自动重试")
        # Keep the same raw-CDP WebSocket on read-only lookup retries so page
        # recovery remains cheap and does not disturb the dedicated session.
        o_id = str(job.get("o_id", ""))
        io_id = str(job.get("io_id", ""))
        index = int(job.get("step_index", 0))
        key = (o_id, io_id)
        retry = self.dom_lookup_retries.get(key, 0) + 1
        self.dom_lookup_retries[key] = retry
        if retry >= DOM_LOOKUP_MAX_ATTEMPTS:
            self.dom_lookup_retries.pop(key, None)
            page_recoveries = self._dom_page_recovery_state()
            recovery = page_recoveries.get(key, 0)
            if recovery < DOM_PAGE_RECOVERY_MAX_ATTEMPTS:
                recovery += 1
                page_recoveries[key] = recovery
                if not self.store.update_job(
                    o_id, io_id, step_index=index, status="PENDING"
                ):
                    raise SafetyStop("接口页恢复前任务状态无法回到待重试")
                recovered = False
                recovery_error = ""
                try:
                    browser = self._browser_for(self._settings())
                    browser.recover_print_page(
                        replace_page=(
                            recovery == DOM_PAGE_RECOVERY_MAX_ATTEMPTS
                        )
                    )
                    recovered = True
                except Exception as exc:
                    recovery_error = str(exc)
                    session = self._browser_session
                    if session is not None and not session.is_healthy():
                        self._disconnect_browser()
                delay = DOM_PAGE_RECOVERY_DELAYS[recovery - 1]
                message = (
                    f"连续 {retry} 次无法唯一定位订单 {o_id}/{io_id}；"
                    f"已在专用浏览器中第 {recovery}/"
                    f"{DOM_PAGE_RECOVERY_MAX_ATTEMPTS} 次重新进入打单拣货，"
                    f"{delay:g} 秒后继续精确查询。"
                )
                if recovered:
                    message += "打单 iframe 接口与仓库已重新确认。"
                else:
                    message += f"本次接口页恢复尚未就绪：{recovery_error}"
                self._event(
                    "WARN",
                    "DOM_PAGE_RECOVERY",
                    message,
                    o_id=o_id,
                    io_id=io_id,
                    detail={"recovery": recovery, "ready": recovered},
                )
                self.set_status(
                    f"打单接口页已自动恢复第 {recovery}/"
                    f"{DOM_PAGE_RECOVERY_MAX_ATTEMPTS} 次，{delay:g} 秒后重查"
                )
                return self._wait(delay)
            # The iframe has remained unprovable through both ordinary retry
            # cycles and bounded same-session page recoveries.  At this point
            # continuing would risk operating in a wrong warehouse/filter.
            page_recoveries.pop(key, None)
            message = (
                f"自动重进打单页后仍连续 {retry} 次无法唯一定位订单 "
                f"{o_id}/{io_id}，已暂停而不是跳过。请检查当前仓库、"
                f"登录态、目标仓库和 LoadDataToJSON 接口状态。"
                f"最后诊断：{reason}"
            )
            self._event(
                "BLOCKED",
                "DOM_ENVIRONMENT_PAUSE",
                message,
                o_id=o_id,
                io_id=io_id,
            )
            self._pause_resumable_job(
                job, message, pause_kind="DOM_ENVIRONMENT"
            )
            return False
        if not self.store.update_job(
            o_id, io_id, step_index=index, status="PENDING"
        ):
            raise SafetyStop("原生接口未找到订单且任务状态无法恢复为待重试")
        delay = DOM_LOOKUP_RETRY_DELAYS[retry - 1]
        message = (
            f"页面原生接口暂未返回目标订单，{delay:g} 秒后自动重新回读并查询"
            f"（第 {retry}/{DOM_LOOKUP_MAX_ATTEMPTS} 次）；"
            f"未提交改快递、取号或打印。诊断：{reason}"
        )
        self._event(
            "WARN",
            "DOM_LOOKUP_RETRY",
            message,
            o_id=o_id,
            io_id=io_id,
        )
        self.set_status(
            f"接口订单暂未加载：{delay:g} 秒后自动重试"
            f"（第 {retry}/{DOM_LOOKUP_MAX_ATTEMPTS} 次）"
        )
        return self._wait(delay)

    def _retry_transient_api(
        self, job: Optional[dict[str, Any]], reason: str
    ) -> bool:
        """Keep a transient backend outage recoverable without repeating ERP writes."""

        self.transient_api_failures += 1
        streak = self.transient_api_failures
        delay = TRANSIENT_API_RETRY_DELAYS[
            min(streak - 1, len(TRANSIENT_API_RETRY_DELAYS) - 1)
        ]
        o_id = str((job or {}).get("o_id", ""))
        io_id = str((job or {}).get("io_id", ""))
        if job and str(job.get("status", "")) != "RUNNING":
            if not self.store.update_job(
                o_id,
                io_id,
                step_index=int(job.get("step_index", 0)),
                status="PENDING",
            ):
                raise SafetyStop("网络恢复前任务状态无法回到待重试")
        message = (
            f"后台网络暂不可用（连续 {streak} 次）：{reason}；"
            f"{delay:g} 秒后自动重试。未重复任何取号、改快递或打印按钮"
        )
        self._event(
            "WARN",
            "API_TRANSIENT_RETRY",
            message,
            o_id=o_id,
            io_id=io_id,
            detail={"streak": streak, "delay_seconds": delay},
        )
        self.set_status(f"网络短暂异常：{delay:g} 秒后自动恢复（第 {streak} 次）")
        return self._wait(delay)

    def _defer_new_running_action_recovery(
        self,
        attempted_job: Optional[dict[str, Any]],
        active: Optional[dict[str, Any]],
        reason: str,
    ) -> bool:
        """Give a newly committed side effect one backend-only recovery round."""

        if not active or str(active.get("status", "")) != "RUNNING":
            return False
        if str((attempted_job or {}).get("status", "")) == "RUNNING":
            return False
        # The queue snapshot was pre-commit, while the durable row is RUNNING:
        # the side-effect boundary was crossed before the exception. Returning
        # it to PENDING risks a duplicate click; retiring it immediately can
        # misclassify a delayed success. The next _process_job round enters its
        # RUNNING branch and performs backend readback only.
        self._event(
            "WARN",
            "RUNNING_ACTION_READBACK_RECOVERY",
            "动作提交后响应异常；保持 RUNNING，2 秒后仅回读"
            "后台结果，绝不重复点击：" + reason,
            o_id=str(active.get("o_id", "")),
            io_id=str(active.get("io_id", "")),
        )
        self.set_status("已提交动作响应异常：正在只读恢复")
        self._wait(2.0)
        return True

    def _poll_readback(
        self,
        planner: PlannerClient,
        o_id: str,
        io_id: str,
        claim_token: str,
        predicate: Callable[[dict[str, Any]], bool],
        description: str,
        attempts: int = 8,
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}
        subject = f"{description}（{o_id}/{io_id}）"
        for attempt in range(attempts):
            last = planner.inspect(o_id, io_id, claim_token)
            if not last.get("found"):
                if attempt + 1 < attempts:
                    # JST may temporarily hide a row while a carrier/waybill
                    # write is committing. Retry only the exact readback; never
                    # repeat the browser action.
                    planner.renew(o_id, io_id, claim_token)
                    time.sleep(min(1.5, 0.5 + attempt * 0.25))
                    continue
                raise SafetyStop(
                    f"{subject}连续 {attempts} 次回读均暂未找到精确订单"
                )
            self._require_job_identity(last, o_id, io_id)
            if self._is_allowlisted_terminal_status(last):
                raise ExternalTerminalStatusDetected(last)
            if last.get("has_ship_action"):
                raise ExternalShipmentDetected(last)
            self._require_waitconfirm_status(last)
            if predicate(last):
                return last
            time.sleep(min(1.5, 0.5 + attempt * 0.25))
        raise SafetyStop(f"{subject}回读未通过：{json.dumps(last, ensure_ascii=False)}")

    def _batch_preflight_readbacks(
        self, planner: PlannerClient, jobs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Renew and inspect a batch once, retaining a safe test/diagnostic fallback."""

        batch_inspect = getattr(planner, "inspect_batch", None)
        if callable(batch_inspect):
            credentials = [self._claim(job["plan"]) for job in jobs]
            self._require_operator_permission()
            try:
                readbacks = batch_inspect(credentials)
            except LeaseLostError:
                # A batch lease failure is intentionally retried one-by-one
                # before any browser side effect so the exact failed order is
                # attributed without weakening the all-or-nothing batch renew.
                readbacks = []
                for job in jobs:
                    with self._job_exception_scope(job):
                        self._renew_step(planner, job["plan"])
                        readbacks.append(self._inspect(planner, job["plan"]))
            self._require_operator_permission()
            if len(readbacks) != len(jobs):
                raise BackendSchemaError("后台批量预检数量与任务数量不一致")
            return readbacks

        # Older unit-test doubles and diagnostic adapters may not implement the
        # network batch method. Production startup requires the current schema-5
        # batch contract, so the live path always uses one call.
        readbacks = []
        for job in jobs:
            with self._job_exception_scope(job):
                self._renew_step(planner, job["plan"])
                readbacks.append(self._inspect(planner, job["plan"]))
        return readbacks

    def _poll_batch_readbacks(
        self,
        planner: PlannerClient,
        checks: list[
            tuple[dict[str, Any], Callable[[dict[str, Any]], bool]]
        ],
        description: str,
        *,
        attempts: int = 8,
        on_resolved: Optional[
            Callable[[dict[str, Any], dict[str, Any]], None]
        ] = None,
    ) -> tuple[
        dict[tuple[str, str], dict[str, Any]], Optional[BaseException]
    ]:
        """Poll every unresolved order together so delay is per round, not per order."""

        batch_inspect = getattr(planner, "inspect_batch", None)
        if not callable(batch_inspect):
            resolved: dict[tuple[str, str], dict[str, Any]] = {}
            for job, predicate in checks:
                plan = job["plan"]
                o_id, io_id, claim_token = self._claim(plan)
                try:
                    with self._job_exception_scope(job):
                        readback = self._poll_readback(
                            planner,
                            o_id,
                            io_id,
                            claim_token,
                            predicate,
                            description,
                            attempts=attempts,
                        )
                except (
                    ExternalTerminalStatusDetected,
                    ExternalShipmentDetected,
                ) as exc:
                    readback = exc.readback
                except Exception as exc:
                    return resolved, exc
                resolved[(o_id, io_id)] = readback
                if on_resolved is not None:
                    with self._job_exception_scope(job):
                        on_resolved(job, readback)
            return resolved, None

        pending: dict[
            tuple[str, str],
            tuple[dict[str, Any], Callable[[dict[str, Any]], bool]],
        ] = {}
        for job, predicate in checks:
            o_id, io_id, _claim_token = self._claim(job["plan"])
            pair = (o_id, io_id)
            if pair in pending:
                raise SafetyStop("批量回读任务身份重复")
            pending[pair] = (job, predicate)
        resolved: dict[tuple[str, str], dict[str, Any]] = {}
        last: dict[tuple[str, str], dict[str, Any]] = {}

        for attempt in range(max(1, attempts)):
            ordered_pairs = list(pending)
            credentials = [self._claim(pending[pair][0]["plan"]) for pair in ordered_pairs]
            try:
                readbacks = batch_inspect(credentials)
            except LeaseLostError:
                # Read-only exact fallback identifies the affected running job;
                # it never repeats the already committed browser action.
                for pair in ordered_pairs:
                    job, predicate = pending[pair]
                    plan = job["plan"]
                    o_id, io_id, claim_token = self._claim(plan)
                    try:
                        with self._job_exception_scope(job):
                            readback = self._poll_readback(
                                planner,
                                o_id,
                                io_id,
                                claim_token,
                                predicate,
                                description,
                                attempts=max(1, attempts - attempt),
                            )
                    except (
                        ExternalTerminalStatusDetected,
                        ExternalShipmentDetected,
                    ) as exc:
                        readback = exc.readback
                    except Exception as exc:
                        return resolved, exc
                    resolved[pair] = readback
                    if on_resolved is not None:
                        with self._job_exception_scope(job):
                            on_resolved(job, readback)
                return resolved, None
            except Exception as exc:
                scoped_identity = getattr(exc, "_jst_job_identity", None)
                if (
                    isinstance(scoped_identity, tuple)
                    and len(scoped_identity) == 2
                ):
                    raise
                raise CommittedBatchUncertain(
                    [pending[pair][0] for pair in ordered_pairs],
                    f"{description}批量只读回查失败",
                ) from exc
            if len(readbacks) != len(ordered_pairs):
                raise CommittedBatchUncertain(
                    [pending[pair][0] for pair in ordered_pairs],
                    f"{description}批量回读数量不一致",
                )

            next_pending: dict[
                tuple[str, str],
                tuple[dict[str, Any], Callable[[dict[str, Any]], bool]],
            ] = {}
            round_resolved: dict[
                tuple[str, str], tuple[dict[str, Any], dict[str, Any]]
            ] = {}
            round_failure: Optional[BaseException] = None
            for pair, readback in zip(ordered_pairs, readbacks):
                job, predicate = pending[pair]
                last[pair] = readback
                try:
                    with self._job_exception_scope(job):
                        if not readback.get("found"):
                            next_pending[pair] = (job, predicate)
                            continue
                        self._require_job_identity(readback, pair[0], pair[1])
                        if (
                            self._is_allowlisted_terminal_status(readback)
                            or readback.get("has_ship_action")
                        ):
                            round_resolved[pair] = (job, readback)
                            continue
                        self._require_waitconfirm_status(readback)
                        if predicate(readback):
                            round_resolved[pair] = (job, readback)
                        else:
                            next_pending[pair] = (job, predicate)
                except Exception as exc:
                    # Classify the whole authoritative response before
                    # surfacing one bad item. Independently proven siblings in
                    # this same round must still be settled.
                    if round_failure is None:
                        round_failure = exc

            # Record every proof before callbacks, then isolate callback
            # failures so one local settlement cannot suppress its siblings.
            for pair, (_job, readback) in round_resolved.items():
                resolved[pair] = readback
            if on_resolved is not None:
                for job, readback in round_resolved.values():
                    try:
                        with self._job_exception_scope(job):
                            on_resolved(job, readback)
                    except Exception as exc:
                        if round_failure is None:
                            round_failure = exc

            if round_failure is not None:
                return resolved, round_failure
            pending = next_pending
            if not pending:
                return resolved, None
            if round_resolved:
                # Return after a partial committed-waybill settlement. The
                # worker prioritizes the newly PENDING print job while its
                # unresolved siblings stay RUNNING for read-only recovery.
                return resolved, None
            if attempt + 1 < max(1, attempts):
                time.sleep(min(1.5, 0.5 + attempt * 0.25))

        failed_pair = next(iter(pending))
        failure = SafetyStop(
            f"{description}（{failed_pair[0]}/{failed_pair[1]}）批量回读未通过："
            f"{json.dumps(last.get(failed_pair, {}), ensure_ascii=False)}"
        )
        setattr(failure, "_jst_job_identity", failed_pair)
        return resolved, failure

    @staticmethod
    def _require_job_identity(
        readback: dict[str, Any], o_id: str, io_id: str
    ) -> None:
        if not readback.get("found"):
            raise PermanentJobError("后台未找到待处理订单")
        if not _is_ascii_order_id(str(o_id)) or not _is_ascii_order_id(str(io_id)):
            raise PermanentJobError("当前任务缺少有效订单复合身份")
        if str(readback.get("o_id", "")) != str(o_id):
            raise PermanentJobError("后台回读的内部订单号与当前任务不一致")
        if str(readback.get("io_id", "")) != str(io_id):
            raise PermanentJobError("后台回读的出库单号与当前任务不一致")

    @staticmethod
    def _terminal_status_kind(readback: dict[str, Any]) -> Optional[str]:
        """Return an exact reviewed terminal kind, never a fuzzy match."""

        value = readback.get("status")
        if not isinstance(value, str):
            return None
        return ALLOWLISTED_TERMINAL_STATUS_KINDS.get(value.strip().lower())

    @classmethod
    def _is_allowlisted_terminal_status(cls, readback: dict[str, Any]) -> bool:
        # Sent is a completed shipping status. Delete is a cancelled/deleted
        # order that can no longer be printed. Similar or unknown values remain
        # fail-closed; only these exact normalized tokens are auto-retired.
        return cls._terminal_status_kind(readback) is not None

    @classmethod
    def _require_waitconfirm_status(cls, readback: dict[str, Any]) -> None:
        if cls._is_allowlisted_terminal_status(readback):
            raise ExternalTerminalStatusDetected(readback)
        if str(readback.get("status", "")).strip().lower() != "waitconfirm":
            raise UnknownOrderStatus(f"订单状态已变化：{readback.get('status')}")

    def _skip_terminal_status_job(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        readback: dict[str, Any],
    ) -> None:
        """Retire exact Sent/Delete; recover SKU only for Sent with PRINT_OK."""

        plan = job["plan"]
        index = int(job["step_index"])
        o_id, io_id, _claim_token = self._claim(plan)
        self._require_job_identity(readback, o_id, io_id)
        terminal_kind = self._terminal_status_kind(readback)
        if terminal_kind is None:
            raise PermanentJobError(
                f"订单状态已变化：{readback.get('status')}"
            )
        print_was_confirmed = terminal_kind == "SENT" and self.store.has_event(
            "PRINT_OK", o_id, io_id
        )
        inserted = 0
        if print_was_confirmed:
            try:
                self._validate_items_unchanged(plan, readback)
                inserted = self.store.record_sku_outbound(readback)
            except PermanentJobError as exc:
                raise TerminalStatusSKUValidationError(
                    f"Sent 订单存在本软件 PRINT_OK，但 SKU 明细无法严格恢复：{exc}"
                ) from exc
        # Remote confirmation is authoritative. If this fails, do not change
        # local job state or claim that the status-only task was retired. SKU
        # insertion is idempotent and will be checked again on a retry.
        self._complete_claim(planner, plan, "TERMINAL")
        local_status = (
            "SKIPPED_TERMINAL_STATUS"
            if terminal_kind == "SENT"
            else "SKIPPED_DELETED"
        )
        self.store.update_job(o_id, io_id, step_index=index, status=local_status)
        if terminal_kind == "SENT":
            message = (
                "后台只读回读确认订单状态为 Sent；已终结自动任务并继续下一单，"
                "未执行浏览器动作；"
                + (
                    f"本软件 PRINT_OK 已核验，SKU 明细已保留（新增 {inserted} 条）"
                    if print_was_confirmed
                    else "无本软件 PRINT_OK，不计入本软件 SKU 统计"
                )
            )
        else:
            message = (
                "后台只读回读确认订单状态为 Delete；订单已删除/取消，"
                "已终结自动任务并继续下一单；未执行浏览器动作，"
                "不计入本软件 SKU 统计"
            )
        self._event(
            "WARN",
            local_status,
            message,
            o_id=o_id,
            io_id=io_id,
            detail={
                "status": str(readback.get("status", "")),
                "terminal_kind": terminal_kind,
                "print_ok": print_was_confirmed,
                "sku_inserted": inserted,
            },
        )

    def _settle_confirmed_status_job(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        readback: dict[str, Any],
    ) -> bool:
        """Remove Confirmed from this queue without treating it as print proof.

        A historical RUNNING row may already have submitted an action. Never
        release it for replay, or turn it into an operator-skippable PAUSED row.
        Confirm UNCERTAIN_ACTION remotely before retiring the local task.
        """

        status = readback.get("status")
        if not isinstance(status, str) or status.strip().lower() != "confirmed":
            return False
        o_id, io_id, _token = self._claim(job["plan"])
        self._require_job_identity(readback, o_id, io_id)
        reason = "后台订单状态已变化为 Confirmed，已不满足 WaitConfirm 自动处理条件"
        if str(job.get("status", "")) == "RUNNING":
            step = self._job_step(job)
            reason += "；之前提交的动作结果待人工核对，不作为打印成功凭据"
            if step == "PRINT_EXPRESS":
                self._skip_uncertain_print_job(planner, job, reason)
            elif step == "GET_WAYBILL" or step.startswith("RESET_CARRIER"):
                self._skip_uncertain_write_job(planner, job, reason)
            else:
                raise UnknownOrderStatus(f"Confirmed 订单无法恢复步骤：{step}")
        else:
            self._release_retryable_job(
                planner, job, "RELEASED_STATUS_CHANGED", reason
            )
        return True

    def _skip_shipped_job(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        readback: dict[str, Any],
    ) -> None:
        """Retire a locally saved job after an external shipment is confirmed.

        No browser action is performed here. SKU rows are recovered only when a
        persisted PRINT_OK event proves that this assistant completed print
        readback for this exact internal/outbound order pair.
        """

        if not self._is_allowlisted_terminal_status(readback):
            raise CompletionProofError(
                "操作记录出现预发货/发货字样，但当前订单仍不是 Sent/Delete；"
                "记录可能失败、取消或属于历史记录，已暂停且不会误报为完成"
            )

        plan = job["plan"]
        index = int(job["step_index"])
        o_id = str(plan["o_id"])
        io_id = str(plan.get("io_id", ""))
        print_was_confirmed = (
            bool(readback.get("has_print_action"))
            and self.store.has_event("PRINT_OK", o_id, io_id)
        )
        inserted = 0
        sku_recovery_error = ""
        if print_was_confirmed:
            try:
                self._validate_items_unchanged(plan, readback)
                inserted = self.store.record_sku_outbound(readback)
            except Exception as exc:
                # A shipped order must never remain the next resumable browser job.
                # Keep the statistics gap visible, but retire the unsafe task.
                sku_recovery_error = str(exc)

        self._complete_claim(planner, plan, "TERMINAL")
        self.store.update_job(
            o_id, io_id, step_index=index, status="SKIPPED_SHIPPED"
        )
        if sku_recovery_error:
            sku_note = f"；SKU 明细恢复失败（{sku_recovery_error}），请按异常记录补核"
        elif print_was_confirmed:
            sku_note = f"；打印回读已确认，SKU 明细已保留（新增 {inserted} 条）"
        else:
            sku_note = "；该任务未证明由本软件完成打印，不计入本软件 SKU 统计"
        self._event(
            "WARN",
            "SKIPPED_SHIPPED",
            "后台确认该单已预发货/发货，已自动跳过且不会再次恢复；"
            "本软件未执行发货按钮" + sku_note,
            o_id=o_id,
            io_id=io_id,
            detail={
                "status": str(readback.get("status", "")),
                "has_ship_action": bool(readback.get("has_ship_action")),
                "has_print_action": bool(readback.get("has_print_action")),
                "action_history_complete": bool(
                    readback.get("action_history_complete")
                ),
            },
        )

    def _skip_already_printed_job(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        readback: dict[str, Any],
    ) -> None:
        """Retire an order printed outside this assistant without counting SKU."""

        plan = job["plan"]
        index = int(job["step_index"])
        o_id = str(plan["o_id"])
        io_id = str(plan.get("io_id", ""))
        self._complete_claim(planner, plan, "PRINTED")
        self.store.update_job(
            o_id, io_id, step_index=index, status="SKIPPED_ALREADY_PRINTED"
        )
        self._event(
            "WARN",
            "SKIPPED_ALREADY_PRINTED",
            "后台已出现打印动作，但没有本软件 PRINT_OK 凭据；"
            "已自动跳过以防重复打印，不计入本软件 SKU 统计",
            o_id=o_id,
            io_id=io_id,
            detail={
                "status": str(readback.get("status", "")),
                "has_print_action": bool(readback.get("has_print_action")),
                "action_history_complete": bool(
                    readback.get("action_history_complete")
                ),
            },
        )

    def _skip_uncertain_print_job(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        message: str,
    ) -> None:
        """Never retry a PRINT step that may already have clicked before a crash."""

        plan = job["plan"]
        index = int(job["step_index"])
        o_id = str(plan["o_id"])
        io_id = str(plan.get("io_id", ""))
        self._complete_claim(planner, plan, "UNCERTAIN_ACTION")
        if not self.store.update_job(
            o_id, io_id, step_index=index, status="SKIPPED_UNCERTAIN_PRINT"
        ):
            raise SafetyStop("后台已确认不确定打印任务，但本地状态写入失败；需只读恢复")
        self._event(
            "BLOCKED",
            "SKIPPED_UNCERTAIN_PRINT",
            message + "；已终结自动任务并继续下一单，请人工核对该单，程序不会自动重打",
            o_id=o_id,
            io_id=io_id,
        )

    def _recover_running_print(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        first_readback: dict[str, Any],
        *,
        attempts: int = 6,
        interval: float = 1.5,
    ) -> None:
        """Resolve a PRINT job left RUNNING without ever clicking again."""

        plan = job["plan"]
        index = int(job["step_index"])
        o_id = str(plan["o_id"])
        io_id = str(plan.get("io_id", ""))
        latest = first_readback
        for attempt in range(max(1, attempts)):
            if not latest.get("found"):
                if attempt + 1 < max(1, attempts):
                    planner.renew(o_id, io_id, str(plan.get("claim_token", "")))
                    time.sleep(max(0.0, interval))
                    latest = planner.inspect(
                        o_id, io_id, str(plan.get("claim_token", ""))
                    )
                    continue
                self._skip_uncertain_print_job(
                    planner,
                    job,
                    "恢复打印任务时连续只读回查仍未找到精确订单",
                )
                return
            self._require_job_identity(latest, o_id, io_id)
            if self._is_allowlisted_terminal_status(latest):
                self._skip_terminal_status_job(planner, job, latest)
                return
            if self._settle_confirmed_status_job(planner, job, latest):
                return
            if latest.get("has_ship_action"):
                self._skip_shipped_job(planner, job, latest)
                return
            self._require_waitconfirm_status(latest)
            if self.store.has_event("PRINT_OK", o_id, io_id):
                self.store.update_job(
                    o_id, io_id, step_index=index + 1, status="PENDING"
                )
                self._event(
                    "WARN",
                    "RECOVERED_APP_PRINT",
                    "恢复到本软件持久化的 PRINT_OK 凭据，未再次点击打印",
                    o_id=o_id,
                    io_id=io_id,
                )
                return
            if latest.get("has_print_action"):
                if self._recover_locally_committed_print(planner, job, latest):
                    return
                self._skip_already_printed_job(planner, job, latest)
                return
            if latest.get("is_print_express"):
                self._skip_uncertain_print_job(
                    planner,
                    job,
                    "恢复中的打印任务已出现聚水潭打印标记，但无本软件 PRINT_OK 凭据",
                )
                return
            if attempt + 1 < max(1, attempts):
                time.sleep(max(0.0, interval))
                latest = planner.inspect(o_id, io_id, str(plan.get("claim_token", "")))
        self._skip_uncertain_print_job(
            planner,
            job,
            "上次运行在打印步骤中断，等待回读后仍无法证明是否已经点击打印",
        )

    def _skip_uncertain_write_job(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        message: str,
    ) -> None:
        plan = job["plan"]
        index = int(job["step_index"])
        o_id, io_id, _claim_token = self._claim(plan)
        self._complete_claim(planner, plan, "UNCERTAIN_ACTION")
        if not self.store.update_job(
            o_id, io_id, step_index=index, status="SKIPPED_UNCERTAIN_WRITE"
        ):
            raise SafetyStop("后台已确认不确定取号/改快递任务，但本地状态写入失败；需只读恢复")
        self._event(
            "BLOCKED",
            "SKIPPED_UNCERTAIN_WRITE",
            message + "；已终结自动任务并继续下一单，程序绝不盲目重做改快递/取号",
            o_id=o_id,
            io_id=io_id,
        )

    def _recover_running_write(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        first_readback: dict[str, Any],
        step: str,
        *,
        attempts: int = 6,
        interval: float = 1.5,
    ) -> None:
        plan = job["plan"]
        index = int(job["step_index"])
        o_id, io_id, claim_token = self._claim(plan)
        latest = first_readback
        for attempt in range(max(1, attempts)):
            if not latest.get("found"):
                if attempt + 1 < max(1, attempts):
                    planner.renew(o_id, io_id, claim_token)
                    time.sleep(max(0.0, interval))
                    latest = planner.inspect(o_id, io_id, claim_token)
                    continue
                self._skip_uncertain_write_job(
                    planner,
                    job,
                    f"恢复 {step} 时连续只读回查仍未找到精确订单",
                )
                return
            self._require_job_identity(latest, o_id, io_id)
            if self._is_allowlisted_terminal_status(latest):
                self._skip_terminal_status_job(planner, job, latest)
                return
            if self._settle_confirmed_status_job(planner, job, latest):
                return
            if latest.get("has_ship_action"):
                self._skip_shipped_job(planner, job, latest)
                return
            self._require_waitconfirm_status(latest)
            if step.startswith("RESET_CARRIER"):
                target_id, target_name = reset_target(step)
                completed = (
                    latest.get("carrier_id") == target_id
                    and latest.get("carrier_name") == target_name
                    and latest.get("privacy_required")
                    is (target_id == PRIVACY_CARRIER_ID)
                    and latest.get("has_waybill") is True
                )
            else:
                expected_id = str(plan.get("current_carrier_id", ""))
                expected_name = str(plan.get("current_carrier", ""))
                carrier_exact = (
                    latest.get("carrier_id") == expected_id
                    and latest.get("carrier_name") == expected_name
                    and latest.get("privacy_required")
                    is bool(plan.get("privacy_required"))
                )
                if latest.get("has_waybill") is True and not carrier_exact:
                    self._skip_uncertain_write_job(
                        planner,
                        job,
                        "恢复取号任务时发现运单已落在错误快递或隐私规则已变化",
                    )
                    return
                completed = latest.get("has_waybill") is True and carrier_exact
            if completed:
                self.store.update_job(
                    o_id, io_id, step_index=index + 1, status="PENDING"
                )
                self._event(
                    "WARN",
                    "RECOVERED_COMPLETED_STEP",
                    f"恢复时只读确认步骤已完成，未重复点击：{step}",
                    o_id=o_id,
                    io_id=io_id,
                )
                return
            if attempt + 1 < max(1, attempts):
                time.sleep(max(0.0, interval))
                latest = planner.inspect(o_id, io_id, claim_token)
        self._skip_uncertain_write_job(
            planner, job, f"上次运行在 {step} 按钮阶段中断，回读仍无法证明动作结果"
        )

    def _retire_lost_lease(self, job: dict[str, Any], message: str) -> None:
        plan = job["plan"]
        index = int(job["step_index"])
        o_id = str(plan.get("o_id", ""))
        io_id = str(plan.get("io_id", ""))
        running = str(job.get("status", "")) == "RUNNING"
        status = "SKIPPED_UNCERTAIN_LEASE_LOST" if running else "RELEASED_LEASE_LOST"
        self.store.update_job(o_id, io_id, step_index=index, status=status)
        if running:
            self.store.exclude_job(o_id, io_id, message)
        self._event(
            "BLOCKED" if running else "WARN",
            status,
            (
                message + "；已提交动作无法安全重放，本机永久排除该订单"
                if running
                else message + "；尚未提交任何动作，等待后台重新领取并继续"
            ),
            o_id=o_id,
            io_id=io_id,
        )

    @classmethod
    def _validate_live_route(
        cls, plan: dict[str, Any], readback: dict[str, Any]
    ) -> None:
        """Prove that current weight/shop/privacy still select the planned paper."""

        if readback.get("warehouse_id") != TARGET_WAREHOUSE_ID:
            raise PermanentJobError("订单实时仓库已变化，禁止继续自动打单")
        live_weight = readback.get("weight_kg")
        planned_weight = plan.get("weight_kg")
        if (
            isinstance(live_weight, bool)
            or isinstance(planned_weight, bool)
            or not isinstance(live_weight, (int, float))
            or not isinstance(planned_weight, (int, float))
            or not math.isfinite(float(live_weight))
            or not math.isfinite(float(planned_weight))
            or float(live_weight) <= 0
            or float(planned_weight) <= 0
        ):
            raise PermanentJobError("订单实时重量或计划重量无效")
        if not math.isclose(
            float(live_weight), float(planned_weight), rel_tol=1e-9, abs_tol=1e-6
        ):
            raise PermanentJobError("订单实时重量与领取计划不一致，需重新领取")
        shop_name = readback.get("shop_name")
        if not isinstance(shop_name, str):
            raise PermanentJobError("订单实时店铺名称无效")
        if readback.get("privacy_required") is True:
            live_profile = PRIVACY_CARRIER_ID
        elif float(live_weight) > WEIGHT_THRESHOLD_KG or any(
            token in shop_name for token in KEEP_STO_SHOP_TOKENS
        ):
            live_profile = SOURCE_CARRIER_ID
        else:
            live_profile = TARGET_CARRIER_ID
        if plan_print_profile(plan) != live_profile:
            raise PermanentJobError("订单实时重量、店铺或隐私规则对应的面单类型已变化")

    @classmethod
    def _preflight(
        cls,
        readback: dict[str, Any],
        step: str,
        plan: Optional[dict[str, Any]] = None,
    ) -> None:
        if plan and plan.get("skip_external_orders"):
            external = readback.get("external_system_order")
            if type(external) is not bool:
                raise BackendSchemaError("后台缺少外部系统订单标签识别结果，请更新后台")
            if external:
                raise ExternalSystemOrderSkipped("标签含外部系统订单，已按本轮设置跳过")
        if not readback.get("found"):
            raise PermanentJobError("后台未找到待处理订单")
        cls._require_waitconfirm_status(readback)
        if not readback.get("action_history_complete"):
            raise PermanentJobError("操作历史未完整覆盖")
        if readback.get("has_ship_action"):
            raise PermanentJobError("订单已有预发货/发货动作")
        if readback.get("has_print_request"):
            raise PermanentJobError(
                "订单已有请求打印快递单动作；"
                "该证据不代表已打印，但为防止重复请求禁止再点击"
            )
        if readback.get("has_print_action"):
            raise PermanentJobError("订单已有打印动作，禁止重复打印")
        if readback.get("is_print_express"):
            raise PermanentJobError("聚水潭已标记打印但操作日志未确认，需人工核查，禁止重打")
        if readback.get("delivery_hold_marked") is True:
            raise PermanentJobError("订单备注、标签或异常说明含停发标记，禁止取号或打印")
        if readback.get("delivery_hold_reasons") != []:
            raise PermanentJobError("订单存在停发诊断原因，禁止取号或打印")
        if plan is not None:
            cls._validate_live_route(plan, readback)
        expected_current_id = str(
            (plan or {}).get("current_carrier_id", SOURCE_CARRIER_ID)
        )
        expected_current_name = str(
            (plan or {}).get("current_carrier", SOURCE_CARRIER_NAME)
        )
        if PRINT_PROFILE_CARRIERS.get(expected_current_id) != expected_current_name:
            raise PermanentJobError("计划当前快递不在允许范围")
        if step.startswith("RESET_CARRIER"):
            target_id, _ = reset_target(step)
            privacy_required = bool(readback.get("privacy_required"))
            if privacy_required and target_id != PRIVACY_CARRIER_ID:
                raise PermanentJobError("备注/多标签要求隐私面单，但计划目标不是隐私快递")
            if not privacy_required and target_id == PRIVACY_CARRIER_ID:
                raise PermanentJobError("备注/多标签未命中隐私规则，禁止切换隐私快递")
            if readback.get("carrier_id") != expected_current_id:
                raise PermanentJobError("重设前快递已变化")
            if readback.get("carrier_name") != expected_current_name:
                raise PermanentJobError("重设前快递名称已变化")
            if readback.get("has_waybill"):
                raise PermanentJobError("重设前已存在运单号")
            if readback.get("has_manual_carrier_action"):
                raise PermanentJobError("已有人工指定快递记录")
        elif step == "GET_WAYBILL":
            if (
                readback.get("privacy_required")
                and expected_current_id != PRIVACY_CARRIER_ID
            ):
                raise PermanentJobError("备注/多标签要求隐私面单，当前快递不是隐私快递")
            if readback.get("has_waybill"):
                raise PermanentJobError("获取前已存在运单号")
            if (
                readback.get("carrier_id") != expected_current_id
                or readback.get("carrier_name") != expected_current_name
            ):
                raise PermanentJobError("取号前实时快递与计划当前快递不一致")
        elif step == "PRINT_EXPRESS" and not readback.get("has_waybill"):
            raise PermanentJobError("打印前没有运单号")

    @staticmethod
    def _planned_privacy(plan: dict[str, Any]) -> bool:
        if type(plan.get("privacy_required")) is bool:
            return bool(plan["privacy_required"])
        privacy_step = (
            f"RESET_CARRIER_AND_GET_WAYBILL:{PRIVACY_CARRIER_ID}:{PRIVACY_CARRIER_NAME}"
        )
        return privacy_step in (plan.get("steps") or [])

    @classmethod
    def _validate_final_print_readback(
        cls,
        plan: dict[str, Any],
        readback: dict[str, Any],
        o_id: str,
        io_id: str,
        expected_waybill_suffix: Optional[str] = None,
        expected_waybill_fingerprint: Optional[str] = None,
    ) -> None:
        cls._require_job_identity(readback, o_id, io_id)
        cls._preflight(readback, "PRINT_EXPRESS", plan)
        planned_privacy = cls._planned_privacy(plan)
        if readback.get("privacy_source") != "remark":
            raise PermanentJobError("打印前隐私规则来源不是订单备注")
        if readback.get("privacy_required") is not planned_privacy:
            raise PermanentJobError("打印前订单备注隐私规则已变化")
        reset_steps = [
            str(step)
            for step in (plan.get("steps") or [])
            if str(step).startswith("RESET_CARRIER_AND_GET_WAYBILL:")
        ]
        if len(reset_steps) > 1:
            raise PermanentJobError("计划包含多个目标快递，禁止打印")
        if reset_steps:
            expected_id, expected_name = reset_target(reset_steps[0])
        else:
            expected_id = str(plan.get("current_carrier_id", ""))
            expected_name = str(plan.get("current_carrier", ""))
        if planned_privacy and expected_id != PRIVACY_CARRIER_ID:
            raise PermanentJobError("隐私订单计划目标不是隐私快递")
        if reset_steps and not planned_privacy and expected_id == PRIVACY_CARRIER_ID:
            raise PermanentJobError("普通订单计划错误指向隐私快递")
        if (
            readback.get("carrier_id") != expected_id
            or readback.get("carrier_name") != expected_name
        ):
            raise PermanentJobError("打印前实时快递与计划目标不一致")
        if expected_waybill_suffix is not None:
            if (
                not isinstance(expected_waybill_suffix, str)
                or not expected_waybill_suffix
                or readback.get("waybill_suffix") != expected_waybill_suffix
            ):
                raise PermanentJobError("打印前运单身份已变化，禁止打印错误面单")
        if expected_waybill_fingerprint is not None:
            if (
                re.fullmatch(r"[0-9a-f]{64}", expected_waybill_fingerprint)
                is None
                or readback.get("waybill_fingerprint")
                != expected_waybill_fingerprint
            ):
                raise PermanentJobError("打印前完整运单指纹已变化，禁止打印错误面单")

    @staticmethod
    def _item_fingerprint(items: Any) -> list[tuple[str, str, str, float, str]]:
        if not isinstance(items, list) or not items:
            raise PermanentJobError("SKU 明细不是数组，禁止完成")
        result: list[tuple[str, str, str, float, str]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise PermanentJobError("SKU 明细格式错误，禁止完成")
            try:
                line_key = item["line_key"]
                sku_id = item.get("sku_id", "")
                sku_name = item["sku_name"]
                qty_value = item["qty"]
                unit = item.get("unit", "")
            except (KeyError, TypeError, ValueError) as exc:
                raise PermanentJobError("SKU 明细身份或数量无效，禁止完成") from exc
            if (
                not isinstance(line_key, str)
                or not line_key
                or line_key in seen
                or not isinstance(sku_id, str)
                or not isinstance(sku_name, str)
                or not sku_name
                or not isinstance(unit, str)
                or isinstance(qty_value, bool)
                or not isinstance(qty_value, (int, float))
                or not math.isfinite(float(qty_value))
                or float(qty_value) <= 0
            ):
                raise PermanentJobError("SKU 明细身份、名称或数量无效，禁止完成")
            seen.add(line_key)
            result.append((line_key, sku_id, sku_name, float(qty_value), unit))
        return sorted(result)

    @classmethod
    def _validate_items_unchanged(
        cls, plan: dict[str, Any], readback: dict[str, Any]
    ) -> None:
        if plan.get("items_complete") is not True:
            raise PermanentJobError("原始计划未确认 SKU 明细完整，禁止完成")
        if readback.get("items_complete") is not True:
            raise PermanentJobError("当前回读未确认 SKU 明细完整，禁止完成")
        if readback.get("item_validation_errors") != []:
            raise PermanentJobError("当前回读存在 SKU 明细校验错误，禁止完成")
        if readback.get("order_validation_errors") != []:
            raise PermanentJobError("当前回读存在订单校验错误，禁止完成")
        planned = cls._item_fingerprint(plan.get("items"))
        current = cls._item_fingerprint(readback.get("items"))
        source_count = plan.get("source_item_count")
        current_source_count = readback.get("source_item_count")
        if (
            isinstance(source_count, bool)
            or not isinstance(source_count, int)
            or source_count != len(planned)
            or isinstance(current_source_count, bool)
            or not isinstance(current_source_count, int)
            or current_source_count != len(current)
            or current_source_count != source_count
            or planned != current
        ):
            raise PermanentJobError("打印后 SKU 明细身份或数量发生变化，禁止完成和部分落库")

    def _complete_printed_job(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        readback: dict[str, Any],
        completed_step_index: int,
    ) -> None:
        """Complete directly from the authoritative post-print readback."""

        plan = job["plan"]
        o_id, io_id, _claim_token = self._claim(plan)
        self._require_job_identity(readback, o_id, io_id)
        self._require_waitconfirm_status(readback)
        if not readback.get("action_history_complete"):
            raise PermanentJobError("打印完成后的操作历史未完整覆盖，禁止漏记 SKU")
        if not readback.get("has_print_action"):
            raise PermanentJobError("未回读到打印动作，暂不计入 SKU 出库统计")
        if not self.store.has_event("PRINT_OK", o_id, io_id):
            self._skip_already_printed_job(planner, job, readback)
            return
        self._validate_items_unchanged(plan, readback)
        inserted = self.store.record_sku_outbound(readback)
        self._complete_claim(planner, plan, "PRINTED")
        self.store.update_job(
            o_id,
            io_id,
            step_index=completed_step_index,
            status="COMPLETED",
        )
        self._event(
            "INFO",
            "COMPLETED",
            f"打单流程完成，已记录 {inserted} 条新 SKU 明细并停在预发货之前",
            o_id=o_id,
            io_id=io_id,
        )

    def _recover_locally_committed_print(
        self,
        planner: PlannerClient,
        job: dict[str, Any],
        readback: dict[str, Any],
    ) -> bool:
        """Recover a lost print callback without treating our print as external.

        RUNNING plus a durable PRINT_COMMIT proves this process crossed its
        guarded submission boundary. The backend still has to prove the exact
        order printed, and all route, waybill and SKU evidence must remain
        consistent. A print with no local commit remains an external print and
        is deliberately excluded from this application's SKU statistics.
        """

        plan = job["plan"]
        index = int(job["step_index"])
        o_id, io_id, _claim_token = self._claim(plan)
        if not self.store.has_event("PRINT_COMMIT", o_id, io_id):
            return False
        try:
            self._require_job_identity(readback, o_id, io_id)
            self._require_waitconfirm_status(readback)
            if readback.get("action_history_complete") is not True:
                raise PermanentJobError("操作历史尚未完整覆盖打印动作")
            if readback.get("has_print_action") is not True:
                return False
            if readback.get("has_ship_action") is True:
                raise PermanentJobError("打印恢复时已出现预发货/发货动作")
            if readback.get("has_waybill") is not True:
                raise PermanentJobError("打印恢复时当前运单号已缺失")
            waybill_suffix = readback.get("waybill_suffix")
            waybill_fingerprint = readback.get("waybill_fingerprint")
            if not isinstance(waybill_suffix, str) or not waybill_suffix:
                raise PermanentJobError("打印恢复缺少运单后缀")
            if (
                not isinstance(waybill_fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", waybill_fingerprint) is None
            ):
                raise PermanentJobError("打印恢复缺少完整运单指纹")
            self._validate_live_route(plan, readback)
            planned_privacy = self._planned_privacy(plan)
            if readback.get("privacy_source") != "remark":
                raise PermanentJobError("打印恢复的隐私规则来源不是订单备注")
            if readback.get("privacy_required") is not planned_privacy:
                raise PermanentJobError("打印恢复时订单隐私规则已变化")
            reset_steps = [
                str(step)
                for step in (plan.get("steps") or [])
                if str(step).startswith("RESET_CARRIER_AND_GET_WAYBILL:")
            ]
            if len(reset_steps) > 1:
                raise PermanentJobError("打印恢复计划包含多个目标快递")
            if reset_steps:
                expected_id, expected_name = reset_target(reset_steps[0])
            else:
                expected_id = str(plan.get("current_carrier_id", ""))
                expected_name = str(plan.get("current_carrier", ""))
            if (
                readback.get("carrier_id") != expected_id
                or readback.get("carrier_name") != expected_name
            ):
                raise PermanentJobError("打印恢复时快递与提交计划不一致")
            detail_reader = getattr(self.store, "latest_event_detail", None)
            commit_detail = (
                detail_reader("PRINT_COMMIT", o_id, io_id)
                if callable(detail_reader)
                else None
            )
            if isinstance(commit_detail, dict) and commit_detail.get(
                "proof_version"
            ) == 1:
                if commit_detail.get("waybill_suffix") != waybill_suffix:
                    raise PermanentJobError("打印恢复时运单后缀与本地提交凭据不一致")
                if commit_detail.get("waybill_fingerprint") != waybill_fingerprint:
                    raise PermanentJobError("打印恢复时完整运单指纹与本地提交凭据不一致")
            self._validate_items_unchanged(plan, readback)
        except PermanentJobError as exc:
            raise CompletionProofError(
                f"本软件打印提交后的恢复证据不完整：{exc}"
            ) from exc

        self._event(
            "WARN",
            "PRINT_OK",
            "打印组件回调丢失，但本地 PRINT_COMMIT 与后台打印动作已严格核对；"
            "已恢复本软件打印确认",
            o_id=o_id,
            io_id=io_id,
            detail={"recovered_from_commit": True},
        )
        steps = plan.get("steps") or []
        completed_step_index = index + 1
        if (
            index + 1 < len(steps)
            and str(steps[index + 1]) == "STOP_BEFORE_PRESHIP"
        ):
            completed_step_index = index + 2
        self._complete_printed_job(
            planner,
            job,
            readback,
            completed_step_index,
        )
        return True

    def _process_job(self, planner: PlannerClient, job: dict[str, Any]) -> None:
        plan = job["plan"]
        settings = self._settings()
        job["plan"]["skip_external_orders"] = settings.skip_external_orders
        profile_matches = plan_print_profile(plan) == settings.print_profile
        steps = plan.get("steps") or []
        index = int(job["step_index"])
        o_id, io_id, claim_token = self._claim(plan)
        if plan.get("outbound_identity_unique") is not True:
            raise PermanentJobError("任务未携带后台出库身份唯一性凭据，禁止操作页面")
        if index >= len(steps):
            raise PermanentJobError("任务步骤已越界但尚未完成 claimed V1 租约")
        step = str(steps[index])
        self._renew_step(planner, plan)

        if str(job.get("status", "")) == "RUNNING":
            first = self._inspect(planner, plan)
            if first.get("found"):
                self._require_job_identity(first, o_id, io_id)
                if self._is_allowlisted_terminal_status(first):
                    self._skip_terminal_status_job(planner, job, first)
                    return
                if self._settle_confirmed_status_job(planner, job, first):
                    return
                if first.get("has_ship_action"):
                    self._skip_shipped_job(planner, job, first)
                    return
            if step == "PRINT_EXPRESS":
                self._recover_running_print(planner, job, first)
                return
            if step == "GET_WAYBILL" or step.startswith("RESET_CARRIER"):
                self._recover_running_write(planner, job, first, step)
                return
            raise PermanentJobError(f"不允许恢复处于 RUNNING 的步骤：{step}")

        if not profile_matches:
            raise SafetyStop(
                "本地活动任务与当前人工选择的面单类型不一致；"
                "任务尚未提交，应释放后等待正确面单类型重新领取"
            )

        if step == "STOP_BEFORE_PRESHIP":
            readback = self._inspect(planner, plan)
            self._require_job_identity(readback, o_id, io_id)
            if self._is_allowlisted_terminal_status(readback):
                self._skip_terminal_status_job(planner, job, readback)
                return
            if self._settle_confirmed_status_job(planner, job, readback):
                return
            if readback.get("has_ship_action"):
                self._skip_shipped_job(planner, job, readback)
                return
            self._complete_printed_job(planner, job, readback, index + 1)
            return

        before = self._inspect(planner, plan)
        self._require_job_identity(before, o_id, io_id)
        if self._is_allowlisted_terminal_status(before):
            self._skip_terminal_status_job(planner, job, before)
            return
        if self._settle_confirmed_status_job(planner, job, before):
            return
        if before.get("has_ship_action"):
            self._skip_shipped_job(planner, job, before)
            return
        self._require_waitconfirm_status(before)

        # V0.5.1 could produce PRINT-only plans from an old "获取电子面单"
        # action even when the current outbound order l_id was empty. Repair an
        # affected claimed task only after a fresh exact-pair readback proves
        # that it is still on the exact planned carrier with no current waybill. This
        # keeps the existing lease/identity but makes GET_WAYBILL the next step.
        if (
            step == "PRINT_EXPRESS"
            and tuple(steps) == ("PRINT_EXPRESS", "STOP_BEFORE_PRESHIP")
            and plan.get("has_waybill") is True
            and before.get("has_waybill") is False
            and before.get("privacy_required")
            is bool(plan.get("privacy_required"))
            and before.get("carrier_id") == plan.get("current_carrier_id")
            and before.get("carrier_name") == plan.get("current_carrier")
            and not self.store.has_event("PRINT_OK", o_id, io_id)
        ):
            repaired = dict(plan)
            repaired["has_waybill"] = False
            repaired["steps"] = [
                "GET_WAYBILL",
                "PRINT_EXPRESS",
                "STOP_BEFORE_PRESHIP",
            ]
            if not self.store.replace_active_job_plan(o_id, io_id, repaired):
                raise SafetyStop("旧取号状态计划纠正失败，禁止继续")
            self._event(
                "WARN",
                "REPAIRED_STALE_WAYBILL_PLAN",
                "当前运单号为空，已把旧任务纠正为先获取电子面单号，再打印快递单",
                o_id=o_id,
                io_id=io_id,
            )
            return

        app_print_confirmed = self.store.has_event("PRINT_OK", o_id, io_id)
        if before.get("has_print_action") and not app_print_confirmed:
            self._skip_already_printed_job(planner, job, before)
            return
        if app_print_confirmed and step != "PRINT_EXPRESS":
            raise PermanentJobError("本地已有打印确认，但任务步骤仍在打印之前，禁止继续")
        already_done = False
        if step.startswith("RESET_CARRIER"):
            target_carrier_id, target_carrier_name = reset_target(step)
            already_done = (
                before.get("carrier_id") == target_carrier_id
                and before.get("carrier_name") == target_carrier_name
                and before.get("privacy_required")
                is (target_carrier_id == PRIVACY_CARRIER_ID)
                and before.get("has_waybill") is True
            )
        elif step == "GET_WAYBILL":
            already_done = (
                before.get("has_waybill") is True
                and before.get("carrier_id") == plan.get("current_carrier_id")
                and before.get("carrier_name") == plan.get("current_carrier")
                and before.get("privacy_required")
                is bool(plan.get("privacy_required"))
            )
        elif step == "PRINT_EXPRESS":
            # PRINT_OK is written only after this assistant successfully read
            # back the print action for this exact o_id/io_id pair. A later
            # stale remote snapshot must never make the assistant click again.
            already_done = app_print_confirmed
        if already_done:
            self.store.update_job(
                o_id, io_id, step_index=index + 1, status="PENDING"
            )
            self._event(
                "WARN",
                "RECOVERED_COMPLETED_STEP",
                f"恢复时确认步骤已完成，未重复点击：{step}",
                o_id=o_id,
                io_id=io_id,
            )
            return

        if step in {"GET_WAYBILL"} or step.startswith("RESET_CARRIER"):
            if not settings.allow_write:
                self.store.update_job(
                    o_id,
                    io_id,
                    step_index=index,
                    status="PAUSED",
                    pause_kind="POLICY_NO_WRITE",
                    pause_reason="当前运行策略禁止改快递/取号",
                )
                self._auto_pause("当前运行策略禁止改快递/取号", plan)
                return
        if step == "PRINT_EXPRESS" and not settings.allow_print:
            self.store.update_job(
                o_id,
                io_id,
                step_index=index,
                status="PAUSED",
                pause_kind="NO_PRINT_BOUNDARY",
                pause_reason="试运行停在打印前",
            )
            self._auto_pause(
                "试运行已完成取号并停在打印前；验收后请用正式模式继续", plan
            )
            return

        self._preflight(before, step, plan)
        expected_print_waybill_suffix: Optional[str] = None
        expected_print_waybill_fingerprint: Optional[str] = None
        if step == "PRINT_EXPRESS":
            suffix = before.get("waybill_suffix")
            fingerprint = before.get("waybill_fingerprint")
            if not isinstance(suffix, str) or not suffix:
                raise PermanentJobError("打印前缺少可核对的运单身份后缀")
            if (
                not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            ):
                raise PermanentJobError("打印前缺少可核对的完整运单指纹")
            expected_print_waybill_suffix = suffix
            expected_print_waybill_fingerprint = fingerprint
        if step == "PRINT_EXPRESS" and not print_service_online():
            self.store.update_job(
                o_id,
                io_id,
                step_index=index,
                status="PAUSED",
                pause_kind="PRINT_SERVICE_OFFLINE",
                pause_reason="本机打印组件未在线",
            )
            self._auto_pause("本机打印组件 54323/54325 未在线", plan)
            return

        self._require_operator_permission()
        self.store.update_job(o_id, io_id, step_index=index, status="PREPARING")
        self._event(
            "INFO",
            "STEP_VERIFY_START",
            f"正在查找并核对当前订单；下一步：{describe_plan_step(step)}",
            o_id=o_id,
            io_id=io_id,
        )
        browser = self._browser_for(settings)
        try:
            def renew_before_non_effect_button() -> None:
                self._renew_step(planner, plan)

            def verify_immediately_before_effect(effect_step: str) -> dict[str, Any]:
                self._require_operator_permission()
                self._renew_step(planner, plan)
                latest = self._inspect(planner, plan)
                self._require_job_identity(latest, o_id, io_id)
                if self._is_allowlisted_terminal_status(latest):
                    raise ExternalTerminalStatusDetected(latest)
                if latest.get("has_ship_action"):
                    raise ExternalShipmentDetected(latest)
                if latest.get("has_print_action"):
                    raise ExternalPrintDetected(latest)
                self._validate_items_unchanged(plan, latest)
                if effect_step == "PRINT_EXPRESS":
                    self._validate_final_print_readback(
                        plan,
                        latest,
                        o_id,
                        io_id,
                    )
                    if (
                        latest.get("waybill_suffix")
                        != expected_print_waybill_suffix
                        or latest.get("waybill_fingerprint")
                        != expected_print_waybill_fingerprint
                    ):
                        raise PermanentJobError(
                            "打印 API 提交前运单身份已变化，禁止打印错误面单"
                        )
                    if not print_service_online():
                        raise SafetyStop("打印 API 提交前复检发现本机打印组件已离线")
                else:
                    self._preflight(latest, effect_step, plan)
                # Inspect may consume most of a short lease. Renew once more
                # after every final validation, still inside action_lock, so a
                # near-expiry claim can never reach RUNNING/click.
                self._renew_step(planner, plan)
                self._require_operator_permission()
                return latest

            def mark_effect_running() -> None:
                if not self.store.update_job(
                    o_id, io_id, step_index=index, status="RUNNING"
                ):
                    raise SafetyStop("任务状态已被其他进程改变，禁止提交原生接口")
                if step.startswith("RESET_CARRIER"):
                    event_type = "CARRIER_CHANGE_COMMIT"
                    message = "接口订单与快递已唯一核验，正在提交改快递并取号"
                elif step == "GET_WAYBILL":
                    event_type = "WAYBILL_COMMIT"
                    message = "接口订单已唯一核验，正在提交获取电子面单"
                elif step == "PRINT_EXPRESS":
                    event_type = "PRINT_COMMIT"
                    message = "接口订单、快递及打印组件已复核，正在提交打印任务"
                else:
                    raise PermanentJobError(f"不认识的提交步骤：{step}")
                detail = None
                if step == "PRINT_EXPRESS":
                    detail = {
                        "proof_version": 1,
                        "waybill_suffix": expected_print_waybill_suffix,
                        "waybill_fingerprint": expected_print_waybill_fingerprint,
                    }
                self._event(
                    "INFO",
                    event_type,
                    message,
                    o_id=o_id,
                    io_id=io_id,
                    detail=detail,
                )

            if step.startswith("RESET_CARRIER"):
                target_carrier_id, target_carrier_name = reset_target(step)
                browser.reset_carrier(
                    o_id,
                    io_id,
                    target_carrier_id,
                    target_carrier_name,
                    outbound_identity_unique=True,
                    before_open=renew_before_non_effect_button,
                    before_confirm=lambda: verify_immediately_before_effect(step),
                    mark_running=mark_effect_running,
                    action_lock=self.action_lock,
                    final_guard=self._require_operator_permission,
                )
                self._clear_dom_recovery((o_id, io_id))
                after = self._poll_readback(
                    planner,
                    o_id,
                    io_id,
                    claim_token,
                    lambda data: data.get("carrier_id") == target_carrier_id
                    and data.get("carrier_name") == target_carrier_name
                    and data.get("privacy_required")
                    is (target_carrier_id == PRIVACY_CARRIER_ID)
                    and data.get("has_waybill") is True,
                    "重设快递并自动取号",
                )
                self._event(
                    "INFO",
                    "PRIVACY_CARRIER_WAYBILL_OK"
                    if target_carrier_id == PRIVACY_CARRIER_ID
                    else "CARRIER_RESET_WAYBILL_OK",
                    f"已改为{target_carrier_name}并取号，运单后四位 {after.get('waybill_suffix', '')}",
                    o_id=o_id,
                    io_id=io_id,
                )
            elif step == "GET_WAYBILL":
                browser.get_waybill(
                    o_id,
                    io_id,
                    outbound_identity_unique=True,
                    before_click=lambda: verify_immediately_before_effect(step),
                    mark_running=mark_effect_running,
                    action_lock=self.action_lock,
                    final_guard=self._require_operator_permission,
                )
                self._clear_dom_recovery((o_id, io_id))
                after = self._poll_readback(
                    planner,
                    o_id,
                    io_id,
                    claim_token,
                    lambda data: data.get("has_waybill") is True
                    and data.get("carrier_id") == plan.get("current_carrier_id")
                    and data.get("carrier_name") == plan.get("current_carrier")
                    and data.get("privacy_required")
                    is bool(plan.get("privacy_required")),
                    "获取电子面单",
                )
                self._event(
                    "INFO",
                    "WAYBILL_OK",
                    f"已取号，运单后四位 {after.get('waybill_suffix', '')}",
                    o_id=o_id,
                    io_id=io_id,
                )
            elif step == "PRINT_EXPRESS":
                def verify_immediately_before_print() -> None:
                    verify_immediately_before_effect("PRINT_EXPRESS")

                browser.print_express(
                    o_id,
                    io_id,
                    outbound_identity_unique=True,
                    before_click=verify_immediately_before_print,
                    mark_running=mark_effect_running,
                    action_lock=self.action_lock,
                    final_guard=self._require_operator_permission,
                )
                self._clear_dom_recovery((o_id, io_id))
                after = self._poll_readback(
                    planner,
                    o_id,
                    io_id,
                    claim_token,
                    lambda data: bool(data.get("has_print_action"))
                    and data.get("waybill_suffix")
                    == expected_print_waybill_suffix
                    and data.get("waybill_fingerprint")
                    == expected_print_waybill_fingerprint,
                    "打印动作",
                    attempts=12,
                )
                self._event(
                    "INFO", "PRINT_OK", "打印动作回读成功", o_id=o_id, io_id=io_id
                )
                if (
                    index + 1 < len(steps)
                    and str(steps[index + 1]) == "STOP_BEFORE_PRESHIP"
                ):
                    # The poll above is already a fresh exact-pair backend
                    # inspect performed after printing. Use that same proof to
                    # finish before pre-ship, instead of starting a third
                    # worker round solely to renew and inspect identical data.
                    self._complete_printed_job(planner, job, after, index + 2)
                    return
            else:
                raise PermanentJobError(f"不认识的流程步骤：{step}")
        except ExternalShipmentDetected as exc:
            self._skip_shipped_job(planner, job, exc.readback)
            return
        except ExternalTerminalStatusDetected as exc:
            self._skip_terminal_status_job(planner, job, exc.readback)
            return
        except ExternalPrintDetected as exc:
            self._skip_already_printed_job(planner, job, exc.readback)
            return
        self.store.update_job(o_id, io_id, step_index=index + 1, status="PENDING")

    def _process_waybill_batch(
        self, planner: PlannerClient, jobs: list[dict[str, Any]]
    ) -> None:
        """Take numbers for one exact 2-10 order action with a single click."""

        if not 2 <= len(jobs) <= BATCH_PRINT_SIZE:
            raise SafetyStop("批量取号任务数量超出安全范围")
        settings = self._settings()
        for job in jobs:
            job["plan"]["skip_external_orders"] = settings.skip_external_orders
        if not settings.allow_write:
            self._process_job(planner, jobs[0])
            return
        step = self._job_step(jobs[0])
        if step != "GET_WAYBILL" and not step.startswith("RESET_CARRIER"):
            raise SafetyStop("批量取号包含非取号步骤，禁止继续")
        for job in jobs[1:]:
            with self._job_exception_scope(job):
                if self._job_step(job) != step:
                    raise SafetyStop("批量取号混入不同快递动作，禁止继续")
        target: Optional[tuple[str, str]] = None
        if step.startswith("RESET_CARRIER"):
            target = reset_target(step)

        identities: list[tuple[str, str]] = []
        for job in jobs:
            with self._job_exception_scope(job):
                plan = job["plan"]
                if plan_print_profile(plan) != settings.print_profile:
                    raise SafetyStop("批量取号中混入其他面单类型，禁止继续")
                index = int(job["step_index"])
                o_id, io_id, _token = self._claim(plan)
                if (
                    str(job.get("status")) != "PENDING"
                    or index >= len(plan.get("steps") or [])
                    or plan.get("outbound_identity_unique") is not True
                ):
                    raise SafetyStop("批量中存在未就绪或身份不唯一的取号任务")
                identities.append((o_id, io_id))

        latest_readbacks = self._batch_preflight_readbacks(planner, jobs)
        for job, latest in zip(jobs, latest_readbacks):
            with self._job_exception_scope(job):
                plan = job["plan"]
                o_id, io_id, _token = self._claim(plan)
                self._require_job_identity(latest, o_id, io_id)
                if (
                    self._is_allowlisted_terminal_status(latest)
                    or latest.get("has_ship_action")
                    or latest.get("has_print_action")
                ):
                    self._process_job(planner, job)
                    return
                self._require_waitconfirm_status(latest)
                self._validate_items_unchanged(plan, latest)
                already_done = False
                if target is not None:
                    already_done = (
                        latest.get("carrier_id") == target[0]
                        and latest.get("carrier_name") == target[1]
                        and latest.get("has_waybill") is True
                    )
                else:
                    already_done = (
                        latest.get("carrier_id") == plan.get("current_carrier_id")
                        and latest.get("carrier_name") == plan.get("current_carrier")
                        and latest.get("has_waybill") is True
                    )
                if already_done:
                    self._process_job(planner, job)
                    return
                self._preflight(latest, step, plan)

        for job in jobs:
            with self._job_exception_scope(job):
                if not self.store.update_job(
                    str(job["o_id"]),
                    str(job["io_id"]),
                    step_index=int(job["step_index"]),
                    status="PREPARING",
                ):
                    raise SafetyStop("批量取号任务无法进入准备状态")
        self._event(
            "INFO",
            "BATCH_WAYBILL_VERIFY_START",
            f"正在通过原生接口核对 {len(jobs)} 笔“"
            f"{PRINT_PROFILE_LABELS[settings.print_profile]}”订单；"
            "核对通过后只提交一次取号 API",
        )
        browser = self._browser_for(settings)
        committed = False

        def validate_local_boundary_before_click() -> None:
            self._require_operator_permission()
            final_readbacks = self._batch_preflight_readbacks(planner, jobs)
            for job, latest in zip(jobs, final_readbacks):
                try:
                    with self._job_exception_scope(job):
                        plan = job["plan"]
                        o_id, io_id, _token = self._claim(plan)
                        self._require_job_identity(latest, o_id, io_id)
                        if self._is_allowlisted_terminal_status(latest):
                            raise PermanentJobError(
                                f"取号 API 提交前订单状态已变为 {latest.get('status')}"
                            )
                        if latest.get("has_ship_action"):
                            raise PermanentJobError("取号 API 提交前已出现发货动作")
                        if latest.get("has_print_action"):
                            raise PermanentJobError("取号 API 提交前已出现打印动作")
                        self._require_waitconfirm_status(latest)
                        self._validate_items_unchanged(plan, latest)
                        if target is not None:
                            already_done = (
                                latest.get("carrier_id") == target[0]
                                and latest.get("carrier_name") == target[1]
                                and latest.get("has_waybill") is True
                            )
                        else:
                            already_done = (
                                latest.get("carrier_id")
                                == plan.get("current_carrier_id")
                                and latest.get("carrier_name")
                                == plan.get("current_carrier")
                                and latest.get("has_waybill") is True
                            )
                        if already_done:
                            raise PermanentJobError("取号 API 提交前已存在目标运单")
                        # Recompute warehouse, weight, shop, privacy and route
                        # from the last live readback before the real button.
                        self._preflight(latest, step, plan)
                except BatchCandidateChanged:
                    raise
                except SafetyStop as exc:
                    raise BatchCandidateChanged(
                        job, f"批量取号候选在 API 提交前发生变化：{exc}"
                    ) from exc
            self._require_operator_permission()

        def mark_all_running() -> None:
            nonlocal committed
            if not self.store.mark_batch_running(jobs):
                raise SafetyStop("批量取号任务状态已变化，禁止提交 API")
            committed = True
            for o_id, io_id in identities:
                self._event(
                    "INFO",
                    "BATCH_WAYBILL_COMMIT",
                    f"正在提交 {len(jobs)} 笔“"
                    f"{PRINT_PROFILE_LABELS[settings.print_profile]}”批量取号",
                    o_id=o_id,
                    io_id=io_id,
                )

        try:
            if target is None:
                browser.get_waybill_batch(
                    identities,
                    before_click=validate_local_boundary_before_click,
                    mark_running=mark_all_running,
                    action_lock=self.action_lock,
                    final_guard=self._require_operator_permission,
                )
            else:
                browser.reset_carrier_batch(
                    identities,
                    target[0],
                    target[1],
                    before_confirm=validate_local_boundary_before_click,
                    mark_running=mark_all_running,
                    action_lock=self.action_lock,
                    final_guard=self._require_operator_permission,
                )
            for identity in identities:
                self._clear_dom_recovery(identity)
        except (BatchSearchUnsupported, OrderRowNotReady) as exc:
            if committed:
                raise CommittedBatchUncertain(
                    jobs,
                    "批量取号已经进入提交状态但页面返回异常，禁止重复取号",
                ) from exc
            for pending in jobs:
                with self._job_exception_scope(pending):
                    self.store.update_job(
                        str(pending["o_id"]),
                        str(pending["io_id"]),
                        step_index=int(pending["step_index"]),
                        status="PENDING",
                    )
            self._event(
                "WARN",
                "BATCH_SEARCH_FALLBACK",
                f"{exc}；已安全回退为逐单取号，未跳过订单",
            )
            with self._job_exception_scope(jobs[0]):
                self._process_job(planner, jobs[0])
            return
        except BatchCandidateChanged as exc:
            if committed:
                raise CommittedBatchUncertain(
                    jobs,
                    "批量取号已经进入提交状态但候选订单变化，禁止重复取号",
                ) from exc
            with self._job_exception_scope(exc.job):
                changed_job = self._batch_job_by_identity(jobs, exc.job)
                for pending in jobs:
                    with self._job_exception_scope(pending):
                        self.store.update_job(
                            str(pending["o_id"]),
                            str(pending["io_id"]),
                            step_index=int(pending["step_index"]),
                            status="PENDING",
                        )
                self._event("WARN", "BATCH_REBUILD", str(exc))
                self._process_job(planner, changed_job)
            return
        except Exception as exc:
            if committed:
                raise CommittedBatchUncertain(
                    jobs,
                    "批量取号已经进入提交状态但页面或点击响应异常，禁止重复取号",
                ) from exc
            raise

        checks: list[
            tuple[dict[str, Any], Callable[[dict[str, Any]], bool]]
        ] = []
        for job in jobs:
            plan = job["plan"]
            if target is None:
                expected_id = str(plan.get("current_carrier_id", ""))
                expected_name = str(plan.get("current_carrier", ""))
                expected_privacy = bool(plan.get("privacy_required"))
                predicate = lambda data, eid=expected_id, ename=expected_name, eprivacy=expected_privacy: (
                    data.get("has_waybill") is True
                    and data.get("carrier_id") == eid
                    and data.get("carrier_name") == ename
                    and data.get("privacy_required") is eprivacy
                )
            else:
                predicate = lambda data, expected=target: (
                    data.get("carrier_id") == expected[0]
                    and data.get("carrier_name") == expected[1]
                    and data.get("privacy_required")
                    is (expected[0] == PRIVACY_CARRIER_ID)
                    and data.get("has_waybill") is True
                )
            checks.append((job, predicate))
        action_name = (
            "批量获取电子面单"
            if target is None
            else "批量重设快递并自动取号"
        )
        event_type = (
            "WAYBILL_OK"
            if target is None
            else (
                "PRIVACY_CARRIER_WAYBILL_OK"
                if target[0] == PRIVACY_CARRIER_ID
                else "CARRIER_RESET_WAYBILL_OK"
            )
        )
        def settle_waybill(job: dict[str, Any], after: dict[str, Any]) -> None:
            plan = job["plan"]
            index = int(job["step_index"])
            o_id, io_id, _claim_token = self._claim(plan)
            if self._is_allowlisted_terminal_status(after):
                self._skip_terminal_status_job(planner, job, after)
                return
            if after.get("has_ship_action"):
                self._skip_shipped_job(planner, job, after)
                return
            self._event(
                "INFO",
                event_type,
                f"批量取号已逐单回读确认，运单后四位 {after.get('waybill_suffix', '')}",
                o_id=o_id,
                io_id=io_id,
            )
            self.store.update_job(
                o_id, io_id, step_index=index + 1, status="PENDING"
            )

        _resolved, readback_error = self._poll_batch_readbacks(
            planner, checks, action_name, on_resolved=settle_waybill
        )
        if readback_error is not None:
            raise readback_error

    def _process_print_batch(
        self, planner: PlannerClient, jobs: list[dict[str, Any]]
    ) -> None:
        """Print one exact 2-10 order batch and settle each lease separately."""

        if not 2 <= len(jobs) <= BATCH_PRINT_SIZE:
            raise SafetyStop("批量打印任务数量超出安全范围")
        settings = self._settings()
        for job in jobs:
            job["plan"]["skip_external_orders"] = settings.skip_external_orders
        if not settings.allow_print or not print_service_online():
            self._process_job(planner, jobs[0])
            return

        identities: list[tuple[str, str]] = []
        for job in jobs:
            with self._job_exception_scope(job):
                plan = job["plan"]
                if plan_print_profile(plan) != settings.print_profile:
                    raise SafetyStop("批量打印中混入了其他面单类型，禁止打印")
                index = int(job["step_index"])
                steps = plan.get("steps") or []
                o_id, io_id, _token = self._claim(plan)
                if (
                    str(job.get("status")) != "PENDING"
                    or index >= len(steps)
                    or str(steps[index]) != "PRINT_EXPRESS"
                    or plan.get("outbound_identity_unique") is not True
                ):
                    raise SafetyStop("批量中存在未就绪或身份不唯一的打印任务")
                identities.append((o_id, io_id))

        latest_readbacks = self._batch_preflight_readbacks(planner, jobs)
        expected_waybill_suffixes: dict[tuple[str, str], str] = {}
        expected_waybill_fingerprints: dict[tuple[str, str], str] = {}
        for job, latest in zip(jobs, latest_readbacks):
            with self._job_exception_scope(job):
                plan = job["plan"]
                o_id, io_id, _token = self._claim(plan)
                self._require_job_identity(latest, o_id, io_id)
                if (
                    self._is_allowlisted_terminal_status(latest)
                    or latest.get("has_ship_action")
                    or latest.get("has_print_action")
                ):
                    self._process_job(planner, job)
                    return
                self._validate_items_unchanged(plan, latest)
                suffix = latest.get("waybill_suffix")
                fingerprint = latest.get("waybill_fingerprint")
                if not isinstance(suffix, str) or not suffix:
                    raise PermanentJobError("批量打印前缺少可核对的运单身份后缀")
                if (
                    not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                ):
                    raise PermanentJobError("批量打印前缺少可核对的完整运单指纹")
                expected_waybill_suffixes[(o_id, io_id)] = suffix
                expected_waybill_fingerprints[(o_id, io_id)] = fingerprint
                self._validate_final_print_readback(plan, latest, o_id, io_id)

        for job in jobs:
            with self._job_exception_scope(job):
                if not self.store.update_job(
                    str(job["o_id"]),
                    str(job["io_id"]),
                    step_index=int(job["step_index"]),
                    status="PREPARING",
                ):
                    raise SafetyStop("批量打印任务无法进入准备状态")
        self._event(
            "INFO",
            "BATCH_VERIFY_START",
            f"正在通过原生接口核对 {len(jobs)} 笔“"
            f"{PRINT_PROFILE_LABELS[settings.print_profile]}”订单；"
            "本次只会打印这一种面单",
        )
        browser = self._browser_for(settings)
        committed = False

        def validate_local_boundary_before_click() -> None:
            self._require_operator_permission()
            final_readbacks = self._batch_preflight_readbacks(planner, jobs)
            for job, latest in zip(jobs, final_readbacks):
                try:
                    with self._job_exception_scope(job):
                        plan = job["plan"]
                        o_id, io_id, _token = self._claim(plan)
                        self._require_job_identity(latest, o_id, io_id)
                        if self._is_allowlisted_terminal_status(latest):
                            raise PermanentJobError(
                                f"打印 API 提交前订单状态已变为 {latest.get('status')}"
                            )
                        if latest.get("has_ship_action"):
                            raise PermanentJobError("打印 API 提交前已出现发货动作")
                        if latest.get("has_print_action"):
                            raise PermanentJobError("打印 API 提交前已出现打印动作")
                        self._validate_items_unchanged(plan, latest)
                        # Includes exact identity, live route/preflight,
                        # privacy and final carrier/waybill verification.
                        self._validate_final_print_readback(
                            plan,
                            latest,
                            o_id,
                            io_id,
                        )
                        if (
                            latest.get("waybill_suffix")
                            != expected_waybill_suffixes[(o_id, io_id)]
                            or latest.get("waybill_fingerprint")
                            != expected_waybill_fingerprints[(o_id, io_id)]
                        ):
                            raise PermanentJobError(
                                "批量打印 API 提交前运单身份已变化"
                            )
                except BatchCandidateChanged:
                    raise
                except SafetyStop as exc:
                    raise BatchCandidateChanged(
                        job, f"批量打印候选在 API 提交前发生变化：{exc}"
                    ) from exc
            if not print_service_online():
                raise SafetyStop("批量打印 API 提交前复检发现打印组件已离线")
            self._require_operator_permission()

        def mark_all_running() -> None:
            nonlocal committed
            if not self.store.mark_batch_running(jobs):
                raise SafetyStop("批量任务状态已变化，禁止提交打印 API")
            committed = True
            for o_id, io_id in identities:
                self._event(
                    "INFO",
                    "PRINT_COMMIT",
                    f"正在提交 {len(jobs)} 笔“"
                    f"{PRINT_PROFILE_LABELS[settings.print_profile]}”批量打印",
                    o_id=o_id,
                    io_id=io_id,
                    detail={
                        "proof_version": 1,
                        "waybill_suffix": expected_waybill_suffixes[(o_id, io_id)],
                        "waybill_fingerprint": expected_waybill_fingerprints[
                            (o_id, io_id)
                        ],
                    },
                )

        try:
            browser.print_express_batch(
                identities,
                before_click=validate_local_boundary_before_click,
                mark_running=mark_all_running,
                action_lock=self.action_lock,
                final_guard=self._require_operator_permission,
            )
            for identity in identities:
                self._clear_dom_recovery(identity)
        except (BatchSearchUnsupported, OrderRowNotReady) as exc:
            if committed:
                raise CommittedBatchUncertain(
                    jobs,
                    "批量打印已经进入提交状态但页面返回异常，禁止重复打印",
                ) from exc
            # Some JST deployments do not accept comma-separated exact order
            # ids. Falling back to one fully verified print is safe and keeps
            # the queue moving instead of turning a batch capability mismatch
            # into a global DOM pause.
            for pending in jobs:
                with self._job_exception_scope(pending):
                    self.store.update_job(
                        str(pending["o_id"]),
                        str(pending["io_id"]),
                        step_index=int(pending["step_index"]),
                        status="PENDING",
                    )
            self._event(
                "WARN",
                "BATCH_SEARCH_FALLBACK",
                f"{exc}；已安全回退为逐单打印，未跳过订单",
            )
            with self._job_exception_scope(jobs[0]):
                self._process_job(planner, jobs[0])
            return
        except BatchCandidateChanged as exc:
            if committed:
                raise CommittedBatchUncertain(
                    jobs,
                    "批量打印已经进入提交状态但候选订单变化，禁止重复打印",
                ) from exc
            with self._job_exception_scope(exc.job):
                changed_job = self._batch_job_by_identity(jobs, exc.job)
                for pending in jobs:
                    with self._job_exception_scope(pending):
                        self.store.update_job(
                            str(pending["o_id"]),
                            str(pending["io_id"]),
                            step_index=int(pending["step_index"]),
                            status="PENDING",
                        )
                self._event("WARN", "BATCH_REBUILD", str(exc))
                self._process_job(planner, changed_job)
            return
        except Exception as exc:
            if committed:
                raise CommittedBatchUncertain(
                    jobs,
                    "批量打印已经进入提交状态但页面或点击响应异常，禁止重复打印",
                ) from exc
            raise

        def settle_print(job: dict[str, Any], after: dict[str, Any]) -> None:
            plan = job["plan"]
            steps = plan.get("steps") or []
            index = int(job["step_index"])
            o_id, io_id, _claim_token = self._claim(plan)
            if self._is_allowlisted_terminal_status(after):
                # One order may be deleted while the batch is being submitted.
                # Settle only that exact lease and continue reading back the
                # remaining orders instead of pausing the whole workstation.
                self._skip_terminal_status_job(planner, job, after)
                return
            if after.get("has_ship_action"):
                self._skip_shipped_job(planner, job, after)
                return
            if (
                after.get("waybill_suffix")
                != expected_waybill_suffixes[(o_id, io_id)]
                or after.get("waybill_fingerprint")
                != expected_waybill_fingerprints[(o_id, io_id)]
            ):
                raise PermanentJobError(
                    "批量打印回读的运单身份发生变化，禁止确认完成"
                )
            self._event(
                "INFO",
                "PRINT_OK",
                "批量打印动作已逐单回读确认",
                o_id=o_id,
                io_id=io_id,
            )
            if (
                index + 1 < len(steps)
                and str(steps[index + 1]) == "STOP_BEFORE_PRESHIP"
            ):
                self._complete_printed_job(planner, job, after, index + 2)
            else:
                self.store.update_job(
                    o_id, io_id, step_index=index + 1, status="PENDING"
                )

        _resolved, readback_error = self._poll_batch_readbacks(
            planner,
            [
                (
                    job,
                    lambda data, identity=(
                        str(job["o_id"]),
                        str(job["io_id"]),
                    ): bool(data.get("has_print_action"))
                    and data.get("waybill_suffix")
                    == expected_waybill_suffixes[identity]
                    and data.get("waybill_fingerprint")
                    == expected_waybill_fingerprints[identity],
                )
                for job in jobs
            ],
            "批量打印动作",
            attempts=12,
            on_resolved=settle_print,
        )
        if readback_error is not None:
            raise readback_error

    def _record_blocked(
        self, payload: dict[str, Any], print_profile: str
    ) -> bool:
        privacy_found = False
        newly_recorded = 0
        for item in payload.get("blocked_preview") or []:
            blockers = item.get("blockers") or []
            key = (
                f"{item.get('o_id')}|{item.get('io_id')}|"
                f"{'|'.join(str(reason) for reason in blockers)}"
            )
            if key in self.seen_blocks:
                continue
            self.seen_blocks.add(key)
            privacy = any("隐私" in str(reason) for reason in blockers)
            privacy_found = privacy_found or privacy
            stored = self.store.event(
                "BLOCKED",
                "PRIVACY_REVIEW" if privacy else "PLANNER_BLOCKED",
                "；".join(str(reason) for reason in blockers),
                o_id=str(item.get("o_id", "")),
                io_id=str(item.get("io_id", "")),
            )
            newly_recorded += 1
            # Privacy review is actionable and remains visible. Ordinary rule
            # exclusions are kept in SQLite/CSV but summarized in the main UI.
            if privacy:
                self.notify(stored)
        counts = payload.get("counts") or {}
        if isinstance(counts, dict):
            orders_read = counts.get("orders_read", 0)
            in_scope = counts.get("in_scope", 0)
            ready = counts.get("ready", 0)
            blocked = counts.get("blocked", 0)
            selected = counts.get("selected", 0)
            profile_ready = counts.get("profile_ready", 0)
            external_excluded = counts.get("external_order_excluded", 0)
            profile_excluded = counts.get("profile_excluded", 0)
            claim_unavailable = counts.get("claim_unavailable", 0)
            if all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (
                    orders_read,
                    in_scope,
                    ready,
                    blocked,
                    selected,
                    profile_ready,
                    profile_excluded,
                    external_excluded,
                    claim_unavailable,
                )
            ):
                signature = (
                    f"{print_profile}|{orders_read}|{in_scope}|{ready}|{blocked}|"
                    f"{profile_ready}|{selected}|{profile_excluded}|{claim_unavailable}|{external_excluded}"
                )
                if signature != self.last_filter_summary or newly_recorded:
                    self.last_filter_summary = signature
                    profile_label = PRINT_PROFILE_LABELS[print_profile]
                    reason_counts = payload.get("blocked_reason_counts") or {}
                    top_reasons: list[str] = []
                    if isinstance(reason_counts, dict):
                        ranked = sorted(
                            (
                                (str(reason), count)
                                for reason, count in reason_counts.items()
                                if isinstance(count, int)
                                and not isinstance(count, bool)
                                and count > 0
                            ),
                            key=lambda item: (-item[1], item[0]),
                        )[:3]
                        top_reasons = [
                            f"{reason}（{count}笔）" for reason, count in ranked
                        ]
                    reasons_text = (
                        f"主要未处理原因：{'；'.join(top_reasons)}。"
                        if top_reasons
                        else ""
                    )
                    self._event(
                        "INFO",
                        "FILTER_SUMMARY",
                        f"后台共扫描 {orders_read} 笔（页面可见订单不等于待打单）；"
                        f"当前选择“{profile_label}”：检查 {in_scope} 笔待审范围订单，"
                        f"其中 {profile_ready} 笔符合本类规则，本次领取 {selected} 笔；"
                        f"按设置跳过外部系统订单 {external_excluded} 笔；"
                        f"本机历史排除 {profile_excluded} 笔，"
                        f"领取阶段不可用 {claim_unavailable} 笔；"
                        f"全部类型共 {ready} 笔符合规则，{blocked} 笔因安全规则未处理。"
                        f"{reasons_text}详细原因可导出异常 CSV",
                    )
        return privacy_found

    def _announce_no_orders(
        self, settings: Settings, payload: dict[str, Any]
    ) -> None:
        profile = settings.print_profile
        profile_label = PRINT_PROFILE_LABELS[profile]
        raw_counts = payload.get("counts")
        counts = raw_counts if isinstance(raw_counts, dict) else {}

        def safe_count(field: str) -> int:
            value = counts.get(field, 0)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
            return 0

        orders_read = safe_count("orders_read")
        in_scope = safe_count("in_scope")
        ready = safe_count("ready")
        blocked = safe_count("blocked")
        profile_ready = safe_count("profile_ready")
        external_excluded = safe_count("external_order_excluded")
        profile_excluded = safe_count("profile_excluded")
        claim_unavailable = safe_count("claim_unavailable")
        if profile_ready > 0:
            category = "READY_BUT_UNCLAIMED"
            status_detail = f"符合 {profile_ready} 笔"
            if profile_excluded or claim_unavailable:
                status_detail += (
                    f"，本机排除 {profile_excluded} 笔，领取不可用 {claim_unavailable} 笔"
                )
                detail = (
                    f"后台本轮发现 {profile_ready} 笔符合本类规则，但未取得可处理租约；"
                    f"其中本机历史安全排除 {profile_excluded} 笔，"
                    f"领取阶段不可用 {claim_unavailable} 笔。"
                    "领取不可用通常表示其他工作站占用或后台已经记录完成。"
                )
            else:
                detail = (
                    f"后台本轮发现 {profile_ready} 笔符合本类规则，但未取得可处理租约；"
                    "可能被本机安全排除、其他工作站占用，或仍在等待后台状态刷新。"
                )
        elif external_excluded > 0:
            category = "EXTERNAL_ORDERS_FILTERED"
            status_detail = f"按设置跳过外部系统订单 {external_excluded} 笔"
            detail = (
                f"当前面单类型有 {external_excluded} 笔订单带外部系统订单标签，"
                "已按本轮设置排除，未领取这些订单。停止后可取消勾选并重新开始。"
            )
        elif ready > 0:
            category = "OTHER_PROFILE"
            status_detail = f"其他面单类型有 {ready} 笔符合规则"
            detail = (
                f"全部面单类型共有 {ready} 笔符合规则，但最终路由到“{profile_label}”"
                "的订单为 0 笔。页面显示的当前快递不等于重量、店铺和隐私规则决定的"
                "最终面单纸类型。"
            )
        elif in_scope > 0 or blocked > 0:
            category = "RULE_BLOCKED"
            status_detail = f"可领取 0 笔，安全阻断 {blocked}/{in_scope} 笔"
            detail = (
                f"后台本轮检查 {in_scope} 笔待审范围订单，"
                f"其中 {blocked} 笔因安全规则未处理，未形成可安全领取的本类候选。"
                "详细原因可导出异常 CSV 查看。"
            )
        else:
            category = "EMPTY_SCOPE"
            status_detail = f"扫描 {orders_read} 笔，待审范围 0 笔"
            raw_scope = payload.get("scope")
            scope = raw_scope if isinstance(raw_scope, dict) else {}
            lookback = scope.get("lookback_hours")
            lookback_text = (
                f"最近 {lookback} 小时有修改的"
                if isinstance(lookback, int)
                and not isinstance(lookback, bool)
                and lookback > 0
                else "本轮"
            )
            detail = (
                f"后台本轮共扫描 {orders_read} 笔；在{lookback_text}订单中，"
                "未扫描到属于当前自动化待审范围的订单。"
            )
        message = (
            f"本轮未领取到“{profile_label}”订单。{detail}"
            f"程序会每 {settings.loop_seconds} 秒继续检查；"
            "如需打印其他类型，请先点击停止，更换面单纸后再选择新的面单类型。"
        )
        self.set_status(
            f"本轮未领取到“{profile_label}”订单：{status_detail}；仍在定时检查"
        )
        notice_key = f"{profile}|{category}"
        if self.no_order_notice_key == notice_key:
            return
        self.no_order_notice_key = notice_key
        self._event("INFO", "NO_MATCHING_ORDERS", message)

    @staticmethod
    def _job_step(job: dict[str, Any]) -> str:
        steps = job.get("plan", {}).get("steps") or []
        index = int(job.get("step_index", 0))
        return str(steps[index]) if 0 <= index < len(steps) else ""

    @staticmethod
    def _batch_job_by_identity(
        jobs: list[dict[str, Any]], identity: dict[str, Any]
    ) -> dict[str, Any]:
        expected = (str(identity.get("o_id", "")), str(identity.get("io_id", "")))
        matches = [
            job
            for job in jobs
            if (str(job.get("o_id", "")), str(job.get("io_id", ""))) == expected
        ]
        if len(matches) != 1:
            raise SafetyStop("批次变化订单无法唯一映射回本地任务，禁止继续")
        return matches[0]

    @staticmethod
    def _job_product_group(
        job: dict[str, Any],
    ) -> Optional[tuple[tuple[str, str], ...]]:
        """Return the job's (货号, SKU 编号) signature, or None when unprovable."""

        items = job.get("plan", {}).get("items")
        if not isinstance(items, list) or not items:
            return None
        pairs: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                return None
            product_id = item.get("product_id")
            sku_id = item.get("sku_id")
            if (
                not isinstance(product_id, str)
                or not product_id.strip()
                or not isinstance(sku_id, str)
                or not sku_id.strip()
            ):
                return None
            pairs.append((product_id.strip(), sku_id.strip()))
        return tuple(sorted(
            set(pairs),
            key=lambda pair: (_natural_sort_key(pair[0]), _natural_sort_key(pair[1])),
        ))

    @classmethod
    def _prioritize_product_groups(
        cls, jobs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Group by the complete 货号 set, then naturally sort SKU details."""

        groups: dict[tuple[Any, ...], list[tuple[tuple[Any, ...], dict[str, Any]]]] = {}
        order: list[tuple[Any, ...]] = []
        for index, job in enumerate(jobs):
            signature = cls._job_product_group(job)
            # Group by the exact complete product set before comparing any SKU.
            products = (
                tuple(sorted({pid for pid, _sid in signature}, key=_natural_sort_key))
                if signature is not None else ()
            )
            key: tuple[Any, ...] = (
                ("product",) + products if signature is not None else ("order", index)
            )
            sku_key = (
                tuple((_natural_sort_key(pid), _natural_sort_key(sid)) for pid, sid in signature)
                if signature is not None else ()
            )
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((sku_key, job))
        order.sort(key=lambda key: (
            (0, tuple(_natural_sort_key(pid) for pid in key[1:]))
            if key[0] == "product" else (1, ())
        ))
        # Stable sorting preserves incoming order for equal SKU signatures and
        # keeps incomplete identities last in their original order.
        return [
            job
            for key in order
            for _sku_key, job in sorted(groups[key], key=lambda entry: entry[0])
        ]

    @classmethod
    def _next_preparation_batch(
        cls, jobs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return up to ten pending jobs sharing one exact waybill action."""

        if not jobs:
            return []
        step = cls._job_step(jobs[0])
        if step != "GET_WAYBILL" and not step.startswith("RESET_CARRIER"):
            return [jobs[0]]
        return [job for job in jobs if cls._job_step(job) == step][
            :BATCH_PRINT_SIZE
        ]

    @staticmethod
    def _next_print_batch(
        jobs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return the next exact print click group without exceeding ten."""

        return list(jobs[:BATCH_PRINT_SIZE])

    @classmethod
    def _group_active_jobs(
        cls, active_jobs: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        recovery = [
            job for job in active_jobs if str(job.get("status")) != "PENDING"
        ]
        preparation = [
            job
            for job in active_jobs
            if str(job.get("status")) == "PENDING"
            and cls._job_step(job) != "PRINT_EXPRESS"
        ]
        print_ready = [
            job
            for job in active_jobs
            if str(job.get("status")) == "PENDING"
            and cls._job_step(job) == "PRINT_EXPRESS"
        ]
        return recovery, preparation, cls._prioritize_product_groups(print_ready)

    @staticmethod
    def _split_active_jobs_for_profile(
        active_jobs: list[dict[str, Any]], print_profile: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Separate current-paper work from safely deferrable local history."""

        matching: list[dict[str, Any]] = []
        mismatched: list[dict[str, Any]] = []
        for job in active_jobs:
            if plan_print_profile(job.get("plan") or {}) == print_profile:
                matching.append(job)
            else:
                mismatched.append(job)
        return matching, mismatched

    @staticmethod
    def _should_print_before_preparation(
        recovery: list[dict[str, Any]], print_ready: list[dict[str, Any]]
    ) -> bool:
        """Drain a useful print batch or a partially settled committed batch first."""

        return len(print_ready) >= 2 or bool(print_ready and recovery)

    @staticmethod
    @contextmanager
    def _job_exception_scope(job: dict[str, Any]):
        """Attach the exact composite identity to exceptions from a batch item.

        Batch side effects are submitted once and then read back one order at a
        time.  The worker-level fallback only knows the first order in the
        batch, so an exception from a later readback must carry its own exact
        identity.  Nested scopes deliberately preserve the innermost identity.
        """

        try:
            yield
        except Exception as exc:
            if isinstance(exc, CommittedBatchUncertain):
                raise
            existing = getattr(exc, "_jst_job_identity", None)
            if not (
                isinstance(existing, tuple)
                and len(existing) == 2
                and all(isinstance(value, str) and value for value in existing)
            ):
                o_id = str(job.get("o_id", ""))
                io_id = str(job.get("io_id", ""))
                if o_id and io_id:
                    setattr(exc, "_jst_job_identity", (o_id, io_id))
            raise

    def _job_for_exception(
        self,
        attempted_job: Optional[dict[str, Any]],
        error: Optional[BaseException] = None,
    ) -> Optional[dict[str, Any]]:
        """Return the exact job whose processing raised the exception.

        Entering PREPARING updates a job's queue timestamp. Looking up the
        oldest job after an exception can therefore return a different order
        from the one that actually failed. A per-item batch exception identity
        takes priority over the worker's batch-head fallback; either way the
        exact composite identity is refreshed before any pause or retirement.

        Planning and batch-level setup have no single attempted identity, so
        those paths retain the queue fallback.
        """

        scoped_identity = getattr(error, "_jst_job_identity", None)
        if (
            isinstance(scoped_identity, tuple)
            and len(scoped_identity) == 2
            and all(isinstance(value, str) and value for value in scoped_identity)
        ):
            attempted_job = {
                "o_id": scoped_identity[0],
                "io_id": scoped_identity[1],
            }
        if attempted_job is None:
            return self.store.next_job()
        o_id = str(attempted_job.get("o_id", ""))
        io_id = str(attempted_job.get("io_id", ""))
        if not o_id or not io_id:
            return None
        exact = self.store.get_job(o_id, io_id)
        if exact and str(exact.get("status", "")) in ACTIVE_JOB_STATUSES:
            return exact
        return None

    def _pause_resumable_job(
        self,
        job: dict[str, Any],
        reason: str,
        pause_kind: str = "SAFETY_ENVIRONMENT",
    ) -> None:
        """Pause only states that are proven to be before a side effect."""

        if str(job.get("status", "")) == "RUNNING":
            return
        o_id = str(job.get("o_id", ""))
        io_id = str(job.get("io_id", ""))
        if not self.store.update_job(
            o_id,
            io_id,
            step_index=int(job.get("step_index", 0)),
            status="PAUSED",
            pause_kind=pause_kind,
            pause_reason=reason,
        ):
            self._auto_pause("异常任务状态无法安全持久化：" + reason, job.get("plan"))
            return
        # A proven per-order conflict is recorded before any business-effect
        # click.  Let other jobs that are already claimed and runnable finish
        # first; do not let one bad pair interrupt an otherwise healthy batch.
        # We still stop once only paused conflicts remain, so no lease is
        # silently released and no order is auto-skipped without an operator.
        if pause_kind == SKIPPABLE_PAUSE_KIND:
            has_other_runnable = getattr(
                self.store, "has_runnable_job_except", None
            )
            if callable(has_other_runnable) and has_other_runnable(o_id, io_id):
                self._event(
                    "WARN",
                    "JOB_CONFLICT_DEFERRED",
                    "当前异常单已在点击前隔离；先继续处理本批其他已领取订单，"
                    "健康订单完成后再暂停等待人工决定：" + reason,
                    o_id=o_id,
                    io_id=io_id,
                )
                self.set_status("异常单已隔离：继续处理本批其他健康订单")
                return
        self._auto_pause(reason, job.get("plan"))

    def _retire_uncertain_running(
        self, planner: PlannerClient, job: dict[str, Any], reason: str
    ) -> bool:
        """Complete and retire a RUNNING side effect, or leave it RUNNING.

        If the complete call cannot be proven, keeping RUNNING is essential:
        the next start performs read-only recovery and never blindly clicks.
        """

        step = self._job_step(job)
        try:
            if step == "PRINT_EXPRESS":
                self._skip_uncertain_print_job(planner, job, reason)
            elif step == "GET_WAYBILL" or step.startswith("RESET_CARRIER"):
                self._skip_uncertain_write_job(planner, job, reason)
            else:
                return False
        except Exception as exc:
            self._event(
                "ERROR",
                "UNCERTAIN_COMPLETE_FAILED",
                f"{reason}；后台终态确认失败（{exc}），保持 RUNNING 等待只读恢复",
                o_id=str(job.get("o_id", "")),
                io_id=str(job.get("io_id", "")),
            )
            return False
        return True

    def operator_skip_target(self) -> dict[str, Any]:
        """Select an exact problem order without restricting its error type."""

        if self.closing_event.is_set() or self.shutdown_event.is_set():
            raise RuntimeError("程序正在关闭，无法保存强制跳过记录")
        job = self.store.latest_problem_job()
        if job is None:
            raise RuntimeError("当前没有可强制跳过的异常订单")
        if not _is_ascii_order_id(job.get("o_id")) or not _is_ascii_order_id(job.get("io_id")):
            raise RuntimeError("异常订单缺少有效内部订单号或出库单号，无法记录后台跳过")
        return job

    def skip_current_and_continue(
        self, expected_identity: Optional[tuple[str, str]] = None
    ) -> tuple[str, str]:
        """Stop the worker, persist a global manual exclusion, then continue."""

        # The UI can submit twice or click Continue while the request is in
        # flight. Serialize skips and prevent a new worker until persistence.
        with self.operator_skip_lock:
            with self.commit_lock:
                job = self.operator_skip_target()
                o_id, io_id = str(job["o_id"]), str(job["io_id"])
                if expected_identity is not None:
                    if not isinstance(expected_identity, tuple) or len(expected_identity) != 2:
                        raise RuntimeError("待确认的异常订单身份格式无效，未执行跳过")
                    if tuple(map(str, expected_identity)) != (o_id, io_id):
                        raise RuntimeError(
                            f"异常订单已从 {expected_identity[0]}/{expected_identity[1]} "
                            f"变为 {o_id}/{io_id}，未执行跳过，请重新确认"
                        )
                with self.thread_lock:
                    self.force_skip_in_progress = True
            succeeded = False
            try:
                # Do not overwrite an in-flight worker's result. Wait for it
                # to finish its current call and stop; no replay is requested.
                self.stop(wait=True)
                current = self.store.get_job(o_id, io_id)
                if current is None:
                    raise RuntimeError("异常订单本地记录已不存在，未执行跳过")
                reason = (
                    f"人工强制跳过；原状态={current.get('status', '')}；"
                    f"异常类型={job.get('pause_kind', '')}；"
                    f"原因={job.get('pause_reason') or '人工指定问题单'}"
                )
                planner = PlannerClient(self._settings(), self.workstation_id)
                # No status proof or valid lease is needed for a manual
                # exclusion. The authenticated backend owns the durable list.
                planner.force_skip(o_id, io_id, reason)
                if not self.store.update_job(
                    o_id, io_id, step_index=int(current.get("step_index", 0)),
                    status="SKIPPED_OPERATOR",
                ):
                    raise RuntimeError("后台已确认跳过，但本地任务状态写入失败，请重新点击强制跳过恢复")
                self.store.exclude_job(o_id, io_id, reason)
                self._event(
                    "WARN", "SKIPPED_OPERATOR",
                    "操作员已强制跳过；已永久记录排除，继续下一单；该单转人工处理",
                    o_id=o_id, io_id=io_id,
                    detail={"previous_status": current.get("status", ""),
                            "pause_kind": job.get("pause_kind", ""), "reason": reason},
                )
                succeeded = True
            finally:
                with self.commit_lock:
                    self.force_skip_in_progress = False
            if succeeded and not self.closing_event.is_set() and not self.shutdown_event.is_set():
                self.resume()
            return o_id, io_id

    def _worker(self) -> None:
        startup_settings = self._settings()
        startup_label = PRINT_PROFILE_LABELS[startup_settings.print_profile]
        self.set_status(f"运行中：本轮只处理“{startup_label}”")
        self._event(
            "INFO",
            "ENGINE_START",
            f"自动打单已启动；本轮只处理“{startup_label}”",
        )
        try:
            while not self.stop_event.is_set() and not self.shutdown_event.is_set():
                if not self._wait_until_running():
                    break
                settings = self._settings()
                planner = PlannerClient(settings, self.workstation_id)
                attempted_job: Optional[dict[str, Any]] = None
                try:
                    active_jobs = self.store.active_jobs()
                    if active_jobs:
                        for queued_job in active_jobs:
                            queued_job["plan"]["skip_external_orders"] = settings.skip_external_orders
                        matching_jobs, mismatched_jobs = (
                            self._split_active_jobs_for_profile(
                                active_jobs, settings.print_profile
                            )
                        )
                        if mismatched_jobs:
                            # RUNNING means a side-effect boundary may already
                            # have been crossed. Recover that exact job first,
                            # using backend readback only; the paper selection
                            # is irrelevant to read-only reconciliation.
                            attempted_job = next(
                                (
                                    item
                                    for item in mismatched_jobs
                                    if str(item.get("status", "")) == "RUNNING"
                                ),
                                mismatched_jobs[0],
                            )
                            if str(attempted_job.get("status", "")) == "RUNNING":
                                self._process_job(planner, attempted_job)
                            else:
                                old_profile = plan_print_profile(
                                    attempted_job.get("plan") or {}
                                )
                                self._release_retryable_job(
                                    planner,
                                    attempted_job,
                                    "RELEASED_PROFILE_MISMATCH",
                                    "本地旧任务需要“"
                                    f"{PRINT_PROFILE_LABELS[old_profile]}”，"
                                    "当前选择为“"
                                    f"{PRINT_PROFILE_LABELS[settings.print_profile]}”",
                                )
                            self.transient_api_failures = 0
                            continue
                        active_jobs = matching_jobs
                        recovery, preparation, print_ready = self._group_active_jobs(
                            active_jobs
                        )
                        print_before_preparation = (
                            self._should_print_before_preparation(
                                recovery, print_ready
                            )
                        )
                        if print_before_preparation:
                            print_batch = self._next_print_batch(print_ready)
                            attempted_job = print_batch[0]
                            if len(print_batch) >= 2:
                                with self._job_exception_scope(attempted_job):
                                    self._process_print_batch(planner, print_batch)
                            else:
                                self._process_job(planner, attempted_job)
                        elif preparation:
                            # Every job in this sub-batch has the exact same
                            # get-waybill/reset-carrier action.  Use JST's real
                            # multi-selection and click once instead of taking
                            # numbers one by one.
                            waybill_batch = self._next_preparation_batch(preparation)
                            attempted_job = waybill_batch[0]
                            if len(waybill_batch) >= 2:
                                with self._job_exception_scope(attempted_job):
                                    self._process_waybill_batch(planner, waybill_batch)
                            else:
                                self._process_job(planner, attempted_job)
                        elif print_ready:
                            attempted_job = print_ready[0]
                            self._process_job(planner, attempted_job)
                        elif recovery:
                            # A historical PAUSED/PREPARING/RUNNING job must not
                            # hold already prepared print jobs behind it. Recover
                            # it only after all currently runnable work has moved.
                            attempted_job = recovery[0]
                            self._process_job(planner, attempted_job)
                        else:
                            attempted_job = active_jobs[0]
                            self._process_job(planner, attempted_job)
                        self.transient_api_failures = 0
                        continue
                    payload = planner.plan(
                        self.store.excluded_pairs(),
                        settings.print_profile,
                        max_candidates=(BATCH_PRINT_SIZE if settings.allow_print else 1),
                    )
                    self.transient_api_failures = 0
                    self._record_blocked(payload, settings.print_profile)
                    selected = payload.get("selected") or []
                    if selected:
                        self.no_order_notice_key = None
                        accepted = 0
                        for plan in selected:
                            attempted_job = {
                                "o_id": str(plan.get("o_id", "")),
                                "io_id": str(plan.get("io_id", "")),
                            }
                            if self.store.save_job(plan):
                                accepted += 1
                                self._event(
                                    "INFO",
                                    "PLAN_SELECTED",
                                    f"已加入“{PRINT_PROFILE_LABELS[settings.print_profile]}”"
                                    f"打印队列；流程：{describe_plan_steps(plan)}",
                                    o_id=str(plan.get("o_id", "")),
                                    io_id=str(plan.get("io_id", "")),
                                )
                                continue
                            o_id, io_id, token = self._claim(plan)
                            existing = self.store.get_job(o_id, io_id)
                            if existing and str(existing.get("status")) not in ACTIVE_JOB_STATUSES:
                                existing_status = str(existing.get("status", ""))
                                if existing_status not in SERVER_SYNCABLE_TERMINAL_STATUSES:
                                    raise SafetyStop(
                                        "后台返回本机非业务完成型旧终态，已保留租约并暂停人工核对；"
                                        "禁止释放轮转或擅自标记服务端完成"
                                    )
                                # Synchronize a trusted local terminal state to
                                # the newly claimed server lease. Never release
                                # it back into a >200 legacy rotation.
                                completion_reason = (
                                    self._persisted_terminal_completion_reason(
                                        planner, plan, existing_status
                                    )
                                )
                                planner.complete(
                                    o_id, io_id, token, completion_reason
                                )
                                self.store.exclude_job(
                                    o_id, io_id, "本机终态已同步至后台"
                                )
                                self._event(
                                    "WARN",
                                    "TERMINAL_STATE_SYNCED",
                                    "后台重新返回本机可信终态订单，已确认服务端永久终态；"
                                    "未执行任何 ERP 浏览器动作",
                                    o_id=o_id,
                                    io_id=io_id,
                                    detail={"completion_reason": completion_reason},
                                )
                            else:
                                planner.release(o_id, io_id, token)
                                raise SafetyStop("本地存在同复合身份的活动任务，已释放新租约")
                        if accepted:
                            self.set_status(
                                f"运行中：已领取 {accepted} 笔“"
                                f"{PRINT_PROFILE_LABELS[settings.print_profile]}”订单，"
                                "正在取号并准备打印"
                            )
                        continue
                    self._announce_no_orders(settings, payload)
                    self._wait(settings.loop_seconds)
                except CommittedBatchUncertain as exc:
                    # The click boundary covered every identity in this batch.
                    # Do not arbitrarily retire the batch head: leave all jobs
                    # RUNNING so the next worker round resolves each one using
                    # the existing read-only recovery path.
                    self._event(
                        "WARN",
                        "BATCH_COMMIT_READBACK_RECOVERY",
                        str(exc),
                    )
                    self.set_status("批量提交响应不确定：正在逐单只读恢复")
                    continue
                except OperatorPaused as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if active:
                        self._pause_resumable_job(
                            active, str(exc), pause_kind="OPERATOR_PAUSED"
                        )
                    else:
                        self.run_event.clear()
                        self.set_status(f"已暂停：{exc}")
                except OperatorStopped as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if active and str(active.get("status")) != "RUNNING":
                        self.store.update_job(
                            str(active["o_id"]),
                            str(active["io_id"]),
                            step_index=int(active["step_index"]),
                            status="PAUSED",
                            pause_kind="OPERATOR_STOPPED",
                            pause_reason=str(exc),
                        )
                    self._event("INFO", "OPERATOR_STOP_BOUNDARY", str(exc))
                    break
                except CompletionProofError as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if self._defer_new_running_action_recovery(
                        attempted_job, active, str(exc)
                    ):
                        continue
                    if active and str(active.get("status")) == "RUNNING":
                        # A side effect may have crossed the click boundary.
                        # Keep RUNNING so restart can only use read-only
                        # recovery; never turn this into a permanent exclusion.
                        self.run_event.clear()
                        self._event(
                            "BLOCKED",
                            "COMPLETION_PROOF_REQUIRED",
                            str(exc) + "；保持 RUNNING，未永久排除订单",
                            o_id=str(active.get("o_id", "")),
                            io_id=str(active.get("io_id", "")),
                        )
                        self.set_status(f"完成证据不足，已暂停：{exc}")
                    elif active:
                        self._pause_resumable_job(
                            active,
                            str(exc),
                            pause_kind="COMPLETION_PROOF_REQUIRED",
                        )
                    else:
                        self._auto_pause(str(exc))
                except LeaseLostError as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if active:
                        self._retire_lost_lease(active, str(exc))
                        continue
                    self._auto_pause(str(exc))
                except TransientAPIError as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    self._retry_transient_api(active, str(exc))
                    continue
                except OrderRowNotReady as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if active and str(active.get("status")) != "RUNNING":
                        self._retry_missing_dom_row(active, str(exc))
                        continue
                    if self._defer_new_running_action_recovery(
                        attempted_job, active, str(exc)
                    ):
                        continue
                    if active and str(active.get("status")) == "RUNNING":
                        # The side-effect boundary may already have been
                        # crossed.  Never return this task to PENDING and never
                        # repeat the page action.  The next worker round enters
                        # _process_job's RUNNING branch, which performs only
                        # backend readback/reconciliation.
                        self._event(
                            "WARN",
                            "RUNNING_DOM_READBACK_RECOVERY",
                            "页面在动作提交后暂时不可定位；保持 RUNNING，"
                            "稍后仅回读后台结果，绝不重复点击：" + str(exc),
                            o_id=str(active.get("o_id", "")),
                            io_id=str(active.get("io_id", "")),
                        )
                        self.set_status("页面响应中断：正在对已提交任务做只读恢复")
                        self._wait(2.0)
                        continue
                    self._auto_pause(
                        f"页面订单行在不可自动恢复的状态下消失：{exc}",
                        (active or {}).get("plan"),
                    )
                except TerminalStatusSKUValidationError as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if self._defer_new_running_action_recovery(
                        attempted_job, active, str(exc)
                    ):
                        continue
                    if active and str(active.get("status")) == "RUNNING":
                        self.run_event.clear()
                        self._event(
                            "BLOCKED",
                            "TERMINAL_STATUS_SKU_BLOCKED",
                            str(exc) + "；未确认服务端终态，保持 RUNNING 仅允许下次只读恢复",
                            o_id=str(active.get("o_id", "")),
                            io_id=str(active.get("io_id", "")),
                        )
                        self.set_status(f"Sent 订单 SKU 核验暂停：{exc}")
                    elif active:
                        self._pause_resumable_job(
                            active, str(exc), pause_kind=SKIPPABLE_PAUSE_KIND
                        )
                    else:
                        self._auto_pause(str(exc))
                except UnknownOrderStatus as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if self._defer_new_running_action_recovery(
                        attempted_job, active, str(exc)
                    ):
                        continue
                    if active and str(active.get("status")) == "RUNNING":
                        self.run_event.clear()
                        self._event(
                            "BLOCKED",
                            "UNKNOWN_ORDER_STATUS",
                            str(exc) + "；未命中 Sent/Delete 白名单，保持 RUNNING 仅允许下次只读恢复",
                            o_id=str(active.get("o_id", "")),
                            io_id=str(active.get("io_id", "")),
                        )
                        self.set_status(f"未知订单状态暂停：{exc}")
                    elif active:
                        # The status changed before this worker crossed a new
                        # side-effect boundary (for example WaitConfirm became
                        # Confirmed). It is no longer eligible for this queue:
                        # release it and keep the workstation moving. Should it
                        # become eligible again, save_job can safely replace the
                        # released audit row with a fresh claimed plan.
                        try:
                            self._release_retryable_job(
                                planner,
                                active,
                                "RELEASED_STATUS_CHANGED",
                                str(exc),
                            )
                        except TransientAPIError as release_error:
                            self._retry_transient_api(active, str(release_error))
                        except Exception as release_error:
                            self._pause_resumable_job(
                                active,
                                "状态变化任务释放失败：" + str(release_error),
                            )
                    else:
                        self._auto_pause(str(exc))
                except PermanentJobError as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if self._defer_new_running_action_recovery(
                        attempted_job, active, str(exc)
                    ):
                        continue
                    if active and str(active.get("status")) == "RUNNING":
                        if self._retire_uncertain_running(planner, active, str(exc)):
                            continue
                        self.run_event.clear()
                        self.set_status(f"不确定动作暂停：{exc}")
                    elif active:
                        if isinstance(exc, ExternalSystemOrderSkipped):
                            try:
                                self._release_retryable_job(planner, active, "RELEASED_EXTERNAL_ORDER", str(exc))
                            except TransientAPIError as release_error:
                                self._retry_transient_api(active, str(release_error))
                            except Exception as release_error:
                                self._pause_resumable_job(active, "外部订单跳过释放失败：" + str(release_error))
                        else:
                            self._pause_resumable_job(
                                active, str(exc), pause_kind=SKIPPABLE_PAUSE_KIND
                            )
                    else:
                        self._auto_pause(str(exc))
                except SafetyStop as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if self._defer_new_running_action_recovery(
                        attempted_job, active, str(exc)
                    ):
                        continue
                    if active and str(active.get("status")) == "RUNNING":
                        if self._retire_uncertain_running(planner, active, str(exc)):
                            continue
                        self.run_event.clear()
                        self.set_status(f"不确定动作暂停：{exc}")
                    elif active:
                        self._pause_resumable_job(active, str(exc))
                    else:
                        self._auto_pause(str(exc))
                except BackendSchemaError as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if self._defer_new_running_action_recovery(
                        attempted_job, active, str(exc)
                    ):
                        continue
                    if active and str(active.get("status")) != "RUNNING":
                        self.store.update_job(
                            str(active["o_id"]),
                            str(active["io_id"]),
                            step_index=int(active["step_index"]),
                            status="PAUSED",
                            pause_kind="BACKEND_SCHEMA",
                            pause_reason=str(exc),
                        )
                    self._auto_pause(f"后台契约不兼容：{exc}", (active or {}).get("plan"))
                except Exception as exc:
                    active = self._job_for_exception(attempted_job, exc)
                    if self._defer_new_running_action_recovery(
                        attempted_job, active, str(exc)
                    ):
                        continue
                    if active and str(active.get("status")) != "RUNNING":
                        self.store.update_job(
                            str(active["o_id"]),
                            str(active["io_id"]),
                            step_index=int(active["step_index"]),
                            status="PAUSED",
                            pause_kind="UNEXPECTED_ERROR",
                            pause_reason=str(exc),
                        )
                    self._event(
                        "ERROR",
                        "UNEXPECTED_ERROR",
                        str(exc),
                        o_id=str((active or {}).get("o_id", "")),
                        io_id=str((active or {}).get("io_id", "")),
                    )
                    self.run_event.clear()
                    self.set_status(f"异常暂停：{exc}")
        finally:
            # Stop/start and paper changes reuse the same trusted dedicated
            # browser socket. Only final app shutdown closes it.
            closing = getattr(self, "closing_event", None)
            if self.shutdown_event.is_set() or (
                isinstance(closing, threading.Event) and closing.is_set()
            ):
                self._disconnect_browser()
            self._event("INFO", "ENGINE_STOP", "自动化引擎已停止")
            self.set_status("已停止")


class DesktopApp:
    def __init__(self, root, *, no_print: bool = False):
        self.root = root
        self.no_print = no_print
        self.store = EventStore()
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.settings = load_settings()
        self.workstation_id = load_workstation_id()
        self.engine: Optional[AutomationEngine] = None
        self._start_check_running = False
        self._pending_start_settings: Optional[Settings] = None
        self._browser_open_running = False
        self._closing = False
        mode_name = "试运行（不打印）" if no_print else "正式自动打单"
        self.root.title(f"{APP_NAME} {APP_VERSION} - {mode_name}")
        self.root.geometry("1080x720")
        self._variables()
        self._build()
        self._insert_event(
            self.store.event(
                "INFO",
                "APP_START",
                f"当前版本 V{APP_VERSION} 已启动；界面只显示本次启动后的日志，历史异常仍可导出",
            )
        )
        for notice in self.store.startup_notices:
            self._insert_event(notice)
        for warning in SETTINGS_WARNINGS:
            self._insert_event(
                self.store.event("WARN", "LOCAL_CONFIG_IGNORED", warning)
            )
        self.root.after(150, self._drain_messages)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        # Installation/startup should land in JST without asking the operator
        # to find the page manually.  This only opens a page in the existing
        # Chrome profile; it never starts the business engine or performs an
        # order action.
        self.root.after(800, self._open_browser)

    def _variables(self) -> None:
        s = self.settings
        self.browser_var = tk.StringVar(value=s.browser_name)
        self.print_profile_var = tk.StringVar(value="")
        self.skip_external_orders_var = tk.BooleanVar(value=s.skip_external_orders)
        self.sku_export_date_var = tk.StringVar(value=date.today().isoformat())
        self.status_var = tk.StringVar(
            value="未启动：试运行不打印" if self.no_print else "未启动：正式自动打单"
        )

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        config = ttk.LabelFrame(outer, text="运行设置", padding=10)
        config.pack(fill="x")
        self.browser_combo = ttk.Combobox(
            config,
            textvariable=self.browser_var,
            values=("Chrome", "Edge"),
            width=14,
            state="readonly",
        )
        self.browser_combo.pack(side="left", padx=(0, 12))
        ttk.Button(
            config,
            text="进入聚水潭",
            command=self._open_browser,
        ).pack(side="left")
        ttk.Label(config, text="本轮面单类型：").pack(side="left", padx=(24, 6))
        self.print_profile_combo = ttk.Combobox(
            config,
            textvariable=self.print_profile_var,
            values=tuple(PRINT_PROFILE_BY_LABEL),
            width=18,
            state="readonly",
        )
        self.print_profile_combo.pack(side="left")
        ttk.Label(config, text="SKU导出打印日期：").pack(side="left", padx=(18, 6))
        self.sku_export_date_entry = ttk.Entry(
            config,
            textvariable=self.sku_export_date_var,
            width=12,
            state="readonly",
        )
        self.sku_export_date_entry.pack(side="left")
        ttk.Button(
            config,
            text="选择日期",
            width=8,
            command=self._choose_sku_export_date,
        ).pack(side="left", padx=(4, 0))
        ttk.Label(
            config,
            text="单机本地协调" if self.settings.local_mode else "后台服务已预配置",
            foreground="#357a38",
        ).pack(side="right")

        self.skip_external_orders_check = ttk.Checkbutton(
            outer, text="跳过外部系统订单（标签含“外部系统订单”）",
            variable=self.skip_external_orders_var,
        )
        self.skip_external_orders_check.pack(anchor="w", pady=(8, 0))

        controls = ttk.Frame(outer, padding=(0, 12))
        controls.pack(fill="x")
        ttk.Button(controls, text="开始", command=self._start).pack(side="left", padx=4)
        ttk.Button(controls, text="暂停", command=self._pause).pack(side="left", padx=4)
        ttk.Button(controls, text="继续", command=self._resume).pack(side="left", padx=4)
        ttk.Button(controls, text="停止", command=self._stop).pack(side="left", padx=4)
        ttk.Button(
            controls,
            text="强制跳过异常单并继续",
            command=self._skip_current,
        ).pack(side="left", padx=(18, 4))
        ttk.Button(
            controls, text="导出SKU出库Excel", command=self._export_sku
        ).pack(side="left", padx=4)
        ttk.Button(controls, text="导出异常 CSV", command=self._export).pack(
            side="left", padx=4
        )
        ttk.Label(controls, textvariable=self.status_var).pack(side="right")

        warning = (
            f"安全边界：人工锁定本轮面单类型，只领取同类订单并组成最多 {BATCH_PRINT_SIZE} 单批量打印；"
            "卖家备注含“隐私”或标记/多标签含“紧急单”时切中通隐私面单；"
            "不会调用预发货/直接发货接口。"
            + ("当前为试运行，只改快递/取号，不打印。" if self.no_print else "当前为正式模式，会真实打印。")
        )
        ttk.Label(outer, text=warning, foreground="#9a4d00").pack(fill="x", pady=(0, 8))

        columns = ("time", "severity", "event", "o_id", "io_id", "message")
        self.table = ttk.Treeview(outer, columns=columns, show="headings")
        headings = {
            "time": "时间",
            "severity": "级别",
            "event": "事件",
            "o_id": "内部订单号",
            "io_id": "出库单号",
            "message": "说明",
        }
        widths = {"time": 150, "severity": 70, "event": 150, "o_id": 100, "io_id": 110, "message": 440}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor="w")
        scroll = ttk.Scrollbar(outer, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _choose_sku_export_date(self) -> None:
        """Open a dependency-free calendar and update the readonly date field."""

        try:
            initial = parse_export_date(self.sku_export_date_var.get())
        except ValueError:
            initial = date.today()

        dialog = tk.Toplevel(self.root)
        dialog.title("选择SKU导出打印日期")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

        visible = {"year": initial.year, "month": initial.month}
        header = ttk.Frame(dialog, padding=(10, 10, 10, 4))
        header.pack(fill="x")
        month_label = tk.StringVar()
        days_frame = ttk.Frame(dialog, padding=(10, 4, 10, 6))
        days_frame.pack(fill="both", expand=True)

        def choose(selected: date) -> None:
            self.sku_export_date_var.set(selected.isoformat())
            dialog.destroy()

        def render() -> None:
            for child in days_frame.winfo_children():
                child.destroy()
            year = visible["year"]
            month = visible["month"]
            month_label.set(f"{year}年{month}月")
            for column, text in enumerate(("一", "二", "三", "四", "五", "六", "日")):
                ttk.Label(days_frame, text=text, anchor="center", width=4).grid(
                    row=0, column=column, padx=1, pady=(0, 3)
                )
            for row_index, week in enumerate(
                calendar_month_dates(year, month), start=1
            ):
                for column, day_value in enumerate(week):
                    if day_value.month != month:
                        ttk.Label(days_frame, text="", width=4).grid(
                            row=row_index, column=column, padx=1, pady=1
                        )
                        continue
                    button = ttk.Button(
                        days_frame,
                        text=str(day_value.day),
                        width=3,
                        command=lambda selected=day_value: choose(selected),
                    )
                    button.grid(row=row_index, column=column, padx=1, pady=1)
                    if day_value == initial:
                        button.focus_set()

        def move(offset: int) -> None:
            try:
                year, month = shift_calendar_month(
                    visible["year"], visible["month"], offset
                )
            except ValueError:
                return
            visible.update(year=year, month=month)
            render()

        ttk.Button(header, text="上个月", command=lambda: move(-1)).pack(side="left")
        ttk.Label(header, textvariable=month_label, anchor="center", width=14).pack(
            side="left", expand=True, padx=8
        )
        ttk.Button(header, text="下个月", command=lambda: move(1)).pack(side="right")

        footer = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        footer.pack(fill="x")
        ttk.Button(footer, text="今天", command=lambda: choose(date.today())).pack(
            side="left"
        )
        ttk.Button(footer, text="取消", command=dialog.destroy).pack(side="right")

        render()
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(
            0, (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2
        )
        y = self.root.winfo_rooty() + 70
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()

    def _current_settings(self) -> Settings:
        print_profile = PRINT_PROFILE_BY_LABEL.get(self.print_profile_var.get())
        if print_profile is None:
            raise ValueError("请先选择本轮面单类型，并确认打印机已装入对应面单纸")
        settings = Settings(
            browser_name=self.browser_var.get(),
            debug_port=self.settings.debug_port,
            api_url=self.settings.api_url,
            api_token=self.settings.api_token,
            backend_mode=self.settings.backend_mode,
            loop_seconds=self.settings.loop_seconds,
            allow_write=True,
            allow_print=not self.no_print,
            print_profile=print_profile,
            skip_external_orders=self.skip_external_orders_var.get(),
        )
        settings.validate()
        return settings

    def _notify(self, item: dict[str, Any]) -> None:
        self.messages.put(("event", item))

    def _set_status(self, value: str) -> None:
        self.messages.put(("status", value))

    def _run_async(self, function: Callable[[], None]) -> None:
        threading.Thread(target=function, daemon=True).start()

    def _open_browser(self) -> None:
        if self._closing or self._browser_open_running:
            return
        try:
            settings = Settings(**asdict(self.settings))
            settings.browser_name = self.browser_var.get()
            settings.validate()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self._browser_open_running = True
        self.status_var.set("正在启动专用浏览器并进入聚水潭…")

        def task() -> None:
            try:
                launch_browser(settings)
                open_jst_print_page(settings)
            except Exception as exc:
                self.messages.put(("browser_open_error", str(exc)))
            else:
                self.messages.put(
                    ("browser_open_ok", "已在专用浏览器打开聚水潭打单拣货页")
                )

        self._run_async(task)

    def _start(self) -> None:
        try:
            settings = self._current_settings()
            save_settings(settings)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"启动前检查失败：{exc}")
            return
        if self._start_check_running:
            return
        confirmation = (
            f"本轮只处理“{PRINT_PROFILE_LABELS[settings.print_profile]}”。"
            f"请确认打印机已装入对应面单纸；开始后会逐单核验并组成最多 {BATCH_PRINT_SIZE} 单真实批量打印。"
            "不会调用预发货/直接发货接口。确认开始吗？"
            if settings.allow_print
            else "开始试运行后会连续逐单改快递和取号，但不会打印。确认开始吗？"
        )
        if not messagebox.askyesno(APP_NAME, confirmation):
            return

        self._start_check_running = True
        self._pending_start_settings = Settings(**asdict(settings))
        self.browser_combo.configure(state="disabled")
        self.print_profile_combo.configure(state="disabled")
        self.skip_external_orders_check.configure(state="disabled")
        self.status_var.set("正在检查浏览器和聚水潭只读查询…")

        def task() -> None:
            try:
                # Start is also a recovery entry point when the dedicated
                # browser is not running. launch_browser starts its persistent
                # application-owned profile without opening debugging UI.
                launch_browser(settings)
                cdp_endpoint(settings.debug_port, settings.browser_name)
                PlannerClient(settings, self.workstation_id).ping()
            except Exception as exc:
                self.messages.put(("start_error", str(exc)))
            else:
                self.messages.put(("start_ready", settings))

        self._run_async(task)

    def _activate_engine(self, settings: Settings) -> None:
        if self._closing:
            return
        settings.validate()
        self.browser_var.set(settings.browser_name)
        self.print_profile_var.set(PRINT_PROFILE_LABELS[settings.print_profile])
        if self.engine is None:
            self.engine = AutomationEngine(
                settings,
                self.store,
                self._notify,
                self._set_status,
                self.workstation_id,
            )
        else:
            self.engine.update_settings(settings)
        self.browser_combo.configure(state="disabled")
        self.print_profile_combo.configure(state="disabled")
        self.skip_external_orders_check.configure(state="disabled")
        self.engine.start()

    def _restore_setting_controls(self) -> None:
        self.browser_combo.configure(state="readonly")
        self.print_profile_combo.configure(state="readonly")
        self.skip_external_orders_check.configure(state="normal")

    def _start_settings_still_match(self, settings: Any) -> bool:
        pending = self._pending_start_settings
        if not isinstance(settings, Settings) or pending is None:
            return False
        try:
            settings.validate()
        except (TypeError, ValueError):
            return False
        return (
            asdict(settings) == asdict(pending)
            and self.skip_external_orders_var.get() == settings.skip_external_orders
            and self.browser_var.get() == settings.browser_name
            and self.print_profile_var.get()
            == PRINT_PROFILE_LABELS.get(settings.print_profile)
        )

    def _pause(self) -> None:
        if self.engine:
            self.status_var.set("正在暂停，等待当前安全边界…")

            def task() -> None:
                self.engine.pause()
                self.messages.put(("pause_done", "已暂停"))

            self._run_async(task)

    def _resume(self) -> None:
        if self._closing:
            return
        if not self.engine:
            self._start()
            return
        try:
            settings = self._current_settings()
            confirmation = (
                "继续后会自动改快递、取号并真实打印，确认吗？"
                if settings.allow_print
                else "继续试运行会改快递和取号，但不会打印，确认吗？"
            )
            if not messagebox.askyesno(APP_NAME, confirmation):
                return
            self.engine.update_settings(settings)
            self.engine.resume()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _stop(self) -> None:
        if self.engine:
            self.status_var.set("正在停止，等待当前安全边界…")

            def task() -> None:
                self.engine.stop(wait=True)
                self.messages.put(("stop_done", "正在停止"))

            self._run_async(task)

    def _skip_current(self) -> None:
        if self._closing:
            return
        if not self.engine:
            messagebox.showwarning(APP_NAME, "当前没有运行中的自动化任务")
            return
        try:
            target = self.engine.operator_skip_target()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"无法跳过：{exc}")
            return
        target_identity = (str(target["o_id"]), str(target["io_id"]))
        pause_reason = str(target.get("pause_reason", "")).strip()
        if not messagebox.askyesno(
            APP_NAME,
            f"确认强制跳过异常订单 {target_identity[0]}/{target_identity[1]} 吗？\n\n"
            f"异常原因：{pause_reason or '未记录具体原因'}\n\n"
            "确认后后台将永久记录并从所有工作站自动队列排除该单，继续下一单。\n"
            "已经发生的取号或打印动作不会撤销，该单须转人工处理。",
        ):
            return

        def task() -> None:
            try:
                skipped = self.engine.skip_current_and_continue(target_identity)
            except Exception as exc:
                self.messages.put(("skip_error", str(exc)))
            else:
                self.messages.put(
                    (
                        "skip_ok",
                        f"异常订单 {skipped[0]}/{skipped[1]} 已从所有工作站永久排除，"
                        "请转人工处理；程序继续检查下一单",
                    )
                )

        self._run_async(task)

    def _export(self) -> None:
        target = filedialog.asksaveasfilename(
            title="导出异常记录",
            defaultextension=".csv",
            initialfile=f"聚水潭打单异常_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not target:
            return
        count = self.store.export_anomalies(Path(target))
        messagebox.showinfo(APP_NAME, f"已导出 {count} 条异常记录")

    def _export_sku(self) -> None:
        try:
            selected_date = parse_export_date(
                self.sku_export_date_var.get()
            ).isoformat()
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc))
            return
        self.sku_export_date_var.set(selected_date)
        target = filedialog.asksaveasfilename(
            title=f"导出 {selected_date} SKU出库明细",
            defaultextension=".xlsx",
            initialfile=(
                f"SKU出库明细_{selected_date.replace('-', '')}_"
                f"{datetime.now():%H%M%S}.xlsx"
            ),
            filetypes=[("Excel 工作簿", "*.xlsx")],
        )
        if not target:
            return
        try:
            line_count, order_count = self.store.export_sku_outbound_xlsx(
                Path(target), selected_date
            )
        except Exception as exc:
            messagebox.showwarning(APP_NAME, str(exc))
            return
        messagebox.showinfo(
            APP_NAME,
            f"已导出 {selected_date} 的 {line_count} 条 SKU 明细，"
            f"涉及 {order_count} 个订单。",
        )

    def _insert_event(self, item: dict[str, Any]) -> None:
        severity = str(item.get("severity", ""))
        event_type = str(item.get("event_type", ""))
        display_time = str(item.get("created_at", "")).replace("T", " ")
        self.table.insert(
            "",
            "end",
            values=(
                display_time,
                UI_SEVERITY_LABELS.get(severity, severity),
                UI_EVENT_LABELS.get(event_type, event_type),
                item.get("o_id", ""),
                item.get("io_id", ""),
                item.get("message", ""),
            ),
        )
        children = self.table.get_children()
        if len(children) > 500:
            self.table.delete(children[0])
        self.table.yview_moveto(1.0)

    def _drain_messages(self) -> None:
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "event":
                    self._insert_event(value)
                elif kind == "status":
                    self.status_var.set(str(value))
                elif kind == "browser_open_error":
                    self._browser_open_running = False
                    self.status_var.set(f"浏览器连接失败：{value}")
                elif kind == "browser_open_ok":
                    self._browser_open_running = False
                    self.status_var.set(str(value))
                elif kind == "start_error":
                    self._start_check_running = False
                    self._pending_start_settings = None
                    self._restore_setting_controls()
                    self.status_var.set("启动前检查失败")
                    messagebox.showerror(APP_NAME, f"启动前检查失败：{value}")
                elif kind == "start_ready":
                    self._start_check_running = False
                    if not self._closing:
                        if not self._start_settings_still_match(value):
                            self._pending_start_settings = None
                            self._restore_setting_controls()
                            self.status_var.set("启动设置已变化，未启动")
                            messagebox.showerror(
                                APP_NAME,
                                "启动检查期间浏览器或面单类型已变化，请重新确认",
                            )
                        else:
                            self._pending_start_settings = None
                            try:
                                self._activate_engine(value)
                            except Exception as exc:
                                self._restore_setting_controls()
                                self.status_var.set("启动失败")
                                messagebox.showerror(APP_NAME, f"启动失败：{exc}")
                elif kind == "skip_error":
                    if not self._closing:
                        messagebox.showerror(APP_NAME, f"无法跳过：{value}")
                elif kind == "skip_ok":
                    if not self._closing:
                        messagebox.showinfo(APP_NAME, str(value))
                elif kind in {"pause_done", "stop_done"}:
                    self.status_var.set(str(value))
                    if kind == "stop_done":
                        self._restore_setting_controls()
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(150, self._drain_messages)

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.status_var.set("正在安全停止，请稍候…")
        if self.engine:
            self.engine.latch_shutdown()
            self._run_async(self.engine.shutdown)
        self._finish_close()

    def _finish_close(self) -> None:
        if self.engine and self.engine.is_alive():
            self.root.after(100, self._finish_close)
            return
        self.root.destroy()


def self_test(output_path: Optional[Path] = None) -> int:
    errors: list[str] = []
    try:
        settings = load_settings()
        settings.validate()
        parsed = parse_json_output('noise\n{"ok": true}\n')
        if parsed != {"ok": True}:
            raise AssertionError("JSON parser self-test failed")
        with sqlite3.connect(":memory:") as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        errors.append(f"基础配置/SQLite 自检失败：{exc}")

    tkinter_imported = tk is not None
    tkinter_available = tkinter_imported
    tkinter_root = None
    if tkinter_available:
        try:
            tkinter_root = tk.Tk()
            tkinter_root.withdraw()
            tkinter_root.update_idletasks()
        except Exception as exc:
            tkinter_available = False
            errors.append(f"tkinter 窗口初始化失败：{exc}")
        finally:
            if tkinter_root is not None:
                try:
                    tkinter_root.destroy()
                except Exception:
                    pass
    native_cdp_available = _module_available("websocket")
    if not tkinter_imported:
        errors.append("缺少 tkinter，桌面界面无法启动")
    if not native_cdp_available:
        errors.append("缺少 websocket-client，原生 CDP/fetch 调用无法运行")
    payload = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "python": sys.version.split()[0],
        "tkinter": tkinter_available,
        "native_cdp": native_cdp_available,
        "browser_chrome": browser_executable("Chrome"),
        "browser_edge": browser_executable("Edge"),
        "print_service_online": print_service_online(),
        "self_test": "FAIL" if errors else "PASS",
        "errors": errors,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_path is not None:
        Path(output_path).write_text(rendered + "\n", encoding="utf-8")
    if sys.stdout is not None:
        print(rendered)
    return 2 if errors else 0


def live_readonly_probe(output_path: Optional[Path] = None) -> int:
    """Exercise the packaged native CDP/fetch path without business writes."""

    errors: list[str] = []
    row_counts: list[int] = []
    total_counts: list[int] = []
    browser: Optional[JSTNativeBrowser] = None
    backend_ping = False
    healthy_before = False
    healthy_after = False
    try:
        settings = load_settings()
        settings.allow_write = False
        settings.allow_print = False
        settings.validate()
        PlannerClient(settings, load_workstation_id()).ping()
        backend_ping = True
        browser = JSTNativeBrowser(settings)
        healthy_before = browser.is_healthy()
        if not healthy_before:
            raise RuntimeError("原生 CDP 连接建立后健康检查失败")
        for _ in range(3):
            returned = browser._call_page(
                "LoadDataToJSON",
                ["1", "[]", "{}"],
                call_control=None,
                timeout=60,
            )
            if not isinstance(returned, str):
                raise RuntimeError("原生 fetch 只读列表未返回 JSON 文本")
            parsed = json.loads(returned)
            rows = parsed.get("datas") if isinstance(parsed, dict) else None
            data_page = parsed.get("dp") if isinstance(parsed, dict) else None
            if not isinstance(rows, list) or not isinstance(data_page, dict):
                raise RuntimeError("原生 fetch 只读列表结构无效")
            total = data_page.get("DataCount")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise RuntimeError("原生 fetch 只读列表总数无效")
            row_counts.append(len(rows))
            total_counts.append(total)
        healthy_after = browser.is_healthy()
        if not healthy_after:
            raise RuntimeError("连续原生 fetch 后 CDP 长连接健康检查失败")
    except Exception as exc:
        errors.append(str(exc))
    finally:
        if browser is not None:
            browser.close()
    payload = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "platform": platform.platform(),
        "backend_ping": backend_ping,
        "cdp_healthy_before": healthy_before,
        "native_fetch_reads": len(row_counts),
        "row_counts": row_counts,
        "total_counts": total_counts,
        "cdp_healthy_after": healthy_after,
        "business_writes": 0,
        "probe": "FAIL" if errors else "PASS",
        "errors": errors,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_path is not None:
        Path(output_path).write_text(rendered + "\n", encoding="utf-8")
    if sys.stdout is not None:
        print(rendered)
    return 2 if errors else 0


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--live-readonly-probe", action="store_true")
    parser.add_argument("--self-test-output", type=Path)
    parser.add_argument("--probe-output", type=Path)
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="operator acceptance mode: allow carrier/waybill writes but never print",
    )
    args = parser.parse_args()
    if args.self_test_output is not None and not args.self_test:
        parser.error("--self-test-output 只能与 --self-test 同时使用")
    if args.probe_output is not None and not args.live_readonly_probe:
        parser.error("--probe-output 只能与 --live-readonly-probe 同时使用")
    if args.self_test:
        return self_test(args.self_test_output)
    if args.live_readonly_probe:
        return live_readonly_probe(args.probe_output)
    if tk is None:
        raise SystemExit("当前 Python 没有 tkinter，无法启动桌面界面")
    ensure_app_dirs()
    instance_lock = SingleInstanceLock()
    if not instance_lock.acquire():
        raise SystemExit("本机已有聚水潭打单助手（可能是旧版本）正在运行，请先关闭")
    try:
        if legacy_assistant_window_exists():
            raise SystemExit(
                f"检测到仍在运行的旧版聚水潭打单助手窗口，请先关闭旧版后再启动 V{APP_VERSION}"
            )
        root = tk.Tk()
        try:
            DesktopApp(root, no_print=args.no_print)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"启动失败：{exc}")
            root.destroy()
            return 2
        root.mainloop()
    finally:
        instance_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
