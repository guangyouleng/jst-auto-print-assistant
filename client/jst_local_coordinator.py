"""In-process single-PC coordinator; no listener, HTTP service or subprocess.

Business validation is migrated from the schema-5 coordinator. Each request
receives an explicit local store and read-only source. Automatic completion
always performs a fresh readback; no server proof cache is required.
"""
from __future__ import annotations
import copy
import math
import re
from datetime import datetime
from typing import Any, Optional
from jst_local_store import Lease, LeaseConflict, LeaseStore, utc_text
PLAN_POOL_SIZE = 5000
INSPECT_TIMEOUT_SECONDS = 35
ACTION_HISTORY_DAYS = 7


MAX_EXCLUDES = 200

MAX_BATCH_CANDIDATES = 10

TARGET_WAREHOUSE_ID = "13673110"

SOURCE_CARRIER_ID = "STO"

TARGET_CARRIER_ID = "ZTO.1"

PRIVACY_CARRIER_ID = "ZTO"

PRINT_PROFILES = {SOURCE_CARRIER_ID, TARGET_CARRIER_ID, PRIVACY_CARRIER_ID}

PRINT_PROFILE_CARRIERS = {
    SOURCE_CARRIER_ID: "申通E物流-山东",
    TARGET_CARRIER_ID: "中通速递-山东",
    PRIVACY_CARRIER_ID: "中通-天猫-隐私面单",
}

API_SCHEMA_VERSION = 5

PLANNER_SCHEMA_VERSION = 5

PLAN_MODE = "LIVE_READ_ONLY_CLAIMED_V1"

INSPECT_MODE = "ORDER_READBACK_CLAIMED_V1"

INSPECT_BATCH_MODE = "ORDER_BATCH_READBACK_CLAIMED_V1"

RAW_PLAN_MODE = "LIVE_READ_ONLY_SHADOW_V5"

RAW_INSPECT_MODE = "ORDER_READBACK_V5"

RAW_INSPECT_BATCH_MODE = "ORDER_BATCH_READBACK_V5"

WORKSTATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")

ORDER_ID_RE = re.compile(r"[0-9]{1,20}")

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")

MIN_CLIENT_VERSION = "0.5.21"

COMPLETION_REASONS = frozenset(
    {"PRINTED", "TERMINAL", "OPERATOR_SKIPPED", "UNCERTAIN_ACTION"}
)

AUTOMATIC_COMPLETION_REASONS = frozenset({"PRINTED", "TERMINAL"})

TERMINAL_ORDER_STATUSES = frozenset({"sent", "delete"})

def _natural_sort_key(value: str) -> tuple[tuple[int, object], ...]:
    """Numeric-aware sort key so 'A2' sorts before 'A10'."""

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", str(value))
    )

class APIError(RuntimeError):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.status = int(status)
        self.code = code
        self.detail = detail

def _aware_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"planner {field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"planner {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"planner {field} must include timezone offset")
    return value

def _is_positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )

def _is_ascii_order_id(value: Any) -> bool:
    return isinstance(value, str) and ORDER_ID_RE.fullmatch(value) is not None

def _validate_items(
    items: Any, source_item_count: Any, *, require_complete: bool
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise RuntimeError("planner items must be a list")
    if (
        not isinstance(source_item_count, int)
        or isinstance(source_item_count, bool)
        or source_item_count < 0
    ):
        raise RuntimeError("planner source_item_count is invalid")
    if require_complete and (not items or source_item_count != len(items)):
        raise RuntimeError("planner returned incomplete SKU items")
    seen: set[str] = set()
    expected_keys = {
        "line_key",
        "product_id",
        "sku_id",
        "sku_name",
        "qty",
        "unit",
    }
    for item in items:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise RuntimeError("planner SKU item schema is invalid")
        line_key = item.get("line_key")
        if not isinstance(line_key, str) or not line_key or line_key in seen:
            raise RuntimeError("planner SKU line_key is empty or duplicate")
        seen.add(line_key)
        if not isinstance(item.get("sku_id"), str):
            raise RuntimeError("planner SKU id must be text")
        if not isinstance(item.get("product_id"), str):
            raise RuntimeError("planner product id must be text")
        if not isinstance(item.get("sku_name"), str) or not item["sku_name"]:
            raise RuntimeError("planner SKU name is missing")
        if not _is_positive_number(item.get("qty")):
            raise RuntimeError("planner SKU quantity must be positive")
        if not isinstance(item.get("unit"), str):
            raise RuntimeError("planner SKU unit must be text")
    return items

def _validate_plan(plan: Any) -> dict[str, Any]:
    expected_keys = {
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
    }
    if not isinstance(plan, dict) or set(plan) - {"external_system_order"} != expected_keys:
        raise RuntimeError("planner selected plan schema is invalid")
    if "external_system_order" in plan and type(plan["external_system_order"]) is not bool:
        raise RuntimeError("planner external_system_order must be boolean")
    if not _is_ascii_order_id(plan.get("o_id")) or not _is_ascii_order_id(
        plan.get("io_id")
    ):
        raise RuntimeError("planner selected identity is invalid")
    if plan.get("outbound_identity_unique") is not True:
        raise RuntimeError("planner selected identity is not uniquely provable")
    if not _is_positive_number(plan.get("weight_kg")):
        raise RuntimeError("planner selected weight is invalid")
    for field in ("current_carrier", "current_carrier_id"):
        if not isinstance(plan.get(field), str) or not plan[field]:
            raise RuntimeError(f"planner selected {field} is invalid")
    if (
        PRINT_PROFILE_CARRIERS.get(plan["current_carrier_id"])
        != plan["current_carrier"]
    ):
        raise RuntimeError("planner selected current carrier is not allowed")
    for field in (
        "has_waybill",
        "privacy_required",
        "delivery_hold_marked",
        "items_complete",
    ):
        if not isinstance(plan.get(field), bool):
            raise RuntimeError(f"planner selected {field} must be boolean")
    if plan["items_complete"] is not True:
        raise RuntimeError("planner selected plan has incomplete SKU items")
    if plan["delivery_hold_marked"] is not False:
        raise RuntimeError("planner selected plan has a delivery suspension marker")
    _validate_items(
        plan.get("items"), plan.get("source_item_count"), require_complete=True
    )
    if plan.get("state") != "READY_HYBRID" or plan.get("blockers") != []:
        raise RuntimeError("planner selected a blocked plan")
    steps = plan.get("steps")
    allowed_reset = {
        "RESET_CARRIER_AND_GET_WAYBILL:STO:申通E物流-山东",
        "RESET_CARRIER_AND_GET_WAYBILL:ZTO.1:中通速递-山东",
        "RESET_CARRIER_AND_GET_WAYBILL:ZTO:中通-天猫-隐私面单",
    }
    valid_steps = (
        steps == ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"]
        or steps == ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"]
        or (
            isinstance(steps, list)
            and len(steps) == 3
            and steps[0] in allowed_reset
            and steps[1:] == ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"]
        )
    )
    if not valid_steps:
        raise RuntimeError("planner selected steps are not an approved sequence")
    reset_steps = [step for step in steps if step.startswith("RESET_CARRIER")]
    resets_to_privacy = bool(
        reset_steps
        and reset_steps[0]
        == "RESET_CARRIER_AND_GET_WAYBILL:ZTO:中通-天猫-隐私面单"
    )
    final_carrier_id = (
        reset_steps[0].split(":", 2)[1]
        if reset_steps
        else str(plan["current_carrier_id"])
    )
    if plan["privacy_required"] and not (
        resets_to_privacy
        or (not reset_steps and plan["current_carrier_id"] == PRIVACY_CARRIER_ID)
    ):
        raise RuntimeError("planner privacy rule does not match final carrier")
    if not plan["privacy_required"] and resets_to_privacy:
        raise RuntimeError("planner routed a normal order to privacy carrier")
    if (
        not plan["privacy_required"]
        and float(plan["weight_kg"]) > 3.0
        and final_carrier_id != SOURCE_CARRIER_ID
    ):
        raise RuntimeError("planner routed an over-3kg normal order away from STO")
    if plan["has_waybill"] != (
        steps == ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"]
    ):
        raise RuntimeError("planner waybill state does not match steps")
    return plan

def validate_plan_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("planner plan response must be an object")
    if result.get("mode") != RAW_PLAN_MODE:
        raise RuntimeError("planner plan mode is invalid")
    if result.get("schema_version") != PLANNER_SCHEMA_VERSION:
        raise RuntimeError("planner plan schema version is invalid")
    _aware_timestamp(result.get("generated_at"), "generated_at")
    if result.get("candidate_pool_complete") is not True:
        raise RuntimeError("planner candidate pool was truncated")
    selected = result.get("selected")
    if not isinstance(selected, list) or len(selected) > PLAN_POOL_SIZE:
        raise RuntimeError("planner candidate pool is invalid")
    for plan in selected:
        _validate_plan(plan)
    for field in ("scope", "counts", "blocked_reason_counts"):
        if not isinstance(result.get(field), dict):
            raise RuntimeError(f"planner {field} is invalid")
    blocked_preview = result.get("blocked_preview")
    if not isinstance(blocked_preview, list):
        raise RuntimeError("planner blocked_preview is invalid")
    blocked_keys = {
        "o_id",
        "io_id",
        "identity_complete",
        "weight_kg",
        "items_complete",
        "blockers",
    }
    for blocked in blocked_preview:
        if not isinstance(blocked, dict) or set(blocked) != blocked_keys:
            raise RuntimeError("planner blocked item schema is invalid")
        identity_complete = blocked.get("identity_complete")
        if not isinstance(identity_complete, bool):
            raise RuntimeError("planner blocked identity_complete must be boolean")
        actual_identity_complete = _is_ascii_order_id(
            blocked.get("o_id")
        ) and _is_ascii_order_id(blocked.get("io_id"))
        if identity_complete != actual_identity_complete:
            raise RuntimeError("planner blocked identity completeness is inconsistent")
        if not isinstance(blocked.get("items_complete"), bool):
            raise RuntimeError("planner blocked items_complete must be boolean")
        blockers = blocked.get("blockers")
        if not isinstance(blockers, list) or not blockers or not all(
            isinstance(reason, str) and reason for reason in blockers
        ):
            raise RuntimeError("planner blocked reasons are invalid")
    if not isinstance(result.get("guardrails"), list):
        raise RuntimeError("planner guardrails is invalid")
    return result

def validate_inspect_result(
    result: Any, expected_o_id: str, expected_io_id: str
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("planner inspect response must be an object")
    base_keys = {
        "mode",
        "schema_version",
        "generated_at",
        "found",
        "o_id",
        "io_id",
    }
    found_keys = base_keys | {
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
    if result.get("mode") != RAW_INSPECT_MODE:
        raise RuntimeError("planner inspect mode is invalid")
    if result.get("schema_version") != PLANNER_SCHEMA_VERSION:
        raise RuntimeError("planner inspect schema version is invalid")
    _aware_timestamp(result.get("generated_at"), "generated_at")
    if not isinstance(result.get("found"), bool):
        raise RuntimeError("planner inspect found must be boolean")
    if str(result.get("o_id", "")) != expected_o_id or str(
        result.get("io_id", "")
    ) != expected_io_id:
        raise RuntimeError("planner inspect identity mismatch")
    if not result["found"]:
        if set(result) != base_keys:
            raise RuntimeError("planner not-found inspect schema is invalid")
        return result

    if "external_system_order" in result and type(result["external_system_order"]) is not bool:
        raise RuntimeError("planner external_system_order must be boolean")
    if set(result) - {"external_system_order"} != found_keys:
        raise RuntimeError("planner found inspect schema is invalid")

    for field in (
        "status",
        "warehouse_id",
        "io_date",
        "shop_name",
        "carrier_id",
        "carrier_name",
        "privacy_source",
        "redaction",
    ):
        if not isinstance(result.get(field), str):
            raise RuntimeError(f"planner inspect {field} must be text")
    if result["warehouse_id"] != TARGET_WAREHOUSE_ID:
        raise RuntimeError("planner inspect warehouse is outside the target scope")
    if not _is_positive_number(result.get("weight_kg")):
        raise RuntimeError("planner inspect weight is invalid")
    waybill_suffix = result.get("waybill_suffix")
    if not isinstance(waybill_suffix, str) or len(waybill_suffix) > 4:
        raise RuntimeError("planner inspect waybill suffix is invalid")
    if bool(waybill_suffix) != bool(result.get("has_waybill")):
        raise RuntimeError("planner inspect waybill state is inconsistent")
    waybill_fingerprint = result.get("waybill_fingerprint")
    if not isinstance(waybill_fingerprint, str) or (
        bool(result.get("has_waybill"))
        and re.fullmatch(r"[0-9a-f]{64}", waybill_fingerprint) is None
    ) or (not result.get("has_waybill") and waybill_fingerprint != ""):
        raise RuntimeError("planner inspect waybill fingerprint is invalid")
    if result["privacy_source"] != "remark":
        raise RuntimeError("planner inspect privacy source is invalid")
    if result["redaction"] != "buyer, address, phone and full waybill are omitted":
        raise RuntimeError("planner inspect redaction contract is invalid")
    bool_fields = (
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
        "items_complete",
    )
    for field in bool_fields:
        if not isinstance(result.get(field), bool):
            raise RuntimeError(f"planner inspect {field} must be boolean")
    errors = result.get("item_validation_errors")
    order_errors = result.get("order_validation_errors")
    delivery_hold_errors = result.get("delivery_hold_reasons")
    if not isinstance(errors, list) or not all(isinstance(x, str) for x in errors):
        raise RuntimeError("planner item_validation_errors is invalid")
    if not isinstance(order_errors, list) or not all(
        isinstance(x, str) for x in order_errors
    ):
        raise RuntimeError("planner order_validation_errors is invalid")
    if not isinstance(delivery_hold_errors, list) or not all(
        isinstance(x, str) and x for x in delivery_hold_errors
    ):
        raise RuntimeError("planner delivery_hold_reasons is invalid")
    if result["delivery_hold_marked"] != bool(delivery_hold_errors):
        raise RuntimeError("planner delivery suspension marker is inconsistent")
    _validate_items(
        result.get("items"),
        result.get("source_item_count"),
        require_complete=bool(result["items_complete"]),
    )
    if result["items_complete"] and (errors or order_errors):
        raise RuntimeError("planner claims complete items with validation errors")
    actions = result.get("actions")
    if not isinstance(actions, list):
        raise RuntimeError("planner actions must be a list")
    for action in actions:
        if (
            not isinstance(action, dict)
            or set(action) != {"name", "time"}
            or not all(isinstance(value, str) for value in action.values())
        ):
            raise RuntimeError("planner action schema is invalid")
    return result

def validate_inspect_batch_result(
    result: Any, expected_pairs: list[tuple[str, str]]
) -> dict[str, Any]:
    expected_keys = {"mode", "schema_version", "generated_at", "results"}
    if not isinstance(result, dict) or set(result) != expected_keys:
        raise RuntimeError("planner batch inspect response schema is invalid")
    if result.get("mode") != RAW_INSPECT_BATCH_MODE:
        raise RuntimeError("planner batch inspect mode is invalid")
    if result.get("schema_version") != PLANNER_SCHEMA_VERSION:
        raise RuntimeError("planner batch inspect schema version is invalid")
    generated_at = _aware_timestamp(result.get("generated_at"), "generated_at")
    results = result.get("results")
    if not isinstance(results, list) or len(results) != len(expected_pairs):
        raise RuntimeError("planner batch inspect result count is invalid")
    for item, (o_id, io_id) in zip(results, expected_pairs):
        validate_inspect_result(item, o_id, io_id)
        if item.get("generated_at") != generated_at:
            raise RuntimeError("planner batch inspect timestamps are inconsistent")
    return result

def _completion_proof_matches(proof: dict[str, Any], reason: str) -> bool:
    if proof.get("found") is not True:
        return False
    if reason == "PRINTED":
        return (
            proof.get("action_history_complete") is True
            and proof.get("has_print_action") is True
        )
    if reason == "TERMINAL":
        status = str(proof.get("status", "")).strip().lower()
        # An action-history row is not an authoritative terminal state.  The
        # upstream action API has no outbound io_id and can contain cancelled,
        # failed, or stale attempts, so only the current allowlisted order
        # status may permanently retire a lease as TERMINAL.
        return status in TERMINAL_ORDER_STATUSES
    return False

def _workstation_id(body: dict[str, Any]) -> str:
    client_value = body.get("workstation_id")
    if not isinstance(client_value, str) or not WORKSTATION_RE.fullmatch(client_value):
        raise APIError(400, "invalid_workstation_id")
    return client_value

def _identity(body: dict[str, Any]) -> tuple[str, str]:
    o_id, io_id = body.get("o_id"), body.get("io_id")
    if not _is_ascii_order_id(o_id) or not _is_ascii_order_id(io_id):
        raise APIError(400, "invalid_order_identity")
    return str(o_id), str(io_id)

def _claim_token(body: dict[str, Any]) -> str:
    value = body.get("claim_token")
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise APIError(400, "invalid_claim_token")
    return value

def _lease_credentials(body: dict[str, Any]) -> tuple[str, str, str, str]:
    if set(body) != {"workstation_id", "o_id", "io_id", "claim_token"}:
        raise APIError(400, "invalid_lease_request")
    workstation_id = _workstation_id(body)
    o_id, io_id = _identity(body)
    return workstation_id, o_id, io_id, _claim_token(body)

def _completion_credentials(
    body: dict[str, Any],
) -> tuple[str, str, str, str, str]:
    if set(body) != {
        "workstation_id",
        "o_id",
        "io_id",
        "claim_token",
        "completion_reason",
    }:
        raise APIError(400, "invalid_completion_request")
    workstation_id = _workstation_id(body)
    o_id, io_id = _identity(body)
    claim_token = _claim_token(body)
    completion_reason = body.get("completion_reason")
    if (
        not isinstance(completion_reason, str)
        or completion_reason not in COMPLETION_REASONS
    ):
        raise APIError(400, "invalid_completion_reason")
    return workstation_id, o_id, io_id, claim_token, completion_reason

def _batch_lease_credentials(
    body: dict[str, Any],
) -> tuple[str, list[tuple[str, str, str, str]]]:
    if set(body) != {"workstation_id", "orders"}:
        raise APIError(400, "invalid_batch_inspect_request")
    workstation_id = _workstation_id(body)
    orders = body.get("orders")
    if not isinstance(orders, list) or not 1 <= len(orders) <= MAX_BATCH_CANDIDATES:
        raise APIError(400, "invalid_batch_inspect_orders")
    credentials: list[tuple[str, str, str, str]] = []
    pairs: list[tuple[str, str]] = []
    for item in orders:
        if not isinstance(item, dict) or set(item) != {
            "o_id",
            "io_id",
            "claim_token",
        }:
            raise APIError(400, "invalid_batch_inspect_order")
        o_id, io_id = _identity(item)
        claim_token = _claim_token(item)
        pairs.append((o_id, io_id))
        credentials.append((o_id, io_id, workstation_id, claim_token))
    if len(set(pairs)) != len(pairs) or len({pair[0] for pair in pairs}) != len(pairs):
        raise APIError(400, "duplicate_batch_inspect_identity")
    return workstation_id, credentials

def _exclude_pairs(body: dict[str, Any]) -> set[tuple[str, str]]:
    value = body.get("exclude", [])
    if not isinstance(value, list) or len(value) > MAX_EXCLUDES:
        raise APIError(400, "invalid_exclude_list")
    result: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"o_id", "io_id"}:
            raise APIError(400, "invalid_exclude_identity")
        o_id, io_id = item.get("o_id"), item.get("io_id")
        if not _is_ascii_order_id(o_id) or not _is_ascii_order_id(io_id):
            raise APIError(400, "invalid_exclude_identity")
        result.add((str(o_id), str(io_id)))
    return result

def _lease_payload(lease: Lease) -> dict[str, Any]:
    payload = lease.as_dict()
    payload.update(
        {
            "ok": True,
            "api_schema_version": API_SCHEMA_VERSION,
            "lease_required": True,
            "generated_at": utc_text(),
        }
    )
    return payload

def candidate_print_profile(candidate: dict[str, Any]) -> str:
    """Map a validated planner sequence to one physical label-paper profile."""

    reset_prefix = "RESET_CARRIER_AND_GET_WAYBILL:"
    reset_steps = [
        str(step)
        for step in (candidate.get("steps") or [])
        if str(step).startswith(reset_prefix)
    ]
    if len(reset_steps) > 1:
        raise RuntimeError("candidate contains multiple target carriers")
    if not reset_steps:
        carrier_id = candidate.get("current_carrier_id")
        carrier_name = candidate.get("current_carrier")
        if PRINT_PROFILE_CARRIERS.get(carrier_id) != carrier_name:
            raise RuntimeError("candidate current carrier is not an allowed print profile")
        return str(carrier_id)
    carrier_id = reset_steps[0][len(reset_prefix) :].split(":", 1)[0]
    if carrier_id not in PRINT_PROFILES:
        raise RuntimeError("candidate target carrier is not an allowed print profile")
    return carrier_id

def _candidate_product_group(
    candidate: dict[str, Any],
) -> Optional[tuple[tuple[str, str], ...]]:
    """Return the order's (货号, SKU 编号) signature, or None when unprovable.

    Keeps the original 货号 grouping, then sorts by SKU 编号 within it, so one
    product's sizes stay together and print in size order.
    """

    items = candidate.get("items")
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

def group_candidates_by_product(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group by 货号 then SKU 编号, sorted by natural order.

    Groups by the complete 货号 set, then orders variants within that set by
    SKU 编号 (e.g. ``…70…`` before ``…80…``).  Candidates without a complete
    identity keep their relative order last.
    """

    groups: dict[tuple[Any, ...], list[tuple[tuple[Any, ...], dict[str, Any]]]] = {}
    order: list[tuple[Any, ...]] = []
    for index, candidate in enumerate(candidates):
        signature = _candidate_product_group(candidate)
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
        groups[key].append((sku_key, candidate))
    order.sort(key=lambda key: (
        (0, tuple(_natural_sort_key(pid) for pid in key[1:]))
        if key[0] == "product" else (1, ())
    ))
    # Stable sorting preserves incoming order for equal SKU signatures and
    # keeps incomplete identities last in their original order.
    return [
        candidate
        for key in order
        for _sku_key, candidate in sorted(groups[key], key=lambda entry: entry[0])
    ]

def _plan(body: dict[str, Any], store: LeaseStore, source: Any) -> dict[str, Any]:
    if not set(body).issubset(
        {"workstation_id", "exclude", "max_candidates", "print_profile", "skip_external_orders"}
    ):
        raise APIError(400, "invalid_plan_request")
    workstation_id = _workstation_id(body)
    print_profile = body.get("print_profile")
    if not isinstance(print_profile, str) or print_profile not in PRINT_PROFILES:
        raise APIError(400, "invalid_print_profile")
    max_candidates = body.get("max_candidates", 1)
    if (
        isinstance(max_candidates, bool)
        or not isinstance(max_candidates, int)
        or not 1 <= max_candidates <= MAX_BATCH_CANDIDATES
    ):
        raise APIError(400, "invalid_max_candidates")
    skip_external = body.get("skip_external_orders", False)
    if type(skip_external) is not bool:
        raise APIError(400, "invalid_skip_external_orders")
    excludes = _exclude_pairs(body)
    raw = validate_plan_result(source.plan())
    if skip_external and any(type(item.get("external_system_order")) is not bool for item in raw["selected"]):
        raise APIError(503, "external_order_labels_unavailable")
    selected_plans: list[dict[str, Any]] = []
    owned = store.active_for_workstation(workstation_id)
    owned_by_pair = {(lease.o_id, lease.io_id): lease for lease in owned}
    candidates = group_candidates_by_product(
        [
            candidate
            for candidate in raw["selected"]
            if candidate_print_profile(candidate) == print_profile
            and (not skip_external or candidate.get("external_system_order") is False)
        ]
    )
    # Keep selection diagnostics separate from planner eligibility.  An empty
    # selected list does not necessarily mean that the planner found no order:
    # the workstation may have excluded every eligible pair, or another
    # workstation/permanent lease state may make every remaining pair
    # unclaimable.  These counters let the client report that distinction
    # without weakening any duplicate-print guardrail.
    profile_excluded = sum(
        1
        for candidate in candidates
        if (
            str(candidate["o_id"]),
            str(candidate["io_id"]),
        )
        in excludes
        and (
            str(candidate["o_id"]),
            str(candidate["io_id"]),
        )
        not in owned_by_pair
    )
    claim_attempted = 0
    claim_unavailable = 0
    for candidate in candidates:
        if len(selected_plans) >= max_candidates:
            break
        pair = (str(candidate["o_id"]), str(candidate["io_id"]))
        lease = owned_by_pair.get(pair)
        # HTTP retries must return every still-owned pair/token even if an old
        # local exclude list contains it. New capacity is filled only from
        # non-excluded candidates.
        if lease is None and pair in excludes:
            continue
        if lease is None:
            claim_attempted += 1
            lease = store.claim(pair[0], pair[1], workstation_id)
        if lease is None:
            claim_unavailable += 1
            continue
        selected_plan = copy.deepcopy(candidate)
        if not skip_external:
            selected_plan.pop("external_system_order", None)
        selected_plan.update(
            {
                "claim_token": lease.claim_token,
                "expires_at": utc_text(lease.expires_epoch),
                "lease_ttl_seconds": lease.lease_ttl_seconds,
            }
        )
        selected_plans.append(selected_plan)

    counts = copy.deepcopy(raw["counts"])
    counts["selected"] = len(selected_plans)
    counts["candidate_pool"] = len(raw["selected"])
    counts["external_order_excluded"] = sum(
        1 for item in raw["selected"]
        if skip_external and item.get("external_system_order") is True
        and candidate_print_profile(item) == print_profile
    )
    counts["profile_ready"] = len(candidates)
    counts["profile_excluded"] = profile_excluded
    counts["claim_attempted"] = claim_attempted
    counts["claim_unavailable"] = claim_unavailable
    return {
        "mode": PLAN_MODE,
        "api_schema_version": API_SCHEMA_VERSION,
        "planner_schema_version": PLANNER_SCHEMA_VERSION,
        "lease_required": True,
        "print_profile": print_profile,
        "skip_external_orders": skip_external,
        "generated_at": raw["generated_at"],
        "scope": raw["scope"],
        "counts": counts,
        "selected": selected_plans,
        "blocked_preview": raw["blocked_preview"],
        "blocked_reason_counts": raw["blocked_reason_counts"],
        "guardrails": list(raw["guardrails"])
        + [
            "Every selected o_id/io_id pair is protected by an atomic local reservation",
            f"At most {MAX_BATCH_CANDIDATES} independently leased orders form one workstation batch",
        ],
    }

def _claimed_inspect_result(
    raw: dict[str, Any], lease: Lease, *, include_external_order_status: bool = False
) -> dict[str, Any]:
    result = copy.deepcopy(raw)
    if not include_external_order_status:
        result.pop("external_system_order", None)
    # Raw JST action names are not part of the browser safety decision and may
    # contain free-form upstream text.  Keep only the validated has_* proof
    # booleans; preserve the existing list field as an empty compatibility shell.
    if result.get("found") is True:
        result["actions"] = []
    result["mode"] = INSPECT_MODE
    result["api_schema_version"] = API_SCHEMA_VERSION
    result["planner_schema_version"] = result.pop("schema_version")
    result["lease_required"] = True
    result["lease_expires_at"] = utc_text(lease.expires_epoch)
    result["lease_ttl_seconds"] = lease.lease_ttl_seconds
    return result

def _require_completion_proof(readback: dict[str, Any], reason: str) -> None:
    if readback.get("found") is not True:
        raise APIError(409, "completion_proof_failed", "order not found")
    if reason == "PRINTED" and not _completion_proof_matches(readback, reason):
        raise APIError(
            409,
            "completion_proof_failed",
            "PRINTED requires complete action history and a print action",
        )
    if reason == "TERMINAL" and not _completion_proof_matches(readback, reason):
        raise APIError(
            409,
            "completion_proof_failed",
            "TERMINAL requires current Sent/Delete status",
        )

def _inspect_feature_request(body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    # Keep existing lease validation exact; this optional readback feature is
    # accepted only on inspect endpoints, never on renew/release/complete.
    credentials = dict(body)
    include_external = credentials.pop("include_external_order_status", False)
    if type(include_external) is not bool:
        raise APIError(400, "invalid_include_external_order_status")
    return credentials, include_external

def _inspect(body: dict[str, Any], store: LeaseStore, source: Any) -> dict[str, Any]:
    body, include_external = _inspect_feature_request(body)
    workstation_id, o_id, io_id, claim_token = _lease_credentials(body)
    store.require_active(o_id, io_id, workstation_id, claim_token)
    raw = source.run(
        ["--inspect-o-id", o_id, "--inspect-io-id", io_id,
         "--action-history-days", str(ACTION_HISTORY_DAYS)],
        timeout=INSPECT_TIMEOUT_SECONDS,
    )
    validate_inspect_result(raw, o_id, io_id)
    lease = store.require_active(o_id, io_id, workstation_id, claim_token)
    return _claimed_inspect_result(
        raw, lease, include_external_order_status=include_external
    )


def _complete(body: dict[str, Any], store: LeaseStore, source: Any) -> dict[str, Any]:
    workstation_id, o_id, io_id, claim_token, reason = _completion_credentials(body)
    try:
        committed = store.completed_for_retry(
            o_id, io_id, workstation_id, claim_token, reason
        )
        if committed is not None:
            return _lease_payload(committed)

        if reason in AUTOMATIC_COMPLETION_REASONS:
            readback = _inspect(
                {"workstation_id": workstation_id, "o_id": o_id,
                 "io_id": io_id, "claim_token": claim_token}, store, source
            )
            _require_completion_proof(readback, reason)
        else:
            # These two reasons are explicit local safety decisions in the
            # single-Bearer/single-workstation deployment.  They intentionally
            # require an active exact lease but no claim about remote order state.
            store.require_active(o_id, io_id, workstation_id, claim_token)

        lease = store.complete(
            o_id,
            io_id,
            workstation_id,
            claim_token,
            reason,
        )
    except LeaseConflict as exc:
        raise APIError(409, "lease_conflict", str(exc)) from exc
    return _lease_payload(lease)

def _inspect_batch(body: dict[str, Any], store: LeaseStore, source: Any) -> dict[str, Any]:
    body, include_external = _inspect_feature_request(body)
    workstation_id, credentials = _batch_lease_credentials(body)
    try:
        store.renew_many(credentials)
    except LeaseConflict as exc:
        raise APIError(409, "lease_conflict", str(exc)) from exc
    pairs = [(o_id, io_id) for o_id, io_id, _owner, _token in credentials]
    arguments: list[str] = []
    for o_id, io_id in pairs:
        arguments.extend(["--inspect-pair", f"{o_id}:{io_id}"])
    arguments.extend(["--action-history-days", str(ACTION_HISTORY_DAYS)])
    raw = source.run(arguments, timeout=INSPECT_TIMEOUT_SECONDS)
    validate_inspect_batch_result(raw, pairs)
    leases: list[Lease] = []
    for o_id, io_id, owner, claim_token in credentials:
        if owner != workstation_id:
            raise RuntimeError("batch reservation owner changed unexpectedly")
        leases.append(store.require_active(o_id, io_id, owner, claim_token))
    raw_results = raw["results"]
    return {
        "mode": INSPECT_BATCH_MODE,
        "api_schema_version": API_SCHEMA_VERSION,
        "planner_schema_version": PLANNER_SCHEMA_VERSION,
        "lease_required": True,
        "generated_at": raw["generated_at"],
        "results": [
            _claimed_inspect_result(item, lease, include_external_order_status=include_external)
            for item, lease in zip(raw_results, leases)
        ],
    }


def process_request(
    path: str, body: dict[str, Any], *, store: LeaseStore, source: Any
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise APIError(400, "invalid_request")
    if path == "/jst-print-api/v1/ping":
        if not set(body).issubset({"workstation_id"}):
            raise APIError(400, "invalid_ping_request")
        if "workstation_id" in body:
            _workstation_id(body)
        return {
            "ok": True,
            "service": "jst-print-api",
            "api_schema_version": API_SCHEMA_VERSION,
            "minimum_client_version": MIN_CLIENT_VERSION,
            "lease_required": True,
            "plan_mode": PLAN_MODE,
            "inspect_mode": INSPECT_MODE,
            "batch_inspect_mode": INSPECT_BATCH_MODE,
            "workstation_binding": "BEARER_TOKEN_DERIVED_SINGLE_WORKSTATION_V1",
            "completion_reasons": sorted(COMPLETION_REASONS),
            "generated_at": utc_text(),
        }

    lease_store = store
    if path == "/jst-print-api/v1/plan":
        return _plan(body, lease_store, source)
    if path == "/jst-print-api/v1/inspect":
        return _inspect(body, lease_store, source)
    if path == "/jst-print-api/v1/inspect-batch":
        return _inspect_batch(body, lease_store, source)
    if path == "/jst-print-api/v1/order/force-skip":
        if set(body) != {"workstation_id", "o_id", "io_id", "reason"}:
            raise APIError(400, "invalid_force_skip_request")
        workstation_id = _workstation_id(body)
        o_id, io_id = _identity(body)
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise APIError(400, "invalid_force_skip_reason")
        record = lease_store.force_skip(o_id, io_id, workstation_id, reason.strip())
        return {
            "ok": True,
            "api_schema_version": API_SCHEMA_VERSION,
            "o_id": o_id,
            "io_id": io_id,
            "permanently_excluded": True,
            "completion_reason": "OPERATOR_SKIPPED",
            "skipped_at": utc_text(float(record["skipped_at"])),
            "generated_at": utc_text(),
        }
    if path in {
        "/jst-print-api/v1/lease/renew",
        "/jst-print-api/v1/lease/release",
    }:
        workstation_id, o_id, io_id, claim_token = _lease_credentials(body)
        try:
            if path.endswith("/renew"):
                lease = lease_store.renew(o_id, io_id, workstation_id, claim_token)
            else:
                lease = lease_store.release(o_id, io_id, workstation_id, claim_token)
        except LeaseConflict as exc:
            raise APIError(409, "lease_conflict", str(exc)) from exc
        return _lease_payload(lease)
    if path == "/jst-print-api/v1/lease/complete":
        return _complete(body, lease_store, source)
    raise APIError(404, "not_found")
