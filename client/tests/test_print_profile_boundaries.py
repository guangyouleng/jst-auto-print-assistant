import sys
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jst_auto_print_app as app

SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER_DIR))
try:
    import jst_print_api_server as api
finally:
    sys.path.remove(str(SERVER_DIR))


def _plan(steps, carrier_id=app.SOURCE_CARRIER_ID, carrier_name=app.SOURCE_CARRIER_NAME):
    return {
        "o_id": "1",
        "io_id": "2",
        "steps": list(steps),
        "current_carrier_id": carrier_id,
        "current_carrier": carrier_name,
    }


def _validated_candidate(
    *,
    weight=4.62,
    steps=None,
    carrier_id=app.TARGET_CARRIER_ID,
    carrier_name=app.TARGET_CARRIER_NAME,
):
    return {
        "o_id": "101",
        "io_id": "201",
        "outbound_identity_unique": True,
        "weight_kg": weight,
        "current_carrier": carrier_name,
        "current_carrier_id": carrier_id,
        "has_waybill": False,
        "privacy_required": False,
        "delivery_hold_marked": False,
        "items_complete": True,
        "source_item_count": 1,
        "items": [
            {
                "line_key": "1",
                "product_id": "P1",
                "sku_id": "S1",
                "sku_name": "测试商品",
                "qty": 1.0,
                "unit": "件",
            }
        ],
        "state": "READY_HYBRID",
        "steps": list(
            steps
            or ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"]
        ),
        "blockers": [],
    }


class PrintProfileBoundaryTests(unittest.TestCase):
    def test_client_ping_rejects_backend_without_minimum_version_contract(self):
        client = object.__new__(app.PlannerClient)
        client.settings = app.Settings()
        client._post = lambda *_args, **_kwargs: {
            "ok": True,
            "api_schema_version": app.API_SCHEMA_VERSION,
            "lease_required": True,
            "plan_mode": "LIVE_READ_ONLY_CLAIMED_V1",
            "inspect_mode": "ORDER_READBACK_CLAIMED_V1",
            "batch_inspect_mode": "ORDER_BATCH_READBACK_CLAIMED_V1",
        }

        with self.assertRaises(app.BackendSchemaError):
            client.ping()

    def test_client_ping_accepts_current_minimum_version_contract(self):
        client = object.__new__(app.PlannerClient)
        client.settings = app.Settings()
        client._post = lambda *_args, **_kwargs: {
            "ok": True,
            "api_schema_version": app.API_SCHEMA_VERSION,
            "minimum_client_version": app.APP_VERSION,
            "lease_required": True,
            "workstation_binding": app.WORKSTATION_BINDING_MODE,
            "completion_reasons": sorted(app.COMPLETION_REASONS),
            "plan_mode": "LIVE_READ_ONLY_CLAIMED_V1",
            "inspect_mode": "ORDER_READBACK_CLAIMED_V1",
            "batch_inspect_mode": "ORDER_BATCH_READBACK_CLAIMED_V1",
        }

        self.assertTrue(client.ping())

    def test_client_and_server_reject_over_3kg_plan_that_stays_zto(self):
        candidate = _validated_candidate()

        with self.assertRaises(app.BackendSchemaError):
            app.PlannerClient._validate_plan_item(candidate)
        with self.assertRaises(RuntimeError):
            api._validate_plan(candidate)

    def test_client_and_server_accept_over_3kg_reset_to_sto(self):
        candidate = _validated_candidate(
            steps=[
                f"RESET_CARRIER_AND_GET_WAYBILL:{app.SOURCE_CARRIER_ID}:{app.SOURCE_CARRIER_NAME}",
                "PRINT_EXPRESS",
                "STOP_BEFORE_PRESHIP",
            ]
        )

        claimed_candidate = dict(
            candidate,
            claim_token="x" * 32,
            lease_ttl_seconds=300,
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        app.PlannerClient._validate_plan_item(claimed_candidate)
        self.assertIs(api._validate_plan(candidate), candidate)

    def test_current_plan_schema_rejects_legacy_inventory_fields(self):
        candidate = _validated_candidate(
            steps=[
                f"RESET_CARRIER_AND_GET_WAYBILL:{app.SOURCE_CARRIER_ID}:{app.SOURCE_CARRIER_NAME}",
                "PRINT_EXPRESS",
                "STOP_BEFORE_PRESHIP",
            ]
        )
        candidate.update(
            inventory_complete=True,
            inventory_sufficient=True,
        )

        with self.assertRaises(RuntimeError):
            api._validate_plan(candidate)

    def test_client_candidate_rejects_legacy_inventory_diagnostics(self):
        candidate = _validated_candidate(
            steps=[
                f"RESET_CARRIER_AND_GET_WAYBILL:{app.SOURCE_CARRIER_ID}:{app.SOURCE_CARRIER_NAME}",
                "PRINT_EXPRESS",
                "STOP_BEFORE_PRESHIP",
            ]
        )
        candidate.update(
            inventory_complete=False,
            inventory_sufficient=False,
            inventory_validation_errors=["库存服务不可用"],
            claim_token="x" * 32,
            lease_ttl_seconds=300,
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )

        with self.assertRaises(app.BackendSchemaError):
            app.PlannerClient._validate_plan_item(candidate)

    def test_client_inspect_schema_rejects_inventory_fields(self):
        generated_at = datetime.now(timezone.utc).isoformat()
        readback = {
            "mode": "ORDER_READBACK_CLAIMED_V1",
            "api_schema_version": app.API_SCHEMA_VERSION,
            "planner_schema_version": app.PLANNER_SCHEMA_VERSION,
            "lease_required": True,
            "generated_at": generated_at,
            "lease_expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
            "lease_ttl_seconds": 300,
            "found": True,
            "o_id": "101",
            "io_id": "201",
            "has_waybill": False,
            "is_print_express": False,
            "action_history_complete": True,
            "has_print_request": False,
            "has_print_action": False,
            "has_ship_action": False,
            "has_manual_carrier_action": False,
            "privacy_required": False,
            "privacy_review_required": False,
            "delivery_hold_marked": False,
            "status": "WaitConfirm",
            "weight_kg": 1.0,
            "warehouse_id": app.TARGET_WAREHOUSE_ID,
            "io_date": "2026-08-26 12:00:00",
            "shop_name": "测试店铺",
            "carrier_id": app.SOURCE_CARRIER_ID,
            "carrier_name": app.SOURCE_CARRIER_NAME,
            "waybill_suffix": "",
            "waybill_fingerprint": "",
            "privacy_source": "remark",
            "actions": [],
            "items_complete": True,
            "item_validation_errors": [],
            "order_validation_errors": [],
            "delivery_hold_reasons": [],
            "source_item_count": 1,
            "items": [
                {
                    "line_key": "1",
                    "product_id": "P1",
                    "sku_id": "S1",
                    "sku_name": "测试商品",
                    "qty": 1.0,
                    "unit": "件",
                }
            ],
            "redaction": "buyer, address, phone and full waybill are omitted",
        }

        app.PlannerClient._validate_inspect_schema(readback, "101", "201")
        with self.assertRaises(app.BackendSchemaError):
            missing_print_request = dict(readback)
            missing_print_request.pop("has_print_request")
            app.PlannerClient._validate_inspect_schema(
                missing_print_request, "101", "201"
            )
        for field, value in (
            ("inventory_complete", False),
            ("inventory_sufficient", False),
            ("inventory_validation_errors", ["库存服务不可用"]),
            ("schema_version", app.PLANNER_SCHEMA_VERSION),
        ):
            with self.subTest(field=field):
                invalid = dict(readback, **{field: value})
                with self.assertRaises(app.BackendSchemaError):
                    app.PlannerClient._validate_inspect_schema(
                        invalid, "101", "201"
                    )
        with self.assertRaises(app.BackendSchemaError):
            app.PlannerClient._validate_inspect_schema(
                dict(readback, waybill_suffix="12345"), "101", "201"
            )
        with self.assertRaises(app.BackendSchemaError):
            app.PlannerClient._validate_inspect_schema(
                dict(readback, waybill_suffix="1234"), "101", "201"
            )
        with self.assertRaises(app.BackendSchemaError):
            app.PlannerClient._validate_inspect_schema(
                dict(
                    readback,
                    has_waybill=True,
                    waybill_suffix="1234",
                    waybill_fingerprint="0" * 63,
                ),
                "101",
                "201",
            )
        with self.assertRaises(app.BackendSchemaError):
            app.PlannerClient._validate_inspect_schema(
                dict(readback, redaction="receiver address included"), "101", "201"
            )

    def test_client_inspect_schema_rejects_private_or_full_waybill_data(self):
        generated_at = datetime.now(timezone.utc).isoformat()
        not_found = {
            "mode": "ORDER_READBACK_CLAIMED_V1",
            "api_schema_version": app.API_SCHEMA_VERSION,
            "planner_schema_version": app.PLANNER_SCHEMA_VERSION,
            "lease_required": True,
            "generated_at": generated_at,
            "lease_expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
            "lease_ttl_seconds": 300,
            "found": False,
            "o_id": "101",
            "io_id": "201",
        }
        app.PlannerClient._validate_inspect_schema(not_found, "101", "201")
        for field in ("buyer_name", "receiver_address", "waybill"):
            with self.subTest(field=field):
                with self.assertRaises(app.BackendSchemaError):
                    app.PlannerClient._validate_inspect_schema(
                        dict(not_found, **{field: "sensitive"}), "101", "201"
                    )

    def test_client_planner_schema_version_is_five(self):
        self.assertEqual(app.PLANNER_SCHEMA_VERSION, 5)

    def test_trial_mode_claims_only_one_order_before_real_waybill_write(self):
        source = inspect.getsource(app.AutomationEngine._worker)

        self.assertIn(
            "max_candidates=(BATCH_PRINT_SIZE if settings.allow_print else 1)",
            source,
        )

    def test_operator_step_descriptions_hide_internal_tokens(self):
        plan = _plan(
            [
                f"RESET_CARRIER_AND_GET_WAYBILL:{app.TARGET_CARRIER_ID}:{app.TARGET_CARRIER_NAME}",
                "PRINT_EXPRESS",
                "STOP_BEFORE_PRESHIP",
            ]
        )
        description = app.describe_plan_steps(plan)
        self.assertEqual(
            description,
            "切换为中通普通面单并取号 → 打印面单 → 完成（停在预发货前）",
        )
        self.assertNotIn("RESET_CARRIER", description)
        self.assertNotIn("PRINT_EXPRESS", description)

    def test_no_order_notice_distinguishes_ready_but_unclaimed_and_deduplicates(self):
        engine = object.__new__(app.AutomationEngine)
        engine.no_order_notice_key = None
        statuses = []
        events = []
        engine.set_status = statuses.append
        engine._event = lambda *args, **kwargs: events.append((args, kwargs))
        settings = app.Settings(
            loop_seconds=30, print_profile=app.TARGET_CARRIER_ID
        )
        payload = {
            "counts": {
                "orders_read": 217,
                "in_scope": 12,
                "blocked": 0,
                "profile_ready": 5,
                "selected": 0,
            },
            "selected": [],
        }

        engine._announce_no_orders(settings, payload)
        engine._announce_no_orders(settings, payload)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0][1], "NO_MATCHING_ORDERS")
        self.assertIn("本轮未领取到“中通普通面单”订单", events[0][0][2])
        self.assertIn("5 笔符合本类规则", events[0][0][2])
        self.assertIn("本机安全排除", events[0][0][2])
        self.assertIn("点击停止", events[0][0][2])
        self.assertIn("仍在定时检查", statuses[-1])
        self.assertNotIn("暂无", events[0][0][2])
        self.assertNotIn("已打完", events[0][0][2])

        engine.no_order_notice_key = None
        engine._announce_no_orders(settings, payload)
        self.assertEqual(len(events), 2)

    def test_no_order_notice_distinguishes_rule_blocked_orders(self):
        engine = object.__new__(app.AutomationEngine)
        engine.no_order_notice_key = None
        statuses = []
        events = []
        engine.set_status = statuses.append
        engine._event = lambda *args, **kwargs: events.append((args, kwargs))
        settings = app.Settings(print_profile=app.SOURCE_CARRIER_ID)

        engine._announce_no_orders(
            settings,
            {
                "counts": {
                    "orders_read": 217,
                    "in_scope": 9,
                    "blocked": 9,
                    "profile_ready": 0,
                    "selected": 0,
                },
                "selected": [],
            },
        )

        message = events[0][0][2]
        self.assertIn("本轮未领取到", message)
        self.assertIn("9 笔待审范围订单", message)
        self.assertIn("9 笔因安全规则未处理", message)
        self.assertIn("导出异常 CSV", message)
        self.assertNotIn("暂无", message)

    def test_no_order_notice_distinguishes_other_paper_profile(self):
        engine = object.__new__(app.AutomationEngine)
        engine.no_order_notice_key = None
        events = []
        engine.set_status = lambda *_args: None
        engine._event = lambda *args, **kwargs: events.append((args, kwargs))
        settings = app.Settings(print_profile=app.TARGET_CARRIER_ID)

        engine._announce_no_orders(
            settings,
            {
                "counts": {
                    "orders_read": 12,
                    "in_scope": 3,
                    "ready": 3,
                    "blocked": 0,
                    "profile_ready": 0,
                    "selected": 0,
                },
                "selected": [],
            },
        )

        message = events[0][0][2]
        self.assertIn("全部面单类型共有 3 笔符合规则", message)
        self.assertIn("最终路由到“中通普通面单”的订单为 0 笔", message)
        self.assertIn("当前快递不等于", message)

    def test_no_order_notice_uses_server_exclude_and_claim_diagnostics(self):
        engine = object.__new__(app.AutomationEngine)
        engine.no_order_notice_key = None
        events = []
        engine.set_status = lambda *_args: None
        engine._event = lambda *args, **kwargs: events.append((args, kwargs))
        settings = app.Settings(print_profile=app.TARGET_CARRIER_ID)

        engine._announce_no_orders(
            settings,
            {
                "counts": {
                    "orders_read": 12,
                    "in_scope": 3,
                    "ready": 3,
                    "blocked": 0,
                    "profile_ready": 3,
                    "profile_excluded": 2,
                    "claim_attempted": 1,
                    "claim_unavailable": 1,
                    "selected": 0,
                },
                "selected": [],
            },
        )

        message = events[0][0][2]
        self.assertIn("本机历史安全排除 2 笔", message)
        self.assertIn("领取阶段不可用 1 笔", message)

    def test_plan_count_contract_rejects_mismatched_selected_count(self):
        with self.assertRaises(app.BackendSchemaError):
            app.PlannerClient._validate_plan_counts(
                {
                    "orders_read": 1,
                    "in_scope": 1,
                    "ready": 1,
                    "blocked": 0,
                    "profile_ready": 1,
                    "selected": 0,
                },
                selected_count=1,
            )

    def test_plan_count_contract_requires_empty_result_diagnostics_to_balance(self):
        with self.assertRaises(app.BackendSchemaError):
            app.PlannerClient._validate_plan_counts(
                {
                    "orders_read": 3,
                    "in_scope": 3,
                    "ready": 3,
                    "blocked": 0,
                    "profile_ready": 3,
                    "profile_excluded": 1,
                    "claim_attempted": 1,
                    "claim_unavailable": 1,
                    "selected": 0,
                },
                selected_count=0,
            )

    def test_no_order_notice_distinguishes_empty_scan(self):
        engine = object.__new__(app.AutomationEngine)
        engine.no_order_notice_key = None
        statuses = []
        events = []
        engine.set_status = statuses.append
        engine._event = lambda *args, **kwargs: events.append((args, kwargs))
        settings = app.Settings(print_profile=app.PRIVACY_CARRIER_ID)

        engine._announce_no_orders(
            settings,
            {
                "counts": {
                    "orders_read": 0,
                    "in_scope": 0,
                    "blocked": 0,
                    "profile_ready": 0,
                    "selected": 0,
                },
                "selected": [],
            },
        )

        message = events[0][0][2]
        self.assertIn("本轮未领取到", message)
        self.assertIn("本轮共扫描 0 笔", message)
        self.assertIn("未扫描到属于当前自动化待审范围", message)
        self.assertNotIn("暂无", message)

    def test_no_order_event_is_non_blocking_log_only(self):
        source = inspect.getsource(app.DesktopApp._drain_messages)

        self.assertNotIn("NO_MATCHING_ORDERS", source)

    def test_filter_summary_explains_why_visible_rows_are_not_printable(self):
        engine = object.__new__(app.AutomationEngine)
        engine.seen_blocks = set()
        engine.last_filter_summary = ""
        engine.store = SimpleNamespace(event=lambda *_args, **_kwargs: {})
        engine.notify = lambda *_args: None
        events = []
        engine._event = lambda *args, **kwargs: events.append((args, kwargs))
        payload = {
            "blocked_preview": [],
            "counts": {
                "orders_read": 2107,
                "in_scope": 72,
                "ready": 19,
                "blocked": 53,
                "profile_ready": 16,
                "selected": 10,
                "profile_excluded": 4,
                "claim_unavailable": 2,
            },
            "blocked_reason_counts": {
                "已有打印动作，禁止重复打印": 40,
                "已有发货或预发货动作，禁止重复处理": 16,
            },
        }

        engine._record_blocked(payload, app.TARGET_CARRIER_ID)

        message = events[-1][0][2]
        self.assertIn("页面可见订单不等于待打单", message)
        self.assertIn("后台共扫描 2107 笔", message)
        self.assertIn("已有打印动作", message)
        self.assertIn("本机历史排除 4 笔", message)
        self.assertIn("领取阶段不可用 2 笔", message)

    def test_each_reviewed_sequence_maps_to_exactly_one_paper_profile(self):
        self.assertEqual(
            app.plan_print_profile(_plan(["GET_WAYBILL", "PRINT_EXPRESS"])),
            app.SOURCE_CARRIER_ID,
        )
        self.assertEqual(
            app.plan_print_profile(
                _plan(
                    [
                        f"RESET_CARRIER_AND_GET_WAYBILL:{app.SOURCE_CARRIER_ID}:{app.SOURCE_CARRIER_NAME}",
                        "PRINT_EXPRESS",
                    ],
                    app.TARGET_CARRIER_ID,
                    app.TARGET_CARRIER_NAME,
                )
            ),
            app.SOURCE_CARRIER_ID,
        )
        self.assertEqual(
            app.plan_print_profile(
                _plan(
                    [
                        f"RESET_CARRIER_AND_GET_WAYBILL:{app.TARGET_CARRIER_ID}:{app.TARGET_CARRIER_NAME}",
                        "PRINT_EXPRESS",
                    ]
                )
            ),
            app.TARGET_CARRIER_ID,
        )
        self.assertEqual(
            app.plan_print_profile(
                _plan(
                    [
                        f"RESET_CARRIER_AND_GET_WAYBILL:{app.PRIVACY_CARRIER_ID}:{app.PRIVACY_CARRIER_NAME}",
                        "PRINT_EXPRESS",
                    ]
                )
            ),
            app.PRIVACY_CARRIER_ID,
        )

    def test_existing_zto_without_reset_stays_in_zto_paper_profile(self):
        plan = _plan(
            ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
            app.TARGET_CARRIER_ID,
            app.TARGET_CARRIER_NAME,
        )

        self.assertEqual(app.plan_print_profile(plan), app.TARGET_CARRIER_ID)
        self.assertEqual(api.candidate_print_profile(plan), app.TARGET_CARRIER_ID)

    def test_existing_zto_get_waybill_preflight_preserves_current_carrier(self):
        plan = _plan(
            ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
            app.TARGET_CARRIER_ID,
            app.TARGET_CARRIER_NAME,
        )
        plan["privacy_required"] = False
        plan["weight_kg"] = 1.0
        readback = {
            "found": True,
            "status": "WaitConfirm",
            "action_history_complete": True,
            "has_ship_action": False,
            "has_print_action": False,
            "is_print_express": False,
            "delivery_hold_marked": False,
            "delivery_hold_reasons": [],
            "inventory_complete": False,
            "inventory_sufficient": False,
            "inventory_validation_errors": ["库存服务不可用"],
            "privacy_required": False,
            "weight_kg": 1.0,
            "warehouse_id": app.TARGET_WAREHOUSE_ID,
            "shop_name": "测试店铺",
            "has_waybill": False,
            "has_manual_carrier_action": True,
            "carrier_id": app.TARGET_CARRIER_ID,
            "carrier_name": app.TARGET_CARRIER_NAME,
        }

        app.AutomationEngine._preflight(readback, "GET_WAYBILL", plan)
        wrong = dict(readback, carrier_id=app.SOURCE_CARRIER_ID, carrier_name=app.SOURCE_CARRIER_NAME)
        with self.assertRaises(app.PermanentJobError):
            app.AutomationEngine._preflight(wrong, "GET_WAYBILL", plan)

    def test_live_preflight_rejects_stale_weight_shop_or_warehouse_route(self):
        plan = _plan(
            ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
            app.TARGET_CARRIER_ID,
            app.TARGET_CARRIER_NAME,
        )
        plan.update(weight_kg=1.0, privacy_required=False)
        readback = {
            "found": True,
            "status": "WaitConfirm",
            "action_history_complete": True,
            "has_ship_action": False,
            "has_print_action": False,
            "is_print_express": False,
            "delivery_hold_marked": False,
            "delivery_hold_reasons": [],
            "privacy_required": False,
            "has_waybill": False,
            "has_manual_carrier_action": False,
            "carrier_id": app.TARGET_CARRIER_ID,
            "carrier_name": app.TARGET_CARRIER_NAME,
            "weight_kg": 1.0,
            "warehouse_id": app.TARGET_WAREHOUSE_ID,
            "shop_name": "普通店铺",
        }

        app.AutomationEngine._preflight(readback, "GET_WAYBILL", plan)
        for changed in (
            dict(readback, weight_kg=1.1),
            dict(readback, shop_name="小红书旗舰店"),
            dict(readback, warehouse_id="99999999"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(app.PermanentJobError):
                    app.AutomationEngine._preflight(changed, "GET_WAYBILL", plan)

    def test_final_print_guard_pins_the_same_waybill_suffix(self):
        plan = _plan(
            ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
            app.TARGET_CARRIER_ID,
            app.TARGET_CARRIER_NAME,
        )
        plan.update(weight_kg=1.0, privacy_required=False)
        readback = {
            "found": True,
            "o_id": "1",
            "io_id": "2",
            "status": "WaitConfirm",
            "action_history_complete": True,
            "has_ship_action": False,
            "has_print_action": False,
            "is_print_express": False,
            "delivery_hold_marked": False,
            "delivery_hold_reasons": [],
            "privacy_required": False,
            "privacy_source": "remark",
            "has_waybill": True,
            "waybill_suffix": "1111",
            "waybill_fingerprint": "a" * 64,
            "has_manual_carrier_action": False,
            "carrier_id": app.TARGET_CARRIER_ID,
            "carrier_name": app.TARGET_CARRIER_NAME,
            "weight_kg": 1.0,
            "warehouse_id": app.TARGET_WAREHOUSE_ID,
            "shop_name": "普通店铺",
        }

        app.AutomationEngine._validate_final_print_readback(
            plan, readback, "1", "2", "1111", "a" * 64
        )
        with self.assertRaisesRegex(app.PermanentJobError, "运单身份已变化"):
            app.AutomationEngine._validate_final_print_readback(
                plan,
                dict(readback, waybill_suffix="2222"),
                "1",
                "2",
                "1111",
                "a" * 64,
            )
        with self.assertRaisesRegex(app.PermanentJobError, "完整运单指纹已变化"):
            app.AutomationEngine._validate_final_print_readback(
                plan,
                dict(readback, waybill_fingerprint="b" * 64),
                "1",
                "2",
                "1111",
                "a" * 64,
            )

    def test_multiple_target_carriers_fail_closed(self):
        with self.assertRaises(app.PermanentJobError):
            app.plan_print_profile(
                _plan(
                    [
                        f"RESET_CARRIER_AND_GET_WAYBILL:{app.TARGET_CARRIER_ID}:{app.TARGET_CARRIER_NAME}",
                        f"RESET_CARRIER_AND_GET_WAYBILL:{app.PRIVACY_CARRIER_ID}:{app.PRIVACY_CARRIER_NAME}",
                    ]
                )
            )

    def test_server_filters_candidates_before_claiming(self):
        sto = _plan(["GET_WAYBILL", "PRINT_EXPRESS"])
        reset_to_sto = _plan(
            [
                f"RESET_CARRIER_AND_GET_WAYBILL:{app.SOURCE_CARRIER_ID}:{app.SOURCE_CARRIER_NAME}",
                "PRINT_EXPRESS",
            ],
            app.TARGET_CARRIER_ID,
            app.TARGET_CARRIER_NAME,
        )
        zto = _plan(
            [
                f"RESET_CARRIER_AND_GET_WAYBILL:{app.TARGET_CARRIER_ID}:{app.TARGET_CARRIER_NAME}",
                "PRINT_EXPRESS",
            ]
        )
        privacy = _plan(
            [
                f"RESET_CARRIER_AND_GET_WAYBILL:{app.PRIVACY_CARRIER_ID}:{app.PRIVACY_CARRIER_NAME}",
                "PRINT_EXPRESS",
            ]
        )
        sto.update(o_id="10", io_id="20")
        reset_to_sto.update(o_id="13", io_id="23")
        zto.update(o_id="11", io_id="21")
        privacy.update(o_id="12", io_id="22")
        raw = {
            "generated_at": "2026-08-24T00:00:00Z",
            "scope": {},
            "counts": {"selected": 4},
            "selected": [sto, reset_to_sto, zto, privacy],
            "blocked_preview": [],
            "blocked_reason_counts": {},
            "guardrails": [],
        }

        class Store:
            claimed = []

            def active_for_workstation(self, _workstation):
                return []

            def claim(self, o_id, io_id, _workstation):
                self.claimed.append((o_id, io_id))
                return SimpleNamespace(
                    o_id=o_id,
                    io_id=io_id,
                    claim_token="x" * 32,
                    expires_epoch=1_900_000_000.0,
                    lease_ttl_seconds=300,
                )

        store = Store()
        with mock.patch.object(api, "get_plan_pool", return_value=raw):
            result = api._plan(
                {
                    "workstation_id": "workstation-123",
                    "exclude": [],
                    "max_candidates": 10,
                    "print_profile": app.TARGET_CARRIER_ID,
                },
                store,
            )
        self.assertEqual(store.claimed, [("11", "21")])
        self.assertEqual(result["print_profile"], app.TARGET_CARRIER_ID)
        self.assertEqual([item["o_id"] for item in result["selected"]], ["11"])

        store.claimed = []
        with mock.patch.object(api, "get_plan_pool", return_value=raw):
            result = api._plan(
                {
                    "workstation_id": "workstation-123",
                    "exclude": [],
                    "max_candidates": 10,
                    "print_profile": app.SOURCE_CARRIER_ID,
                },
                store,
            )
        self.assertEqual(store.claimed, [("10", "20"), ("13", "23")])
        self.assertEqual(
            [item["o_id"] for item in result["selected"]], ["10", "13"]
        )

    def test_server_sorts_by_product_then_sku(self):
        def candidate(o_id, product_id, sku_id):
            return {
                "o_id": o_id,
                "items": [{"product_id": product_id, "sku_id": sku_id}],
            }

        rows = [
            candidate("1", "B", "B-70"),
            candidate("2", "A", "A-80"),
            candidate("3", "A", "A-70"),
            candidate("4", "", ""),
            candidate("5", "B", "B-80"),
        ]

        grouped = api.group_candidates_by_product(rows)

        self.assertEqual(
            [item["o_id"] for item in grouped],
            ["3", "2", "1", "5", "4"],
        )

    def test_server_sorts_sku_naturally_within_product(self):
        def candidate(o_id, sku_id):
            return {"o_id": o_id, "items": [{"product_id": "A", "sku_id": sku_id}]}

        rows = [
            candidate("1", "A-10"),
            candidate("2", "A-2"),
            candidate("3", "A-10"),
            candidate("4", "A-2"),
        ]

        grouped = api.group_candidates_by_product(rows)

        self.assertEqual(
            [item["o_id"] for item in grouped],
            ["2", "4", "1", "3"],
        )

    def test_write_readback_retries_temporary_not_found_without_reclick(self):
        responses = iter(
            [
                {"found": False, "o_id": "11", "io_id": "21"},
                {"found": False, "o_id": "11", "io_id": "21"},
                {
                    "found": True,
                    "o_id": "11",
                    "io_id": "21",
                    "status": "WaitConfirm",
                    "has_ship_action": False,
                    "has_waybill": True,
                },
            ]
        )

        class Planner:
            renewals = 0

            def inspect(self, *_args):
                return next(responses)

            def renew(self, *_args):
                self.renewals += 1

        planner = Planner()
        engine = object.__new__(app.AutomationEngine)
        with mock.patch.object(app.time, "sleep"):
            result = engine._poll_readback(
                planner,
                "11",
                "21",
                "x" * 32,
                lambda data: data.get("has_waybill") is True,
                "获取电子面单",
                attempts=3,
            )
        self.assertTrue(result["has_waybill"])
        self.assertEqual(planner.renewals, 2)


if __name__ == "__main__":
    unittest.main()
