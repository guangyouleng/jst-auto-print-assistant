#!/usr/bin/env python3
"""Read-only candidate planner for the JST shipping-label workflow.

This program deliberately contains no write/print/pre-shipment operation.  It
uses the existing authenticated JST OpenAPI client to find at most one safe
candidate and emits the browser/local-print steps that would be required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


WAREHOUSE_ID = "13673110"
WAREHOUSE_NAME = "山东汇馨仓库"
SOURCE_CARRIER_ID = "STO"
SOURCE_CARRIER_NAME = "申通E物流-山东"
TARGET_CARRIER_ID = "ZTO.1"
TARGET_CARRIER_NAME = "中通速递-山东"
PRIVACY_CARRIER_ID = "ZTO"
PRIVACY_CARRIER_NAME = "中通-天猫-隐私面单"
ALLOWED_CARRIERS = {
    SOURCE_CARRIER_ID: SOURCE_CARRIER_NAME,
    TARGET_CARRIER_ID: TARGET_CARRIER_NAME,
    PRIVACY_CARRIER_ID: PRIVACY_CARRIER_NAME,
}
PRIVACY_REMARK_TOKENS = ("隐私",)
PRIVACY_LABEL_TOKENS = ("紧急单",)
WEIGHT_THRESHOLD_KG = 3.0
KEEP_STO_SHOP_TOKENS = ("小红书", "红书", "视频号")
PLANNER_SCHEMA_VERSION = 5
MAX_LOOKBACK_HOURS = 720
MAX_ACTION_HISTORY_DAYS = 7
MAX_CREATED_CLOCK_SKEW_MINUTES = 5
MAX_CANDIDATE_POOL = 5000
CANDIDATE_CACHE_SCHEMA = 1
ORDER_ID_BATCH_SIZE = 10
MAX_INSPECT_BATCH_SIZE = 10
ORDER_ID_RE = re.compile(r"[0-9]{1,20}")
ORDER_QUERY_EXTRA_FIELDS = (
    "labels",
    "presend_status",
    "question_desc",
    "receiver_state",
    "receiver_city",
    "receiver_district",
)
DELIVERY_HOLD_TOKENS = ("停发", "停运", "暂停发货", "不可达", "不发货")

# These actions mean that repeating the workflow may create a duplicate label,
# or cross the user's explicit stop boundary.
# A request proves that repeating the click could duplicate a label, but it
# does not prove that the print service/printer actually completed the job.
# Keep request and completion as separate exact semantic categories.
PRINT_REQUEST_ACTION_MARKERS = ("请求打印快递单",)
PRINT_ACTION_MARKERS = ("打印快递单",)
WAYBILL_ACTION_MARKERS = ("获取电子面单", "设置快递单号")
SHIP_ACTION_MARKERS = (
    "预发货",
    "预发货成功",
    "直接发货",
    "线上发货",
    "检测到线上发货已经成功",
    "发货成功",
)
MANUAL_CARRIER_ACTION_MARKERS = ("手工指定快递", "手工设置快递", "人工指定快递")
AUDIT_STAGE_MANUAL_CARRIER_CONFIRM_ACTION = "强制审核"
AUDIT_STAGE_MANUAL_CARRIER_WINDOW_SECONDS = 60
ACTION_ACTOR_FIELDS = (
    "creator_id",
    "creator_name",
    "user_id",
    "user_name",
    "operator_id",
    "operator_name",
    "source",
    "source_name",
)
SAFE_ACTION_CATEGORIES = (
    ("PRINT_REQUEST", PRINT_REQUEST_ACTION_MARKERS),
    ("PRINT", PRINT_ACTION_MARKERS),
    ("WAYBILL", WAYBILL_ACTION_MARKERS),
    ("SHIP", SHIP_ACTION_MARKERS),
    ("MANUAL_CARRIER", MANUAL_CARRIER_ACTION_MARKERS),
)
EXACT_ACTION_CATEGORIES = {
    marker: category
    for category, markers in SAFE_ACTION_CATEGORIES
    for marker in markers
}
# Only the exact names above are accepted as successful business evidence.
# Any other action mentioning one of these domains is semantically unknown:
# it may be a cancellation, a failure, or a new JST action name whose meaning
# has not been reviewed.  Unknown related actions therefore make the history
# fail closed instead of being ignored or treated as a successful substring.
ACTION_RELATED_KEYWORDS = (
    "打印",
    "面单",
    "发货",
    "指定快递",
    "设置快递",
    "快递单号",
    "运单号",
)
# These are ordinary metadata/warehouse events observed in the authoritative
# JST action history.  Their names happen to contain ``发货`` but they do not
# prove that an order was printed, pre-shipped or shipped.  Keep this an exact
# allowlist: any new related action name still fails closed until reviewed.
BENIGN_RELATED_ACTIONS = frozenset(
    {
        "提交仓库发货",
        "订单预处理规则设发货仓",
        "修改发货仓库",
        "最晚发货时间变更",
        "设置发货仓库",
        "发货追加备注",
    }
)
SPLIT_ORDER_BLOCKER = "同一内部订单存在多个出库单，操作历史无法按出库单归属，禁止自动处理"
BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")

DEFAULT_BRIDGE_ROOT = ""


@dataclass
class Plan:
    o_id: str
    io_id: str
    outbound_identity_unique: bool
    weight_kg: float
    current_carrier: str
    current_carrier_id: str
    has_waybill: bool
    privacy_required: bool
    delivery_hold_marked: bool
    items_complete: bool
    source_item_count: int
    items: list[dict[str, Any]]
    state: str
    external_system_order: bool = False
    steps: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_order_id(value: Any) -> bool:
    return ORDER_ID_RE.fullmatch(_text(value)) is not None


def utc_now_text() -> str:
    """Return an unambiguous UTC timestamp for the HTTP freshness contract."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def business_now() -> datetime:
    """Return a naive JST business timestamp explicitly based on Shanghai."""

    return datetime.now(BUSINESS_TIMEZONE).replace(tzinfo=None)


def _weight(row: dict[str, Any]) -> float:
    for key in ("weight", "f_weight"):
        try:
            value = float(row.get(key) or 0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 0.0


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _text(value).lower() in {"1", "true", "yes", "y", "是", "已打印"}


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _classify_actions(values: Iterable[str]) -> tuple[set[str], bool]:
    """Return exact successful categories and whether semantics are unknown."""

    successful: set[str] = set()
    ambiguous = False
    for value in values:
        name = _text(value)
        if not name:
            continue
        category = EXACT_ACTION_CATEGORIES.get(name)
        if category is not None:
            successful.add(category)
        elif name in BENIGN_RELATED_ACTIONS:
            continue
        elif any(keyword in name for keyword in ACTION_RELATED_KEYWORDS):
            ambiguous = True
    return successful, ambiguous


def has_protected_manual_carrier_action(
    actions: Iterable[dict[str, Any]],
) -> bool:
    """Protect real manual overrides but not the carrier assigned by auditing.

    JST records the batch audit workflow itself as ``手工指定快递`` and then
    records ``强制审核`` a few seconds later.  Treat only that tightly bounded,
    ordered pair as the initial audit assignment.  When actor/source evidence
    is present it must also match exactly. A manual-carrier event with no
    parseable time, no following forced audit, outside the 60-second window,
    mismatched actor/source, or after auditing remains protected.
    """

    relevant: list[tuple[datetime | None, int, str, tuple[tuple[str, str], ...]]] = []
    for index, action in enumerate(actions):
        name = _text(action.get("name"))
        if name in MANUAL_CARRIER_ACTION_MARKERS:
            kind = "MANUAL"
        elif name == AUDIT_STAGE_MANUAL_CARRIER_CONFIRM_ACTION:
            kind = "AUDIT"
        else:
            continue
        relevant.append(
            (
                _parse_dt(
                    action.get("created")
                    or action.get("modified")
                    or action.get("time")
                ),
                index,
                kind,
                tuple(
                    (field, _text(action.get(field)))
                    for field in ACTION_ACTOR_FIELDS
                    if _text(action.get(field))
                ),
            )
        )
    if not any(kind == "MANUAL" for _time, _index, kind, _actor in relevant):
        return False
    if any(value is None for value, _index, _kind, _actor in relevant):
        return True
    ordered = sorted(
        (
            (value, index, kind, actor)
            for value, index, kind, actor in relevant
            if value is not None
        ),
        key=lambda item: (item[0], item[1]),
    )
    position = 0
    actorless_pairs = 0
    while position < len(ordered):
        event_time, _index, kind, manual_actor = ordered[position]
        if kind != "MANUAL":
            position += 1
            continue
        if position + 1 >= len(ordered):
            return True
        audit_time, _audit_index, audit_kind, audit_actor = ordered[position + 1]
        if (
            audit_kind != "AUDIT"
            or not 0
            <= (audit_time - event_time).total_seconds()
            <= AUDIT_STAGE_MANUAL_CARRIER_WINDOW_SECONDS
            or bool(manual_actor or audit_actor)
            and manual_actor != audit_actor
        ):
            return True
        if not manual_actor and not audit_actor:
            actorless_pairs += 1
            # Production read-only samples prove one actor-less pair is the
            # normal batch-audit signature.  They do not prove that a pair
            # surrounded by other manual/audit events is safe, so actor-less
            # evidence is accepted only when the entire relevant history is
            # exactly this one MANUAL -> AUDIT pair.
            if actorless_pairs > 1 or position != 0 or len(ordered) != 2:
                return True
        # Consume this audit once. Two manual assignments can never reuse one
        # later 强制审核 event as evidence that both came from the audit stage.
        position += 2
    return False


def keep_sto_for_platform(row: dict[str, Any]) -> bool:
    shop_name = _text(row.get("shop_name"))
    return any(token in shop_name for token in KEEP_STO_SHOP_TOKENS)


def privacy_required_from_remark(row: dict[str, Any]) -> bool:
    """Route privacy remarks and the explicit urgent-order label to privacy."""

    remark = _text(row.get("remark"))
    labels = _text(row.get("labels"))
    return any(token in remark for token in PRIVACY_REMARK_TOKENS) or any(
        token in labels for token in PRIVACY_LABEL_TOKENS
    )


def delivery_hold_reasons(row: dict[str, Any]) -> list[str]:
    """Detect authoritative manual suspension markers without exposing text."""

    values = (
        _text(row.get("remark")),
        _text(row.get("labels")),
        _text(row.get("tag")),
        _text(row.get("question_desc")),
    )
    markers = sorted({token for token in DELIVERY_HOLD_TOKENS if any(token in value for value in values)})
    if not markers:
        return []
    return [f"订单备注、标签或异常说明含停发标记：{'/'.join(markers)}"]


def normalize_items(
    row: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Validate every outbound line; never silently drop a malformed SKU line."""

    raw_items = row.get("items")
    if not isinstance(raw_items, list):
        return [], ["SKU明细字段不是列表"], 0
    if not raw_items:
        return [], ["SKU明细为空"], 0

    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_line_keys: set[str] = set()
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            errors.append(f"SKU第{index}行格式错误")
            continue
        line_key = _text(item.get("ioi_id"))
        product_id = _text(item.get("i_id"))
        sku_name = _text(item.get("name"))
        sku_id = _text(item.get("sku_id"))
        raw_qty = item.get("qty")
        try:
            qty = float(raw_qty)
        except (TypeError, ValueError):
            qty = 0.0
        valid_line_key = True
        if not line_key:
            errors.append(f"SKU第{index}行缺少明细行号")
            valid_line_key = False
        elif line_key in seen_line_keys:
            errors.append(f"SKU第{index}行明细行号重复")
            valid_line_key = False
        else:
            seen_line_keys.add(line_key)
        if not sku_name:
            errors.append(f"SKU第{index}行缺少SKU名称")
        if not sku_id:
            errors.append(f"SKU第{index}行缺少SKU编号")
        if not math.isfinite(qty) or qty <= 0:
            errors.append(f"SKU第{index}行数量必须大于0")
        if valid_line_key and sku_id and sku_name and math.isfinite(qty) and qty > 0:
            normalized.append(
                {
                    "line_key": line_key,
                    # i_id is JST's stable main-product identity.  Variants
                    # such as colours/sizes share it while retaining distinct
                    # sku_id values, so the executor can keep warehouse picking
                    # runs together without guessing from display names.
                    "product_id": product_id,
                    "sku_id": sku_id,
                    "sku_name": sku_name,
                    "qty": qty,
                    "unit": _text(item.get("unit")),
                }
            )
    return normalized, errors, len(raw_items)


def required_order_blockers(row: dict[str, Any]) -> list[str]:
    """Return fail-closed reasons for fields required by browser execution."""

    blockers: list[str] = []
    if not _is_order_id(row.get("o_id")):
        blockers.append("缺少有效内部订单号")
    if not _is_order_id(row.get("io_id")):
        blockers.append("缺少有效出库单号")
    if not _text(row.get("wms_co_id")):
        blockers.append("缺少仓库编号")
    if not _text(row.get("status")):
        blockers.append("缺少订单状态")
    if not _text(row.get("logistics_company")):
        blockers.append("缺少快递名称")
    if not _text(row.get("lc_id")):
        blockers.append("缺少快递编号")
    if _weight(row) <= 0:
        blockers.append("缺少有效订单重量")
    _, item_errors, _ = normalize_items(row)
    blockers.extend(item_errors)
    return blockers


def row_scope_state(row: dict[str, Any]) -> str:
    """Classify a JST row as OUT_OF_SCOPE, BLOCKED_INPUT, or CANDIDATE.

    Non-empty values that clearly belong to another warehouse, state or carrier
    are outside this automation.  Missing values on an otherwise matching row
    are surfaced as blocked input instead of disappearing from the report.
    """

    warehouse_id = _text(row.get("wms_co_id"))
    status = _text(row.get("status")).lower()
    carrier_name = _text(row.get("logistics_company"))
    carrier_id = _text(row.get("lc_id"))

    if warehouse_id and warehouse_id != WAREHOUSE_ID:
        return "OUT_OF_SCOPE"
    if not warehouse_id:
        if status == "waitconfirm" and (
            carrier_name in ALLOWED_CARRIERS.values()
            or carrier_id in ALLOWED_CARRIERS
        ):
            return "BLOCKED_INPUT"
        return "OUT_OF_SCOPE"
    if status and status != "waitconfirm":
        return "OUT_OF_SCOPE"
    if not status:
        return "BLOCKED_INPUT"
    if carrier_name and carrier_name not in ALLOWED_CARRIERS.values():
        return "OUT_OF_SCOPE"
    if carrier_id and carrier_id not in ALLOWED_CARRIERS:
        return "OUT_OF_SCOPE"
    if not carrier_name or not carrier_id:
        return "BLOCKED_INPUT"
    if ALLOWED_CARRIERS.get(carrier_id) != carrier_name:
        return "BLOCKED_INPUT"
    return "CANDIDATE"


def is_preliminary_candidate(row: dict[str, Any]) -> bool:
    return row_scope_state(row) == "CANDIDATE" and not required_order_blockers(row)


def desired_carrier(row: dict[str, Any]) -> tuple[str, str]:
    """Return the final carrier/paper profile required by business rules."""

    if privacy_required_from_remark(row):
        return PRIVACY_CARRIER_ID, PRIVACY_CARRIER_NAME
    if _weight(row) > WEIGHT_THRESHOLD_KG or keep_sto_for_platform(row):
        return SOURCE_CARRIER_ID, SOURCE_CARRIER_NAME
    return TARGET_CARRIER_ID, TARGET_CARRIER_NAME


def safety_blockers(
    row: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    action_history_complete: bool,
    split_outbound_order: bool = False,
) -> list[str]:
    """Return every non-destructive eligibility blocker for one order."""

    names = [_text(action.get("name")) for action in actions]
    successful_actions, action_semantics_ambiguous = _classify_actions(names)
    blockers = required_order_blockers(row)
    if row_scope_state(row) == "BLOCKED_INPUT":
        blockers.append("订单仓库、状态或快递字段不完整或不一致，禁止自动处理")
    blockers.extend(delivery_hold_reasons(row))
    if not action_history_complete or action_semantics_ambiguous:
        blockers.append("操作历史未完整覆盖，禁止自动处理")
    if split_outbound_order:
        blockers.append(SPLIT_ORDER_BLOCKER)
    if "SHIP" in successful_actions:
        blockers.append("已有发货或预发货动作，禁止重复处理")
    if "PRINT" in successful_actions or "PRINT_REQUEST" in successful_actions:
        blockers.append("已有打印动作，禁止重复打印")
    if _flag(row.get("is_print_express")):
        blockers.append("聚水潭已标记打印，需人工核查，禁止重复打印")

    current_carrier_id = _text(row.get("lc_id"))
    reset_required = current_carrier_id != desired_carrier(row)[0]
    has_waybill = bool(_text(row.get("l_id")))
    if reset_required and has_protected_manual_carrier_action(actions):
        blockers.append("审单后已有手工指定快递，禁止自动覆盖人工决定")
    if reset_required and has_waybill:
        blockers.append("已有运单号，重设快递前需人工确认旧面单处置")
    return blockers


def action_history_independent_blocked(row: dict[str, Any]) -> bool:
    """Return whether row fields alone already prove the order is blocked.

    Action history remains mandatory for every order that could become READY.
    These cases cannot become READY regardless of the action response, so
    skipping their expensive history read changes no executable candidate.
    """

    if row_scope_state(row) != "CANDIDATE":
        return True
    if delivery_hold_reasons(row) or _flag(row.get("is_print_express")):
        return True
    reset_required = _text(row.get("lc_id")) != desired_carrier(row)[0]
    return reset_required and bool(_text(row.get("l_id")))


def make_plan(
    row: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    action_history_complete: bool,
    split_outbound_order: bool = False,
) -> Plan:
    weight = _weight(row)
    current_carrier_id = _text(row.get("lc_id"))
    current_carrier_name = _text(row.get("logistics_company"))
    items, item_errors, source_item_count = normalize_items(row)
    # The current outbound-order field is the only authoritative proof that a
    # usable waybill still exists.  An old "获取电子面单" action may remain after
    # the number was cancelled/cleared; treating that history as a live
    # waybill would incorrectly skip GET_WAYBILL and pause at print time.
    has_waybill = bool(_text(row.get("l_id")))
    privacy_required = privacy_required_from_remark(row)
    hold_blockers = delivery_hold_reasons(row)
    blockers = safety_blockers(
        row,
        actions,
        action_history_complete=action_history_complete,
        split_outbound_order=split_outbound_order,
    )

    final_carrier = desired_carrier(row)
    reset_target_pair: tuple[str, str] | None = None
    if current_carrier_id != final_carrier[0]:
        reset_target_pair = final_carrier

    if blockers:
        state = "BLOCKED"
        steps: list[str] = []
    elif reset_target_pair is not None:
        target_id, target_name = reset_target_pair
        state = "READY_HYBRID"
        steps = [
            f"RESET_CARRIER_AND_GET_WAYBILL:{target_id}:{target_name}",
            "PRINT_EXPRESS",
            "STOP_BEFORE_PRESHIP",
        ]
    elif has_waybill:
        state = "READY_HYBRID"
        steps = ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"]
    else:
        state = "READY_HYBRID"
        steps = ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"]

    return Plan(
        o_id=_text(row.get("o_id")),
        io_id=_text(row.get("io_id")),
        # JST's order-action API has only o_id and the current print table may
        # not render io_id.  A browser is allowed to use a unique o_id row only
        # when this planner has proved that the complete scan contains no
        # sibling outbound io_id for that order.
        outbound_identity_unique=not split_outbound_order,
        weight_kg=weight,
        current_carrier=current_carrier_name,
        current_carrier_id=current_carrier_id,
        has_waybill=has_waybill,
        privacy_required=privacy_required,
        external_system_order="外部系统订单" in _text(row.get("labels")),
        delivery_hold_marked=bool(hold_blockers),
        items_complete=not item_errors and len(items) == source_item_count,
        source_item_count=source_item_count,
        items=items,
        state=state,
        steps=steps,
        blockers=blockers,
    )


def _page_all(client: Any, method_name: str, **kwargs: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    method = getattr(client, method_name)
    for page in range(1, 1001):
        response = None
        for attempt in range(5):
            try:
                response = method(page_no=page, page_size=100, **kwargs)
                break
            except Exception as exc:
                if _text(getattr(exc, "code", "")) != "199" or attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        assert response is not None
        rows.extend(response.get("datas") or [])
        if not response.get("has_next"):
            break
        time.sleep(0.35)
    else:
        raise RuntimeError(f"{method_name} exceeded the 1000-page safety limit")
    return rows


def _save_candidate_cache(
    path: Path, scanned_at: datetime, order_ids: Iterable[str]
) -> None:
    payload = {
        "schema_version": CANDIDATE_CACHE_SCHEMA,
        "last_scan": scanned_at.strftime("%Y-%m-%d %H:%M:%S"),
        "o_ids": sorted({_text(value) for value in order_ids if _is_order_id(value)}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _load_candidate_cache(path: Path, begin: datetime, now: datetime) -> tuple[datetime, set[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("候选索引缺失或损坏，必须先执行安全全量建索引") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != CANDIDATE_CACHE_SCHEMA:
        raise RuntimeError("候选索引版本不兼容，必须先执行安全全量建索引")
    scanned_at = _parse_dt(payload.get("last_scan"))
    raw_ids = payload.get("o_ids")
    if (
        scanned_at is None
        or scanned_at < begin
        or scanned_at > now + timedelta(minutes=5)
        or not isinstance(raw_ids, list)
        or any(not isinstance(value, str) or not _is_order_id(value) for value in raw_ids)
    ):
        raise RuntimeError("候选索引已过期或格式错误，必须先执行安全全量建索引")
    return scanned_at, set(raw_ids)


def _deduplicate_orders(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    fingerprints: dict[tuple[str, str], tuple[Any, ...]] = {}
    malformed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (_text(row.get("o_id")), _text(row.get("io_id")))
        if _is_order_id(key[0]) and _is_order_id(key[1]):
            items, item_errors, source_count = normalize_items(row)
            fingerprint = (
                _text(row.get("wms_co_id")),
                _text(row.get("status")),
                _text(row.get("shop_name")),
                _text(row.get("lc_id")),
                _text(row.get("logistics_company")),
                _weight(row),
                _text(row.get("l_id")),
                _flag(row.get("is_print_express")),
                privacy_required_from_remark(row),
                tuple(delivery_hold_reasons(row)),
                source_count,
                tuple(item_errors),
                tuple(
                    sorted(
                        json.dumps(item, ensure_ascii=False, sort_keys=True)
                        for item in items
                    )
                ),
            )
            previous = fingerprints.get(key)
            if previous is not None and previous != fingerprint:
                raise RuntimeError(
                    f"聚水潭重复返回的订单 {key[0]}/{key[1]} 安全字段互相冲突"
                )
            unique.setdefault(key, row)
            fingerprints[key] = fingerprint
        else:
            malformed.append(row)
    return list(unique.values()) + malformed


def _query_exact_order_ids(
    client: Any, order_ids: Iterable[str]
) -> list[dict[str, Any]]:
    """Read every outbound sibling for exact o_ids without a modified window."""

    exact: list[dict[str, Any]] = []
    ordered_ids = sorted({_text(value) for value in order_ids if _is_order_id(value)})
    for offset in range(0, len(ordered_ids), ORDER_ID_BATCH_SIZE):
        batch = ordered_ids[offset : offset + ORDER_ID_BATCH_SIZE]
        batch_set = set(batch)
        response_rows = _page_all(
            client,
            "query_orders_out",
            o_ids=[int(value) for value in batch],
            inout_flds=list(ORDER_QUERY_EXTRA_FIELDS),
        )
        if any(
            not isinstance(row, dict)
            or _text(row.get("o_id")) not in batch_set
            for row in response_rows
        ):
            raise RuntimeError("聚水潭精确订单查询返回了批次外订单")
        exact.extend(response_rows)
    return exact


def _refresh_discovered_identities(
    client: Any, discovery_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace potentially window-truncated candidates with exact identities."""

    candidate_ids = {
        _text(row.get("o_id"))
        for row in discovery_rows
        if isinstance(row, dict)
        and row_scope_state(row) != "OUT_OF_SCOPE"
        and _is_order_id(row.get("o_id"))
    }
    if not candidate_ids:
        return _deduplicate_orders(discovery_rows)
    exact = _query_exact_order_ids(client, candidate_ids)
    returned_ids = {
        _text(row.get("o_id")) for row in exact if isinstance(row, dict)
    }
    if not candidate_ids.issubset(returned_ids):
        raise RuntimeError("聚水潭精确订单查询缺少刚发现的订单，禁止使用不完整身份")
    untouched = [
        row
        for row in discovery_rows
        if not isinstance(row, dict) or _text(row.get("o_id")) not in candidate_ids
    ]
    return _deduplicate_orders(exact + untouched)


def discover_orders(
    client: Any,
    *,
    begin: datetime,
    now: datetime,
    candidate_cache: str,
) -> list[dict[str, Any]]:
    """Refresh a small persistent candidate index and return exact live rows."""

    if not candidate_cache:
        return _refresh_discovered_identities(
            client,
            _page_all(
                client,
                "query_orders_out",
                modified_begin=begin.strftime("%Y-%m-%d %H:%M:%S"),
                modified_end=now.strftime("%Y-%m-%d %H:%M:%S"),
                inout_flds=list(ORDER_QUERY_EXTRA_FIELDS),
            ),
        )
    cache_path = Path(candidate_cache)
    try:
        cache_path.lstat()
    except FileNotFoundError:
        # A clean deployment has no identity index yet.  Bootstrap it from one
        # authoritative read-only scan, then continue this same plan from the
        # complete result.  Existing-but-unreadable/corrupt indexes still fail
        # closed in _load_candidate_cache rather than being silently replaced.
        rows = _refresh_discovered_identities(
            client,
            _page_all(
                client,
                "query_orders_out",
                modified_begin=begin.strftime("%Y-%m-%d %H:%M:%S"),
                modified_end=now.strftime("%Y-%m-%d %H:%M:%S"),
                inout_flds=list(ORDER_QUERY_EXTRA_FIELDS),
            ),
        )
        initial_ids = {
            _text(row.get("o_id"))
            for row in rows
            if row_scope_state(row) != "OUT_OF_SCOPE"
            and _is_order_id(row.get("o_id"))
        }
        _save_candidate_cache(cache_path, now, initial_ids)
        return rows
    except OSError as exc:
        raise RuntimeError("候选索引无法访问，禁止自动覆盖") from exc
    last_scan, cached_ids = _load_candidate_cache(cache_path, begin, now)
    incremental_begin = max(begin, last_scan - timedelta(minutes=10))
    recent = _page_all(
        client,
        "query_orders_out",
        modified_begin=incremental_begin.strftime("%Y-%m-%d %H:%M:%S"),
        modified_end=now.strftime("%Y-%m-%d %H:%M:%S"),
        inout_flds=list(ORDER_QUERY_EXTRA_FIELDS),
    )
    refresh_ids = cached_ids | {
        _text(row.get("o_id"))
        for row in recent
        if isinstance(row, dict) and _is_order_id(row.get("o_id"))
    }
    exact = _query_exact_order_ids(client, refresh_ids)
    rows = _deduplicate_orders(
        exact
        + [
            row
            for row in recent
            if not _is_order_id(row.get("o_id"))
            or not _is_order_id(row.get("io_id"))
        ]
    )
    next_ids = {
        _text(row.get("o_id"))
        for row in rows
        if row_scope_state(row) != "OUT_OF_SCOPE"
        and _is_order_id(row.get("o_id"))
    }
    _save_candidate_cache(cache_path, now, next_ids)
    return rows


def query_actions(
    client: Any,
    o_id: str,
    created: datetime | None,
    now: datetime,
    history_days: int,
) -> tuple[list[dict[str, Any]], bool]:
    floor = now - timedelta(days=history_days)
    if created is None or created > now + timedelta(
        minutes=MAX_CREATED_CLOCK_SKEW_MINUTES
    ):
        return [], False
    complete = created >= floor
    cursor = max(created - timedelta(hours=1), floor)
    rows: list[dict[str, Any]] = []

    # JST rejects action-query windows of one day or more, so use 23-hour slices.
    while cursor < now:
        end = min(cursor + timedelta(hours=23), now)
        rows.extend(
            _page_all(
                client,
                "query_order_action",
                modified_begin=cursor.strftime("%Y-%m-%d %H:%M:%S"),
                modified_end=end.strftime("%Y-%m-%d %H:%M:%S"),
                o_ids=[int(o_id)],
            )
        )
        cursor = end + timedelta(seconds=1)
    # The JST action API exposes o_id but no io_id.  Never accept a response
    # containing another order, and let callers block split outbound orders
    # rather than guessing which io_id owns an action.
    if any(
        not isinstance(row, dict) or _text(row.get("o_id")) != str(o_id)
        for row in rows
    ):
        return [], False
    return rows, complete


def query_actions_bulk(
    client: Any,
    rows: list[dict[str, Any]],
    now: datetime,
    history_days: int,
) -> dict[str, tuple[list[dict[str, Any]], bool]]:
    """Fetch complete action history for up to ten order ids per API request."""

    floor = now - timedelta(days=history_days)
    result: dict[str, tuple[list[dict[str, Any]], bool]] = {}
    eligible: list[tuple[datetime, str]] = []
    for row in rows:
        o_id = _text(row.get("o_id"))
        created = _parse_dt(row.get("created"))
        if (
            not _is_order_id(o_id)
            or created is None
            or created < floor
            or created > now + timedelta(minutes=MAX_CREATED_CLOCK_SKEW_MINUTES)
        ):
            result[o_id] = ([], False)
        else:
            eligible.append((created, o_id))
    # Group similar creation times together so each ten-order request scans
    # only the windows actually needed by that group.
    eligible = sorted(set(eligible))
    for offset in range(0, len(eligible), ORDER_ID_BATCH_SIZE):
        group = eligible[offset : offset + ORDER_ID_BATCH_SIZE]
        group_ids = {o_id for _created, o_id in group}
        actions_by_id = {o_id: [] for o_id in group_ids}
        complete = True
        cursor = max(min(created for created, _o_id in group) - timedelta(hours=1), floor)
        while cursor < now:
            end = min(cursor + timedelta(hours=23), now)
            action_rows = _page_all(
                client,
                "query_order_action",
                modified_begin=cursor.strftime("%Y-%m-%d %H:%M:%S"),
                modified_end=end.strftime("%Y-%m-%d %H:%M:%S"),
                o_ids=[int(value) for value in sorted(group_ids)],
            )
            if any(
                not isinstance(action, dict)
                or _text(action.get("o_id")) not in group_ids
                for action in action_rows
            ):
                complete = False
                break
            for action in action_rows:
                actions_by_id[_text(action.get("o_id"))].append(action)
            cursor = end + timedelta(seconds=1)
        for _created, o_id in group:
            result[o_id] = (actions_by_id[o_id] if complete else [], complete)
    return result


def split_order_ids(rows: Iterable[dict[str, Any]]) -> set[str]:
    """Return internal orders that map to multiple outbound order ids."""

    outbound_by_order: dict[str, set[str]] = {}
    for row in rows:
        o_id = _text(row.get("o_id"))
        io_id = _text(row.get("io_id"))
        if _is_order_id(o_id) and _is_order_id(io_id):
            outbound_by_order.setdefault(o_id, set()).add(io_id)
    return {
        o_id for o_id, io_ids in outbound_by_order.items() if len(io_ids) > 1
    }


def run_live_readonly(args: argparse.Namespace, *, client: Any = None) -> dict[str, Any]:
    if client is None:
        client = _load_jst_client(args.bridge_root)
    # JST business windows always use Asia/Shanghai.
    current_business_time = business_now()
    begin = current_business_time - timedelta(hours=args.lookback_hours)
    rows = discover_orders(
        client,
        begin=begin,
        now=current_business_time,
        candidate_cache=args.candidate_cache,
    )
    relevant = sorted(
        (row for row in rows if row_scope_state(row) != "OUT_OF_SCOPE"),
        key=lambda row: (_text(row.get("created")), _text(row.get("o_id"))),
    )
    # Detect split identities across the complete query result, not only the
    # rows still in scope.  A shipped/out-of-scope sibling still makes o_id-only
    # action history and browser matching ambiguous for the remaining row.
    split_orders = split_order_ids(rows)
    action_query_rows = [
        row
        for row in relevant
        if is_preliminary_candidate(row)
        and _text(row.get("o_id")) not in split_orders
        and not action_history_independent_blocked(row)
    ]
    action_proofs = query_actions_bulk(
        client, action_query_rows, current_business_time, args.action_history_days
    )

    plans: list[Plan] = []
    for row in relevant:
        structural_blockers = required_order_blockers(row)
        o_id = _text(row.get("o_id"))
        is_split = o_id in split_orders
        independently_blocked = action_history_independent_blocked(row)
        if structural_blockers or is_split or independently_blocked:
            # The row is already fail-closed.  Avoid a misleading API failure
            # caused by trying to query actions with an absent/ambiguous identity.
            actions, complete = [], not is_split
        else:
            actions, complete = action_proofs.get(o_id, ([], False))
        plans.append(
            make_plan(
                row,
                actions,
                action_history_complete=complete,
                split_outbound_order=is_split,
            )
        )

    ready = [plan for plan in plans if plan.state == "READY_HYBRID"]
    privacy_ready = [plan for plan in ready if plan.privacy_required]
    selected = ready[: args.max_candidates]
    candidate_pool_complete = len(ready) <= args.max_candidates
    blocked_reason_counts = Counter(
        reason
        for plan in plans
        if plan.state == "BLOCKED"
        for reason in plan.blockers
    )
    return {
        "mode": "LIVE_READ_ONLY_SHADOW_V5",
        "schema_version": PLANNER_SCHEMA_VERSION,
        "generated_at": utc_now_text(),
        "candidate_pool_complete": candidate_pool_complete,
        "scope": {
            "warehouse_id": WAREHOUSE_ID,
            "warehouse_name": WAREHOUSE_NAME,
            "source_carrier_id": SOURCE_CARRIER_ID,
            "source_carrier_name": SOURCE_CARRIER_NAME,
            "allowed_current_carriers": dict(ALLOWED_CARRIERS),
            "target_carrier_id_for_le_3kg": TARGET_CARRIER_ID,
            "target_carrier_name_for_le_3kg": TARGET_CARRIER_NAME,
            "target_carrier_id_for_gt_3kg": SOURCE_CARRIER_ID,
            "target_carrier_name_for_gt_3kg": SOURCE_CARRIER_NAME,
            "privacy_source_field": "remark",
            "privacy_remark_token": "隐私",
            "privacy_remark_tokens": list(PRIVACY_REMARK_TOKENS),
            "privacy_label_tokens": list(PRIVACY_LABEL_TOKENS),
            "privacy_rule_source_fields": ["remark", "labels"],
            "privacy_target_carrier_id": PRIVACY_CARRIER_ID,
            "privacy_target_carrier_name": PRIVACY_CARRIER_NAME,
            "weight_threshold_kg": WEIGHT_THRESHOLD_KG,
            "le_3kg_keep_sto_shop_tokens": list(KEEP_STO_SHOP_TOKENS),
            "lookback_hours": args.lookback_hours,
            "max_candidates": args.max_candidates,
        },
        "counts": {
            "orders_read": len(rows),
            "in_scope": len(relevant),
            "preliminary": sum(
                1 for row in relevant if is_preliminary_candidate(row)
            ),
            "ready": len(ready),
            "privacy_ready": len(privacy_ready),
            "blocked": len(plans) - len(ready),
            "selected": len(selected),
        },
        "selected": [asdict(plan) for plan in selected],
        "privacy_ready_preview": [
            {
                "o_id": plan.o_id,
                "io_id": plan.io_id,
                "weight_kg": plan.weight_kg,
                "privacy_required": True,
            }
            for plan in privacy_ready[:20]
        ],
        "blocked_preview": [
            {
                "o_id": plan.o_id,
                "io_id": plan.io_id,
                "identity_complete": _is_order_id(plan.o_id)
                and _is_order_id(plan.io_id),
                "weight_kg": plan.weight_kg,
                "items_complete": plan.items_complete,
                "blockers": plan.blockers,
            }
            for plan in plans
            if plan.state == "BLOCKED"
        ][:50],
        "blocked_reason_counts": dict(blocked_reason_counts),
        "guardrails": [
            "No business write is implemented in this program",
            "Never call pre-shipment or direct-shipment operations",
            "Never rely on is_print_express alone",
            "Never overwrite a manually assigned carrier",
            "Route non-privacy orders <=3kg to ZTO and >3kg to STO",
            "Keep STO for Xiaohongshu/WeChat Channels orders",
            "Use privacy carrier when remark contains 隐私 or labels contain 紧急单",
            "At most max_candidates orders may advance to a future executor",
            "The candidate pool must be complete; truncation is fail-closed",
            "Buyer, address, phone and full waybill values are never emitted",
        ],
    }


def run_seed_candidate_cache(args: argparse.Namespace) -> dict[str, Any]:
    """Build the persistent identity-only index without action-history reads."""

    if not args.candidate_cache:
        raise RuntimeError("--seed-candidate-cache requires --candidate-cache")
    client = _load_jst_client(args.bridge_root)

    current_business_time = business_now()
    begin = current_business_time - timedelta(hours=args.lookback_hours)
    rows = _page_all(
        client,
        "query_orders_out",
        modified_begin=begin.strftime("%Y-%m-%d %H:%M:%S"),
        modified_end=current_business_time.strftime("%Y-%m-%d %H:%M:%S"),
        inout_flds=list(ORDER_QUERY_EXTRA_FIELDS),
    )
    order_ids = {
        _text(row.get("o_id"))
        for row in rows
        if row_scope_state(row) != "OUT_OF_SCOPE"
        and _is_order_id(row.get("o_id"))
    }
    _save_candidate_cache(Path(args.candidate_cache), current_business_time, order_ids)
    return {
        "mode": "CANDIDATE_CACHE_SEED_V1",
        "generated_at": utc_now_text(),
        "orders_read": len(rows),
        "candidate_order_ids": len(order_ids),
    }


def _not_found_inspect_result(
    o_id: str, io_id: str, *, generated_at: str
) -> dict[str, Any]:
    return {
        "mode": "ORDER_READBACK_V5",
        "schema_version": PLANNER_SCHEMA_VERSION,
        "generated_at": generated_at,
        "found": False,
        "o_id": o_id,
        "io_id": io_id,
    }


def _found_inspect_result(
    row: dict[str, Any],
    actions: list[dict[str, Any]],
    action_history_complete: bool,
    *,
    generated_at: str,
) -> dict[str, Any]:
    raw_names = [_text(action.get("name")) for action in actions]
    successful_actions, action_semantics_ambiguous = _classify_actions(raw_names)
    action_items: list[dict[str, str]] = []
    for action in actions:
        raw_name = _text(action.get("name"))
        parsed_time = _parse_dt(
            action.get("created") or action.get("modified") or action.get("time")
        )
        safe_time = parsed_time.strftime("%Y-%m-%d %H:%M:%S") if parsed_time else ""
        category = EXACT_ACTION_CATEGORIES.get(raw_name)
        if category is not None:
            action_items.append({"name": category, "time": safe_time})
    waybill = _text(row.get("l_id"))
    item_rows, item_errors, source_item_count = normalize_items(row)
    hold_reasons = delivery_hold_reasons(row)
    return {
        "mode": "ORDER_READBACK_V5",
        "schema_version": PLANNER_SCHEMA_VERSION,
        "generated_at": generated_at,
        "found": True,
        "o_id": _text(row.get("o_id")),
        "io_id": _text(row.get("io_id")),
        "warehouse_id": _text(row.get("wms_co_id")),
        "status": _text(row.get("status")),
        "weight_kg": _weight(row),
        "io_date": _text(row.get("io_date")),
        "shop_name": _text(row.get("shop_name")),
        "carrier_id": _text(row.get("lc_id")),
        "carrier_name": _text(row.get("logistics_company")),
        "has_waybill": bool(waybill),
        "waybill_suffix": waybill[-4:] if waybill else "",
        "waybill_fingerprint": (
            hashlib.sha256(waybill.encode("utf-8")).hexdigest()
            if waybill
            else ""
        ),
        "is_print_express": _flag(row.get("is_print_express")),
        "action_history_complete": action_history_complete
        and not action_semantics_ambiguous,
        "actions": action_items,
        "has_print_request": "PRINT_REQUEST" in successful_actions,
        "has_print_action": "PRINT" in successful_actions,
        "has_ship_action": "SHIP" in successful_actions,
        # This compatibility field means a manual override that must still be
        # protected.  The initial batch-audit assignment is intentionally not
        # reported as an override.
        "has_manual_carrier_action": has_protected_manual_carrier_action(actions),
        "external_system_order": "外部系统订单" in _text(row.get("labels")),
        "privacy_required": privacy_required_from_remark(row),
        "privacy_source": "remark",
        "privacy_review_required": False,
        "delivery_hold_marked": bool(hold_reasons),
        "delivery_hold_reasons": hold_reasons,
        "items_complete": not item_errors and len(item_rows) == source_item_count,
        "source_item_count": source_item_count,
        "item_validation_errors": item_errors,
        "order_validation_errors": required_order_blockers(row),
        "items": item_rows,
        "redaction": "buyer, address, phone and full waybill are omitted",
    }


def _load_jst_client(bridge_root_value: str) -> Any:
    if not bridge_root_value:
        from jst_openapi import JSTReadonlyClient
        return JSTReadonlyClient(Path.home() / ".jst-auto-print" / "jst_openapi_config.json")
    bridge_root = Path(bridge_root_value)
    if not (bridge_root / "app" / "clients" / "jst_client.py").exists():
        raise RuntimeError(f"JST client not found under {bridge_root}")
    sys.path.insert(0, str(bridge_root))
    from app.clients.jst_client import JSTClient  # type: ignore

    return JSTClient()


def run_inspect_order(args: argparse.Namespace, *, client: Any = None) -> dict[str, Any]:
    """Read back one exact internal/outbound pair without exposing PII."""
    current_business_time = business_now()
    if client is None:
        client = _load_jst_client(args.bridge_root)
    rows = _page_all(
        client,
        "query_orders_out",
        o_ids=[int(args.inspect_o_id)],
        inout_flds=list(ORDER_QUERY_EXTRA_FIELDS),
    )
    exact = [
        row
        for row in rows
        if _text(row.get("o_id")) == args.inspect_o_id
        and _text(row.get("io_id")) == args.inspect_io_id
    ]
    if not exact:
        return _not_found_inspect_result(
            args.inspect_o_id,
            args.inspect_io_id,
            generated_at=utc_now_text(),
        )
    if len(exact) != 1:
        raise RuntimeError("exact o_id/io_id pair is not unique")

    row = exact[0]
    is_split = args.inspect_o_id in split_order_ids(rows)
    if is_split:
        # Action rows have no io_id, so reusing all o_id actions for one split
        # outbound order would create false print/shipment state.
        actions, complete = [], False
    else:
        actions, complete = query_actions(
            client,
            args.inspect_o_id,
            _parse_dt(row.get("created")),
            current_business_time,
            args.action_history_days,
        )
    return _found_inspect_result(
        row, actions, complete, generated_at=utc_now_text()
    )


def run_inspect_batch(args: argparse.Namespace, *, client: Any = None) -> dict[str, Any]:
    """Read up to ten exact pairs with one order query and one bulk action pass."""

    requested_pairs = list(args.inspect_pairs or [])
    if not 1 <= len(requested_pairs) <= MAX_INSPECT_BATCH_SIZE:
        raise RuntimeError("batch inspect requires between one and ten pairs")
    requested_o_ids = {o_id for o_id, _io_id in requested_pairs}
    current_business_time = business_now()
    if client is None:
        client = _load_jst_client(args.bridge_root)
    rows = _page_all(
        client,
        "query_orders_out",
        o_ids=[int(value) for value in sorted(requested_o_ids)],
        inout_flds=list(ORDER_QUERY_EXTRA_FIELDS),
    )
    if any(
        not isinstance(row, dict) or _text(row.get("o_id")) not in requested_o_ids
        for row in rows
    ):
        raise RuntimeError("batch exact order query returned an unrequested order")

    exact_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {
        pair: [] for pair in requested_pairs
    }
    for row in rows:
        pair = (_text(row.get("o_id")), _text(row.get("io_id")))
        if pair in exact_by_pair:
            exact_by_pair[pair].append(row)
    for pair, exact in exact_by_pair.items():
        if len(exact) > 1:
            raise RuntimeError(
                f"exact o_id/io_id pair is not unique: {pair[0]}/{pair[1]}"
            )

    split_orders = split_order_ids(rows)
    action_rows = [
        exact[0]
        for pair, exact in exact_by_pair.items()
        if exact and pair[0] not in split_orders
    ]
    action_proofs = query_actions_bulk(
        client, action_rows, current_business_time, args.action_history_days
    )
    generated_at = utc_now_text()
    results: list[dict[str, Any]] = []
    for o_id, io_id in requested_pairs:
        exact = exact_by_pair[(o_id, io_id)]
        if not exact:
            results.append(
                _not_found_inspect_result(
                    o_id, io_id, generated_at=generated_at
                )
            )
            continue
        row = exact[0]
        if o_id in split_orders:
            actions, complete = [], False
        else:
            actions, complete = action_proofs.get(o_id, ([], False))
        results.append(
            _found_inspect_result(
                row,
                actions,
                complete,
                generated_at=generated_at,
            )
        )
    return {
        "mode": "ORDER_BATCH_READBACK_V5",
        "schema_version": PLANNER_SCHEMA_VERSION,
        "generated_at": generated_at,
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--live-readonly",
        action="store_true",
        help="read live JST data; still performs no write or print",
    )
    mode.add_argument(
        "--inspect-o-id",
        help="read back one internal JST order id; still performs no write or print",
    )
    mode.add_argument(
        "--inspect-pair",
        action="append",
        dest="inspect_pair_values",
        metavar="O_ID:IO_ID",
        help="read back 1-10 exact pairs in one request; may be repeated",
    )
    mode.add_argument(
        "--seed-candidate-cache",
        action="store_true",
        help="perform one full read-only scan and seed the identity-only cache",
    )
    parser.add_argument(
        "--inspect-io-id",
        help="exact outbound order id paired with --inspect-o-id",
    )
    parser.add_argument("--lookback-hours", type=int, default=48)
    parser.add_argument("--action-history-days", type=int, default=7)
    parser.add_argument("--max-candidates", type=int, default=10)
    parser.add_argument("--bridge-root", default=str(DEFAULT_BRIDGE_ROOT))
    parser.add_argument("--candidate-cache", default="")
    args = parser.parse_args()
    if not 1 <= args.max_candidates <= MAX_CANDIDATE_POOL:
        parser.error(
            f"--max-candidates must be between 1 and {MAX_CANDIDATE_POOL}"
        )
    if not 1 <= args.lookback_hours <= MAX_LOOKBACK_HOURS:
        parser.error(
            f"--lookback-hours must be between 1 and {MAX_LOOKBACK_HOURS}"
        )
    if not 1 <= args.action_history_days <= MAX_ACTION_HISTORY_DAYS:
        parser.error(
            "--action-history-days must be between 1 and "
            f"{MAX_ACTION_HISTORY_DAYS}"
        )
    if args.inspect_o_id and not _is_order_id(args.inspect_o_id):
        parser.error("--inspect-o-id must contain digits only")
    if args.inspect_o_id and (
        not args.inspect_io_id or not _is_order_id(args.inspect_io_id)
    ):
        parser.error("--inspect-io-id must contain digits and accompany --inspect-o-id")
    if args.inspect_io_id and not args.inspect_o_id:
        parser.error("--inspect-io-id requires --inspect-o-id")
    inspect_pairs: list[tuple[str, str]] = []
    for raw_pair in args.inspect_pair_values or []:
        parts = raw_pair.split(":")
        if (
            len(parts) != 2
            or not _is_order_id(parts[0])
            or not _is_order_id(parts[1])
        ):
            parser.error("--inspect-pair must be O_ID:IO_ID using digits only")
        inspect_pairs.append((parts[0], parts[1]))
    if args.inspect_pair_values and not 1 <= len(inspect_pairs) <= MAX_INSPECT_BATCH_SIZE:
        parser.error("--inspect-pair may be repeated between 1 and 10 times")
    if len(set(inspect_pairs)) != len(inspect_pairs):
        parser.error("--inspect-pair identities must be unique")
    args.inspect_pairs = inspect_pairs
    return args


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.inspect_o_id:
        result = run_inspect_order(parsed)
    elif parsed.inspect_pairs:
        result = run_inspect_batch(parsed)
    elif parsed.seed_candidate_cache:
        result = run_seed_candidate_cache(parsed)
    else:
        result = run_live_readonly(parsed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
