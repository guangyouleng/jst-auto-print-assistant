import inspect
import hashlib
import re
import sys
import types
import unittest
from unittest import mock

import jst_auto_print_app as app


class _SearchSubmitted(RuntimeError):
    pass


class _BrowserDouble:
    def __init__(self, contexts, *, connected=True):
        self.contexts = contexts
        self._connected = connected

    def is_connected(self):
        return self._connected


class _Store:
    def __init__(self):
        self.updates = []

    def update_job(self, o_id, io_id, **fields):
        self.updates.append((o_id, io_id, fields))
        return True


class _RoutingStore:
    def __init__(self):
        self.next_job_calls = 0
        self.updates = []
        self.exact = {
            "o_id": "6788192",
            "io_id": "13539937",
            "status": "PREPARING",
            "step_index": 0,
        }
        self.wrong_oldest = {
            "o_id": "6788331",
            "io_id": "13539939",
            "status": "PENDING",
            "step_index": 0,
        }

    def get_job(self, o_id, io_id):
        if (str(o_id), str(io_id)) == ("6788192", "13539937"):
            return dict(self.exact)
        return None

    def next_job(self):
        self.next_job_calls += 1
        return dict(self.wrong_oldest)

    def update_job(self, o_id, io_id, **fields):
        self.updates.append((str(o_id), str(io_id), fields))
        return True


class _LocatorList:
    def __init__(self, items):
        self.items = list(items)

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _VisibleNode:
    def is_visible(self):
        return True


class _DialogCloseNode(_VisibleNode):
    def __init__(self, host):
        self.host = host
        self.actionability_probes = 0
        self.clicks = 0

    def is_enabled(self):
        return True

    def evaluate(self, _script):
        return False

    def click(self, trial=False, timeout=None):
        if trial:
            self.actionability_probes += 1
            return
        self.clicks += 1
        self.host.visible = False


class _DialogHost(_VisibleNode):
    def __init__(self, close_count=1):
        self.visible = True
        self.close_nodes = [_DialogCloseNode(self) for _ in range(close_count)]

    def is_visible(self):
        return self.visible

    def locator(self, selector):
        if "panel-tool-close" in selector:
            return _LocatorList(self.close_nodes)
        return _LocatorList([])


class _DialogFrame:
    def __init__(self, url, host, *, confirmation=False):
        self.url = url
        self.host = host
        self.confirmation = confirmation

    def is_detached(self):
        return False

    def frame_element(self):
        return self.host

    def locator(self, selector):
        if selector == "#confirm_confirm" and self.confirmation:
            return _LocatorList([_VisibleNode()])
        return _LocatorList([])


class _TextNode(_VisibleNode):
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _Checkbox(_VisibleNode):
    def __init__(self, checked=False):
        self.checked = checked

    def is_checked(self):
        return self.checked

    def check(self, force=False):
        self.checked = True

    def uncheck(self, force=False):
        self.checked = False


class _DetachedCheckbox(_Checkbox):
    """A JTable checkbox locator invalidated by the row's check redraw."""

    def __init__(self):
        super().__init__(False)
        self.detached = False

    def is_checked(self):
        if self.detached:
            raise RuntimeError("detached from redrawn JTable row")
        return super().is_checked()


class _JTableRow(_VisibleNode):
    def __init__(self, index, o_id, io_id, checked=False, visible=True):
        self.index = str(index)
        self.o_cell = _TextNode(o_id)
        self.io_cell = _TextNode(io_id)
        self.checkbox = _Checkbox(checked)
        self.visible = visible

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        return {
            "class": "_jt_row _jt_rh",
            "index": self.index,
            "id": "",
        }.get(name)

    def locator(self, selector):
        if selector == "._jt_cell_o_id[data-id='o_id']":
            return _LocatorList([self.o_cell])
        if selector == "._jt_cell_io_id[data-id='io_id']":
            return _LocatorList([self.io_cell])
        if "_jt_cell_checked" in selector:
            return _LocatorList([self.checkbox])
        return _LocatorList([])


class _JTableScope:
    def __init__(self, rows):
        self.rows = rows

    def locator(self, selector):
        if selector == "._jt_row._jt_rh[index]":
            return _LocatorList(self.rows)
        if selector == (
            "._jt_row._jt_rh ._jt_cell_checked[data-id='checked'] "
            "input._jt_cbx[type='checkbox']:checked"
        ):
            return _LocatorList(
                [row.checkbox for row in self.rows if row.checkbox.checked]
            )
        row_match = re.fullmatch(r"\._jt_row\._jt_rh\[index='(\d+)'\]", selector)
        if row_match:
            return _LocatorList(
                [row for row in self.rows if row.index == row_match.group(1)]
            )
        match = re.search(r"_jt_rh\[index='(\d+)'\]", selector)
        if match and "_jt_cell_checked" in selector:
            boxes = [
                row.checkbox for row in self.rows if row.index == match.group(1)
            ]
            return _LocatorList(boxes)
        return _LocatorList([])


class DomLookupSafetyTests(unittest.TestCase):
    """Regression coverage for the retired, non-production DOM adapter."""

    @classmethod
    def setUpClass(cls):
        cls._legacy_browser_patch = mock.patch.object(
            app, "JSTBrowser", app._LegacyPlaywrightJSTBrowser
        )
        cls._legacy_browser_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._legacy_browser_patch.stop()

    def test_slow_2000_row_clear_finishes_before_next_search(self):
        """Regression: the live 2,000-row grid can take >12s to clear."""

        clock = {"now": 0.0}

        class SlowGridFrame:
            def is_detached(self):
                return False

            def evaluate(self, _script):
                loading = clock["now"] < 14.0
                return {
                    "active": 1 if loading else 0,
                    "masks": 1 if loading else 0,
                }

        def advance(seconds):
            clock["now"] += seconds

        with mock.patch.object(
            app.time, "time", side_effect=lambda: clock["now"]
        ), mock.patch.object(app.time, "sleep", side_effect=advance):
            app.JSTBrowser._wait_grid_idle(
                SlowGridFrame(), "清空筛选页面稳定"
            )

        self.assertGreaterEqual(clock["now"], 14.0 + app.GRID_IDLE_QUIET_SECONDS)
        self.assertGreaterEqual(app.GRID_IDLE_TIMEOUT_SECONDS, 30.0)

    def test_dom_lookup_readiness_timeouts_cover_slow_live_grid(self):
        self.assertGreaterEqual(app.GRID_SEARCH_IDLE_TIMEOUT_SECONDS, 30.0)
        self.assertGreaterEqual(app.EXACT_ROW_TIMEOUT_SECONDS, 15.0)
        self.assertGreaterEqual(app.BATCH_EXACT_ROWS_TIMEOUT_SECONDS, 20.0)
        self.assertIn(
            "GRID_SEARCH_IDLE_TIMEOUT_SECONDS",
            inspect.getsource(app.JSTBrowser._submit_order_search),
        )

    def test_grid_timeout_reports_pending_request_and_mask_counts(self):
        clock = {"now": 0.0}

        class BusyGridFrame:
            def is_detached(self):
                return False

            def evaluate(self, _script):
                return {"active": 2, "masks": 1}

        def advance(seconds):
            clock["now"] += seconds

        with mock.patch.object(
            app.time, "time", side_effect=lambda: clock["now"]
        ), mock.patch.object(app.time, "sleep", side_effect=advance):
            with self.assertRaises(app.OrderRowNotReady) as raised:
                app.JSTBrowser._wait_grid_idle(
                    BusyGridFrame(), "精确查单", timeout=1.0
                )

        message = str(raised.exception)
        self.assertIn("未完成请求 2", message)
        self.assertIn("可见加载遮罩 1", message)

    def test_known_stale_carrier_dialog_is_closed_before_search(self):
        browser = object.__new__(app.JSTBrowser)
        main = object()
        host = _DialogHost()
        carrier = _DialogFrame(
            "https://www.erp321.com/app/wms/saleout/SelectlogLstics_company.aspx",
            host,
        )
        browser.page = mock.Mock(main_frame=main, frames=[main, carrier])

        closed = browser._dismiss_stale_carrier_dialog()

        self.assertTrue(closed)
        self.assertFalse(host.visible)
        self.assertEqual(host.close_nodes[0].actionability_probes, 1)
        self.assertEqual(host.close_nodes[0].clicks, 1)

    def test_confirmation_dialog_is_never_auto_closed_before_search(self):
        browser = object.__new__(app.JSTBrowser)
        main = object()
        host = _DialogHost()
        confirmation = _DialogFrame(
            "https://www.erp321.com/epaas-dialog-frame.html",
            host,
            confirmation=True,
        )
        browser.page = mock.Mock(main_frame=main, frames=[main, confirmation])

        with self.assertRaises(app.SafetyStop) as raised:
            browser._dismiss_stale_carrier_dialog()

        self.assertIn("确认框", str(raised.exception))
        self.assertEqual(host.close_nodes[0].clicks, 0)

    def test_filter_reset_dismisses_stale_carrier_dialog_first(self):
        source = inspect.getsource(app.JSTBrowser._reset_search_filters)

        self.assertIn("self._dismiss_stale_carrier_dialog()", source)

    def test_jtable_exact_pair_uses_dedicated_identity_cells(self):
        browser = object.__new__(app.JSTBrowser)
        row = _JTableRow("0", "6788422复", "13540000")

        matched = browser._find_exact_jtable_row(
            _JTableScope([row]), "6788422", "13540000", True
        )

        self.assertIs(matched[0], row)
        self.assertIs(matched[1], row.checkbox)

    def test_jtable_selection_clears_other_rows_and_checks_only_target(self):
        browser = object.__new__(app.JSTBrowser)
        old = _JTableRow("0", "6788422", "13540000", checked=True)
        target = _JTableRow("1", "6788548", "13540053")

        state = browser._jtable_selection_state(
            _JTableScope([old, target]), ("1",), "check"
        )

        self.assertFalse(old.checkbox.checked)
        self.assertTrue(target.checkbox.checked)
        self.assertEqual(state["checked_indices"], ["1"])

    def test_jtable_hidden_checked_row_blocks_batch_operation(self):
        browser = object.__new__(app.JSTBrowser)
        target = _JTableRow("1", "6788548", "13540053")
        hidden_old = _JTableRow(
            "2", "6788550", "13540054", checked=True, visible=False
        )

        with self.assertRaisesRegex(app.SafetyStop, "隐藏或无法映射"):
            browser._jtable_selection_state(
                _JTableScope([target, hidden_old]), ("1",), "check"
            )

    def test_select_order_does_not_reuse_checkbox_detached_by_jtable_redraw(self):
        browser = object.__new__(app.JSTBrowser)
        frame = object()
        row = _JTableRow("1", "6788548", "13540053")
        stale = _DetachedCheckbox()
        row.checkbox = stale
        scope = object()
        browser._express_frame = lambda: frame
        browser._reset_sidebar_scope = lambda _frame: None
        browser._reset_search_filters = lambda _frame: object()
        browser._submit_order_search = lambda *_args: None
        browser._wait_exact_row = lambda *_args, **_kwargs: (row, stale)
        browser._selection_scope = lambda *_args: scope
        checked_nodes = []
        browser._checked_data_checkboxes = lambda _scope: list(checked_nodes)

        def selection_state(_scope, targets, action, grid_kind):
            self.assertEqual(targets, ("1",))
            self.assertEqual(grid_kind, "jtable")
            if action == "clear":
                return {"checked_indices": [], "selected_indices": []}
            if action == "check":
                stale.detached = True
                checked_nodes.append(object())
                return {"checked_indices": ["1"], "selected_indices": []}
            raise AssertionError(action)

        browser._grid_selection_state = selection_state

        selected = browser.select_order("6788548", "13540053", True)

        self.assertEqual(selected.target_index, "1")

    def test_select_order_searches_before_scanning_large_current_grid(self):
        browser = object.__new__(app.JSTBrowser)
        frame = object()
        row = _JTableRow("0", "6788422", "13540000")
        scope = object()
        submitted = []
        browser._express_frame = lambda: frame
        browser._reset_sidebar_scope = lambda _frame: None
        browser._reset_search_filters = lambda _frame: object()
        browser._submit_order_search = lambda *_args: submitted.append(True)
        browser._find_exact_jtable_row = lambda *_args: (_ for _ in ()).throw(
            AssertionError("must not scan the unfiltered JTable")
        )
        browser._find_exact_anchor_row = lambda *_args: None
        browser._find_exact_easyui_row = lambda *_args: None
        browser._wait_exact_row = lambda *_args, **_kwargs: (row, row.checkbox)
        browser._selection_scope = lambda *_args: scope
        checked_nodes = []
        browser._checked_data_checkboxes = lambda _scope: list(checked_nodes)

        def grid_state(_scope, _targets, action, _kind):
            if action == "clear":
                checked_nodes.clear()
                indices = []
            else:
                checked_nodes[:] = [object()]
                indices = ["0"]
            return {"checked_indices": indices, "selected_indices": []}

        browser._grid_selection_state = grid_state

        selected = browser.select_order("6788422", "13540000", True)

        self.assertTrue(submitted)
        self.assertEqual(selected.target_index, "0")

    def test_click_boundary_rejects_same_index_after_order_identity_changes(self):
        browser = object.__new__(app.JSTBrowser)
        changed = _JTableRow("0", "6788999", "13549999", checked=True)
        selected = app.SelectedOrder(
            frame=object(),
            checkbox=_DetachedCheckbox(),
            target_index="0",
            selection_scope=_JTableScope([changed]),
            grid_kind="jtable",
            o_id="6788422",
            io_id="13540000",
        )

        with self.assertRaises(app.SafetyStop) as raised:
            browser._verify_selected_order(selected)

        self.assertIn("6788422/13540000", str(raised.exception))

    def test_batch_click_boundary_identifies_changed_second_order(self):
        browser = object.__new__(app.JSTBrowser)
        first = _JTableRow("0", "6788422", "13540000", checked=True)
        changed_second = _JTableRow("1", "6788999", "13549999", checked=True)
        selected = app.SelectedBatch(
            frame=object(),
            selection_scope=_JTableScope([first, changed_second]),
            target_indices=("0", "1"),
            identities=(
                ("6788422", "13540000"),
                ("6788423", "13540001"),
            ),
            grid_kind="jtable",
        )

        with self.assertRaises(app.BatchCandidateChanged) as raised:
            browser._verify_selected_identity(selected)

        self.assertEqual(
            raised.exception._jst_job_identity,
            ("6788423", "13540001"),
        )
        self.assertEqual(
            (raised.exception.job["o_id"], raised.exception.job["io_id"]),
            ("6788423", "13540001"),
        )

    def test_old_paused_job_does_not_hide_print_ready_jobs(self):
        paused = {
            "status": "PAUSED",
            "step_index": 0,
            "plan": {"steps": ["GET_WAYBILL", "PRINT_EXPRESS"]},
        }
        ready = {
            "status": "PENDING",
            "step_index": 1,
            "plan": {"steps": ["GET_WAYBILL", "PRINT_EXPRESS"]},
        }

        recovery, preparation, print_ready = app.AutomationEngine._group_active_jobs(
            [paused, ready]
        )

        self.assertEqual(recovery, [paused])
        self.assertEqual(preparation, [])
        self.assertEqual(print_ready, [ready])

    def test_partial_settlement_prints_before_starting_another_waybill_group(self):
        self.assertTrue(
            app.AutomationEngine._should_print_before_preparation(
                [{"status": "RUNNING"}], [{"status": "PENDING"}]
            )
        )

    def test_ready_print_batch_runs_before_more_waybill_preparation(self):
        self.assertTrue(
            app.AutomationEngine._should_print_before_preparation(
                [], [{"status": "PENDING"}, {"status": "PENDING"}]
            )
        )
        self.assertFalse(
            app.AutomationEngine._should_print_before_preparation(
                [], [{"status": "PENDING"}]
            )
        )

    def test_effect_buttons_target_enabled_parent_and_probe_actionability(self):
        get_waybill_source = inspect.getsource(app.JSTBrowser.get_waybill)
        batch_waybill_source = inspect.getsource(app.JSTBrowser.get_waybill_batch)
        print_source = inspect.getsource(app.JSTBrowser.print_express)

        self.assertIn('frame, "#GETExpress_Btn", "获取单号"', get_waybill_source)
        self.assertNotIn("#GETExpress_Btn > .ding_db_txt", get_waybill_source)
        self.assertIn("self.select_orders(identities)", batch_waybill_source)
        self.assertIn('"#GETExpress_Btn", "批量获取单号"', batch_waybill_source)
        self.assertIn('frame, "#printExpress_Btn", "打印快递单"', print_source)
        self.assertIn("_prove_button_actionable", print_source)

    def test_preparation_batches_only_identical_waybill_actions(self):
        get_a = {
            "status": "PENDING",
            "step_index": 0,
            "plan": {"steps": ["GET_WAYBILL", "PRINT_EXPRESS"]},
        }
        reset = {
            "status": "PENDING",
            "step_index": 0,
            "plan": {
                "steps": [
                    f"RESET_CARRIER_AND_GET_WAYBILL:{app.TARGET_CARRIER_ID}:"
                    f"{app.TARGET_CARRIER_NAME}",
                    "PRINT_EXPRESS",
                ]
            },
        }
        get_b = {
            "status": "PENDING",
            "step_index": 0,
            "plan": {"steps": ["GET_WAYBILL", "PRINT_EXPRESS"]},
        }

        batch = app.AutomationEngine._next_preparation_batch(
            [get_a, reset, get_b]
        )

        self.assertEqual(batch, [get_a, get_b])

    def test_print_queue_sorts_by_product_then_sku(self):
        def job(label, product_id, sku_id):
            item = {"product_id": product_id, "sku_id": sku_id, "sku_name": label}
            return {"label": label, "plan": {"items": [item]}}

        jobs = [
            job("B-70", "B", "B-70"),
            job("A-80", "A", "A-80"),
            job("A-70", "A", "A-70"),
            job("B-80", "B", "B-80"),
        ]

        ordered = app.AutomationEngine._prioritize_product_groups(jobs)

        self.assertEqual(
            [item["label"] for item in ordered],
            ["A-70", "A-80", "B-70", "B-80"],
        )

    def test_print_queue_sorts_sku_naturally_within_product(self):
        def job(label, sku_id):
            item = {"product_id": "A", "sku_id": sku_id, "sku_name": label}
            return {"label": label, "plan": {"items": [item]}}

        jobs = [
            job("A-10-a", "A-10"),
            job("A-2-a", "A-2"),
            job("A-10-b", "A-10"),
            job("A-2-b", "A-2"),
        ]

        ordered = app.AutomationEngine._prioritize_product_groups(jobs)

        self.assertEqual(
            [item["label"] for item in ordered],
            ["A-2-a", "A-2-b", "A-10-a", "A-10-b"],
        )

    def test_print_queue_without_product_or_sku_keeps_normal_order(self):
        jobs = [
            {"label": label, "plan": {"items": [{"sku_name": label}]}}
            for label in ("first", "second", "third")
        ]

        self.assertEqual(
            app.AutomationEngine._prioritize_product_groups(jobs), jobs
        )

    def test_filter_and_dialog_actions_use_clickable_parent_buttons(self):
        reset_source = inspect.getsource(app.JSTBrowser._reset_search_filters)
        search_source = inspect.getsource(app.JSTBrowser._submit_order_search)
        carrier_source = inspect.getsource(app.JSTBrowser._choose_target_carrier)

        self.assertIn('frame, ".btn_search_reset", "清空筛选"', reset_source)
        self.assertIn('frame, ".btn_search", "搜索订单"', search_source)
        self.assertIn("input[type='radio'][name='lc']", carrier_source)
        self.assertIn("span.btn_1.big[onclick*='Confirm']", carrier_source)
        self.assertIn('frame.locator("#confirm_confirm")', carrier_source)

    def test_live_page_stable_ids_and_loading_mask_are_pinned(self):
        input_source = inspect.getsource(app.JSTBrowser._order_input)
        scope_source = inspect.getsource(app.JSTBrowser._reset_sidebar_scope)
        idle_source = inspect.getsource(app.JSTBrowser._wait_grid_idle)
        reset_source = inspect.getsource(app.JSTBrowser.reset_carrier)

        self.assertIn('frame.locator("#o_id")', input_source)
        self.assertIn('frame.locator("#lc_id_1")', scope_source)
        self.assertIn(".panel-loading, .wait", idle_source)
        self.assertIn('frame, "#ResetLc_Btn", "重设快递"', reset_source)

    def test_only_observed_reset_carrier_warnings_are_whitelisted(self):
        self.assertEqual(
            app.JSTBrowser._reset_warning_kind(
                "订单：6789727已经预发货成功，"
                "如需继续设定面单号请点击确定按钮",
                "6789727",
            ),
            "PRESHIPPED_ORDER",
        )
        self.assertEqual(
            app.JSTBrowser._reset_warning_kind(
                "快递单(运单)号：773438059044157已经打印过面单 "
                "请确认是否重设快递公司？",
                "6789727",
            ),
            "PRINTED_WAYBILL",
        )
        self.assertIsNone(
            app.JSTBrowser._reset_warning_kind(
                "订单：6789728已经预发货成功，"
                "如需继续设定面单号请点击确定按钮",
                "6789727",
            )
        )
        self.assertIsNone(
            app.JSTBrowser._reset_warning_kind("确定删除当前订单？", "6789727")
        )

    def test_reset_warning_is_classified_but_never_auto_confirmed(self):
        source = inspect.getsource(app.JSTBrowser._cancel_known_reset_warning)

        self.assertIn('dialog_frame.locator("#confirm_confirm")', source)
        self.assertIn('dialog_frame.locator("#confirm_close")', source)
        self.assertIn('dialog_frame.locator("#confirm_top")', source)
        self.assertNotIn('dialog_frame.locator("body")', source)
        self.assertIn("遇到未授权的确认框", source)
        self.assertIn("cancel.click()", source)
        self.assertNotIn("confirm.click()", source)

    def test_printed_or_preshipped_reset_warning_never_clicks_confirm(self):
        browser = object.__new__(app.JSTBrowser)

        class Button(_VisibleNode):
            def __init__(self):
                self.clicks = 0

            def is_enabled(self):
                return True

            def evaluate(self, _script):
                return False

            def click(self, trial=False, timeout=None):
                if not trial:
                    self.clicks += 1

        class Dialog:
            def __init__(self, message):
                self.confirm = Button()
                self.cancel = Button()
                self.prompt = _TextNode(message)

            def locator(self, selector):
                return {
                    "#confirm_confirm": _LocatorList([self.confirm]),
                    "#confirm_close": _LocatorList([self.cancel]),
                    "#confirm_top": _LocatorList([self.prompt]),
                }.get(selector, _LocatorList([]))

        selected = type("Selected", (), {"o_id": "6789727"})()
        messages = (
            "订单：6789727已经预发货成功，如需继续设定面单号请点击确定按钮",
            "快递单(运单)号：773438059044157已经打印过面单 请确认是否重设快递公司？",
        )
        for message in messages:
            with self.subTest(message=message):
                dialog = Dialog(message)
                with self.assertRaises(app.SafetyStop):
                    browser._confirm_reset_warning(selected, dialog, set())
                self.assertEqual(dialog.confirm.clicks, 0)
                self.assertEqual(dialog.cancel.clicks, 1)

    def test_incomplete_batch_search_falls_back_to_verified_single_print(self):
        engine = object.__new__(app.AutomationEngine)
        engine.store = _Store()
        engine.action_lock = app.threading.RLock()
        engine.dom_lookup_retries = {}
        engine._settings = lambda: app.Settings(
            allow_write=True,
            allow_print=True,
            print_profile=app.SOURCE_CARRIER_ID,
        )
        engine._claim = lambda plan: (
            str(plan["o_id"]),
            str(plan["io_id"]),
            str(plan["claim_token"]),
        )
        engine._renew_step = lambda *_args: None
        engine._inspect = lambda _planner, plan: {
            "o_id": str(plan["o_id"]),
            "io_id": str(plan["io_id"]),
            "has_waybill": True,
            "waybill_suffix": str(plan["io_id"])[-4:],
            "waybill_fingerprint": hashlib.sha256(
                str(plan["io_id"]).encode("utf-8")
            ).hexdigest(),
        }
        engine._require_job_identity = lambda *_args: None
        engine._is_allowlisted_terminal_status = lambda _value: False
        engine._validate_items_unchanged = lambda *_args: None
        engine._validate_final_print_readback = lambda *_args: None
        engine._event = lambda *_args, **_kwargs: None
        engine._require_operator_permission = lambda: None

        class Browser:
            def print_express_batch(self, *_args, **_kwargs):
                raise app.BatchSearchUnsupported("逗号批量查询未完整返回")

        engine._browser_for = lambda _settings: Browser()
        processed = []
        engine._process_job = lambda _planner, job: processed.append(job)
        jobs = [
            {
                "o_id": str(6788422 + index),
                "io_id": str(13540000 + index),
                "status": "PENDING",
                "step_index": 0,
                "plan": {
                    "o_id": str(6788422 + index),
                    "io_id": str(13540000 + index),
                    "claim_token": "x" * 32,
                    "steps": ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
                    "outbound_identity_unique": True,
                },
            }
            for index in range(2)
        ]

        with mock.patch.object(app, "print_service_online", return_value=True):
            engine._process_print_batch(object(), jobs)

        self.assertEqual(processed, [jobs[0]])
        self.assertEqual(
            [update[2]["status"] for update in engine.store.updates],
            ["PREPARING", "PREPARING", "PENDING", "PENDING"],
        )

    def test_two_waybill_jobs_use_one_real_batch_click(self):
        class Store(_Store):
            running_batches = []

            def mark_batch_running(self, jobs):
                self.running_batches.append(list(jobs))
                return True

        engine = object.__new__(app.AutomationEngine)
        engine.store = Store()
        engine.action_lock = app.threading.RLock()
        engine.dom_lookup_retries = {}
        engine._settings = lambda: app.Settings(
            allow_write=True,
            allow_print=True,
            print_profile=app.SOURCE_CARRIER_ID,
        )
        engine._claim = lambda plan: (
            str(plan["o_id"]),
            str(plan["io_id"]),
            str(plan["claim_token"]),
        )
        engine._renew_step = lambda *_args: None
        engine._inspect = lambda _planner, plan: {
            "o_id": str(plan["o_id"]),
            "io_id": str(plan["io_id"]),
        }
        engine._require_job_identity = lambda *_args: None
        engine._is_allowlisted_terminal_status = lambda _value: False
        engine._require_waitconfirm_status = lambda *_args: None
        engine._validate_items_unchanged = lambda *_args: None
        engine._preflight = lambda *_args: None
        engine._event = lambda *_args, **_kwargs: None
        engine._require_operator_permission = lambda: None
        engine._poll_readback = lambda *_args, **_kwargs: {
            "waybill_suffix": "1234",
            "waybill_fingerprint": "a" * 64,
        }

        clicked = []

        class Browser:
            def get_waybill_batch(self, identities, **kwargs):
                clicked.append(list(identities))
                kwargs["before_click"]()
                kwargs["mark_running"]()
                kwargs["final_guard"]()

        engine._browser_for = lambda _settings: Browser()
        jobs = [
            {
                "o_id": str(6788422 + index),
                "io_id": str(13540000 + index),
                "status": "PENDING",
                "step_index": 0,
                "plan": {
                    "o_id": str(6788422 + index),
                    "io_id": str(13540000 + index),
                    "claim_token": "x" * 32,
                    "steps": [
                        "GET_WAYBILL",
                        "PRINT_EXPRESS",
                        "STOP_BEFORE_PRESHIP",
                    ],
                    "outbound_identity_unique": True,
                    "items": [],
                },
            }
            for index in range(2)
        ]

        engine._process_waybill_batch(object(), jobs)

        self.assertEqual(
            clicked,
            [[("6788422", "13540000"), ("6788423", "13540001")]],
        )
        self.assertEqual(engine.store.running_batches, [jobs])
        self.assertEqual(
            [update[2]["status"] for update in engine.store.updates],
            ["PREPARING", "PREPARING", "PENDING", "PENDING"],
        )

    def test_carrier_dialog_is_scoped_by_frame_and_exact_id_name_value(self):
        source = inspect.getsource(app.JSTBrowser._choose_target_carrier)
        self.assertIn('_is_jst_frame_path(url, "carrier")', source)
        self.assertIn(
            'expected_value = f"{target_carrier_id},{target_carrier_name}"',
            source,
        )
        self.assertIn("frame.frame_element().is_visible()", source)

    def test_jtable_identity_does_not_accept_generic_index_attribute(self):
        row = _JTableRow("7", "6788422", "13540000")
        self.assertEqual(
            app.JSTBrowser._row_identity(row), ("jtable-index", "7")
        )

        row.get_attribute = lambda name: "other-row" if name == "class" else (
            "7" if name == "index" else None
        )
        self.assertIsNone(app.JSTBrowser._row_identity(row))

    def test_exception_is_routed_to_attempted_order_not_oldest_queue_order(self):
        engine = object.__new__(app.AutomationEngine)
        engine.store = _RoutingStore()
        attempted = {
            "o_id": "6788192",
            "io_id": "13539937",
            "status": "PENDING",
            "step_index": 0,
        }

        active = engine._job_for_exception(attempted)

        self.assertEqual(
            (active["o_id"], active["io_id"]), ("6788192", "13539937")
        )
        self.assertEqual(active["status"], "PREPARING")
        self.assertEqual(engine.store.next_job_calls, 0)

        engine.dom_lookup_retries = {}
        engine._disconnect_browser = lambda: None
        engine._wait = lambda _seconds: True
        engine._event = lambda *_args, **_kwargs: None
        engine.set_status = lambda _message: None
        engine._retry_missing_dom_row(active, "表格暂未呈现")

        self.assertEqual(
            engine.store.updates[0][:2], ("6788192", "13539937")
        )
        self.assertNotIn(("6788331", "13539939"), engine.dom_lookup_retries)

    def test_select_order_clears_filters_even_for_same_iframe(self):
        browser = object.__new__(app.JSTBrowser)
        frame = object()
        search = object()
        browser._cleared_filter_frame = frame
        browser._express_frame = lambda: frame
        browser._reset_sidebar_scope = lambda _frame: None
        browser._find_exact_anchor_row = lambda *_args: None
        browser._find_exact_easyui_row = lambda *_args: None
        browser._order_input = lambda _frame: search
        calls = []

        def reset_filters(target):
            calls.append(target)
            return search

        browser._reset_search_filters = reset_filters
        browser._submit_order_search = lambda *_args: (_ for _ in ()).throw(
            _SearchSubmitted()
        )

        with self.assertRaises(_SearchSubmitted):
            browser.select_order("6787909", "13539546", True)

        self.assertEqual(calls, [frame])

    def test_sidebar_label_fallback_rechecks_carrier_shortcuts_after_refresh(self):
        source = inspect.getsource(app.JSTBrowser._reset_sidebar_scope)

        self.assertGreaterEqual(source.count("_sidebar_label_state("), 2)
        self.assertIn('state.get("checkedShortcutCount") != 0', source)

    def test_easyui_identity_ignores_arbitrary_scalar_fields(self):
        source = inspect.getsource(app.JSTBrowser._easyui_result_indices)
        self.assertNotIn("Object.values(row)", source)
        self.assertNotIn("scalarValues", source)
        self.assertIn("key === 'oid'", source)
        self.assertIn("key === 'ioid'", source)

    def test_wrong_warehouse_is_blocked_before_iframe_use(self):
        browser = object.__new__(app.JSTBrowser)

        class Locator:
            def count(self):
                return 0

        class Page:
            def get_by_text(self, *_args, **_kwargs):
                return Locator()

        with self.assertRaises(app.SafetyStop) as raised:
            browser._verify_warehouse_context(Page())
        self.assertIn("山东汇馨仓库", str(raised.exception))

    def test_page_recovery_reuses_the_only_current_epaas_tab(self):
        browser = object.__new__(app.JSTBrowser)
        navigations = []
        frame_proofs = []

        class Page:
            url = app.JST_HOME_URL

            def __init__(self, context):
                self.context = context

            def goto(self, url, *, wait_until, timeout):
                navigations.append((url, wait_until, timeout))
                self.url = url

            def is_closed(self):
                return False

        context = type("Context", (), {})()
        page = Page(context)
        context.pages = [page]
        browser.browser = _BrowserDouble([context])
        browser.page = page
        browser.context = context
        browser._express_frame = lambda: frame_proofs.append("proved") or object()

        browser.recover_print_page()

        self.assertEqual(
            navigations,
            [(app.JST_HOME_URL, "domcontentloaded", 20_000)],
        )
        self.assertIs(browser.page, page)
        self.assertEqual(frame_proofs, ["proved"])

    def test_page_recovery_does_not_reuse_the_only_ordinary_epaas_tab(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url, *, closed=False):
                self.context = context
                self.url = url
                self.closed = closed
                self.goto_calls = []

            def goto(self, url, **kwargs):
                self.goto_calls.append((url, kwargs))
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []
                self.new_page_calls = 0

            def new_page(self):
                self.new_page_calls += 1
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        inventory = Page(context, "https://www.erp321.com/epaas?n=库存")
        context.pages.append(inventory)
        closed_print_page = Page(context, app.JST_HOME_URL, closed=True)
        browser.browser = _BrowserDouble([context])
        browser.page = closed_print_page
        browser.context = context
        browser._prove_page_ready = lambda _page: None
        browser._express_frame = lambda: object()

        browser.recover_print_page()

        self.assertEqual(inventory.goto_calls, [])
        self.assertFalse(inventory.closed)
        self.assertEqual(context.new_page_calls, 1)
        self.assertIsNot(browser.page, inventory)
        self.assertIs(browser.page.context, context)

    def test_final_recovery_does_not_close_the_only_ordinary_epaas_tab(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url, *, closed=False):
                self.context = context
                self.url = url
                self.closed = closed

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []
                self.new_page_calls = 0

            def new_page(self):
                self.new_page_calls += 1
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        inventory = Page(context, "https://www.erp321.com/epaas?n=库存")
        context.pages.append(inventory)
        browser.browser = _BrowserDouble([context])
        browser.page = Page(context, app.JST_HOME_URL, closed=True)
        browser.context = context
        browser._prove_page_ready = lambda _page: None
        browser._express_frame = lambda: object()

        browser.recover_print_page(replace_page=True)

        self.assertFalse(inventory.closed)
        self.assertEqual(context.new_page_calls, 1)
        self.assertIsNot(browser.page, inventory)
        self.assertIs(browser.page.context, context)

    def test_missing_epaas_opens_one_tab_in_the_only_existing_context(self):
        browser = object.__new__(app.JSTBrowser)
        navigations = []

        class Page:
            url = "about:blank"

            def __init__(self, context):
                self.context = context

            def goto(self, url, *, wait_until, timeout):
                navigations.append((url, wait_until, timeout))
                self.url = url

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []
                self.new_page_calls = 0

            def new_page(self):
                self.new_page_calls += 1
                page = Page(self)
                self.pages.append(page)
                return page

        context = Context()
        browser.browser = _BrowserDouble([context])

        with self.assertRaises(app.JSTPageMissing):
            browser._find_jst_page()
        page = browser._open_jst_page_in_unique_context()

        self.assertIs(page.context, context)
        self.assertEqual(context.new_page_calls, 1)
        self.assertEqual(navigations, [(app.JST_HOME_URL, "domcontentloaded", 20_000)])

    def test_missing_epaas_never_guesses_between_browser_contexts(self):
        browser = object.__new__(app.JSTBrowser)

        class Context:
            pages = []

            def new_page(self):
                self.fail("must not create a tab in an ambiguous context")

        browser.browser = type(
            "Browser", (), {"contexts": [Context(), Context()]}
        )()
        with self.assertRaisesRegex(app.SafetyStop, "上下文不唯一"):
            browser._open_jst_page_in_unique_context()

    def test_recovery_never_switches_from_bound_context_to_replacement_context(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url, *, closed=False):
                self.context = context
                self.url = url
                self.closed = closed

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []
                self.new_page_calls = 0

            def new_page(self):
                self.new_page_calls += 1
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        bound_context = Context()
        replacement_context = Context()
        old_page = Page(bound_context, app.JST_HOME_URL, closed=True)
        browser.browser = type(
            "Browser", (), {"contexts": [replacement_context]}
        )()
        browser.page = old_page
        browser.context = bound_context
        browser._prove_page_ready = lambda _page: None
        browser._express_frame = lambda: object()

        with self.assertRaisesRegex(app.SafetyStop, "上下文"):
            browser.recover_print_page(replace_page=True)

        self.assertEqual(replacement_context.new_page_calls, 0)
        self.assertIs(browser.context, bound_context)
        self.assertTrue(old_page.closed)
        self.assertIsNone(browser.page)

    def test_recovery_rejects_foreign_context_existing_print_page_and_clears_cache(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url, *, closed=False):
                self.context = context
                self.url = url
                self.closed = closed

            def is_closed(self):
                return self.closed

        bound_context = type("BoundContext", (), {"pages": []})()
        old_page = Page(bound_context, app.JST_HOME_URL, closed=True)
        foreign_context = type("ForeignContext", (), {})()
        foreign_page = Page(foreign_context, app.JST_HOME_URL)
        foreign_context.pages = [foreign_page]
        browser.browser = type(
            "Browser", (), {"contexts": [foreign_context]}
        )()
        browser.page = old_page
        browser.context = bound_context
        browser._page_has_visible_express_frame = lambda _page: False

        with self.assertRaisesRegex(app.SafetyStop, "禁止切换会话"):
            browser.recover_print_page()

        self.assertFalse(foreign_page.closed)
        self.assertIs(browser.context, bound_context)
        self.assertIsNone(browser.page)

    def test_new_same_context_tab_redirected_to_login_is_closed(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            url = "about:blank"
            closed = False

            def goto(self, *_args, **_kwargs):
                self.url = "https://www.erp321.com/login"

            def close(self):
                self.closed = True

        page = Page()
        context = type("Context", (), {"pages": [], "new_page": lambda self: page})()
        browser.browser = _BrowserDouble([context])

        with self.assertRaisesRegex(app.OrderRowNotReady, "不会自动登录"):
            browser._open_jst_page_in_unique_context()
        self.assertTrue(page.closed)

    def test_final_recovery_replaces_only_selected_tab_in_same_context(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url):
                self.context = context
                self.url = url
                self.closed = False

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        old = Page(context, app.JST_HOME_URL)
        context.pages.append(old)
        browser.browser = _BrowserDouble([context])
        browser.page = old
        browser.context = context
        browser._prove_page_ready = lambda page: None
        browser._express_frame = lambda: object()

        browser.recover_print_page(replace_page=True)

        self.assertTrue(old.closed)
        self.assertIs(browser.page.context, context)
        self.assertIsNot(browser.page, old)
        self.assertEqual(browser.page.url, app.JST_HOME_URL)

    def test_final_recovery_keeps_old_page_until_every_proof_succeeds(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url):
                self.context = context
                self.url = url
                self.closed = False

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        old = Page(context, app.JST_HOME_URL)
        context.pages.append(old)
        browser.browser = _BrowserDouble([context])
        browser.page = old
        browser.context = context
        old_closed_during_final_proof = []

        def fail_final_proof(_old_page, _new_page):
            old_closed_during_final_proof.append(old.closed)
            raise app.OrderRowNotReady("最终 iframe 证明失败")

        browser._prove_replacement_topology = fail_final_proof

        with self.assertRaisesRegex(app.OrderRowNotReady, "最终 iframe"):
            browser.recover_print_page(replace_page=True)

        new_page = context.pages[-1]
        self.assertEqual(old_closed_during_final_proof, [False])
        self.assertFalse(old.closed)
        self.assertTrue(new_page.closed)
        self.assertIs(browser.page, old)
        self.assertIs(browser.context, context)

    def test_final_recovery_closed_replacement_after_proof_keeps_old_page(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url):
                self.context = context
                self.url = url
                self.closed = False

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        old = Page(context, app.JST_HOME_URL)
        context.pages.append(old)
        browser.browser = _BrowserDouble([context])
        browser.page = old
        browser.context = context

        def close_replacement_after_proof(_old_page, new_page):
            new_page.closed = True

        browser._prove_replacement_topology = close_replacement_after_proof

        with self.assertRaisesRegex(app.OrderRowNotReady, "提交前已关闭"):
            browser.recover_print_page(replace_page=True)

        replacement = context.pages[-1]
        self.assertFalse(old.closed)
        self.assertTrue(replacement.closed)
        self.assertIs(browser.page, old)
        self.assertIs(browser.context, context)

    def test_final_recovery_disconnected_transport_never_closes_old_page(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url):
                self.context = context
                self.url = url
                self.closed = False

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        old = Page(context, app.JST_HOME_URL)
        context.pages.append(old)
        browser.browser = type(
            "Browser",
            (),
            {"contexts": [context], "is_connected": lambda self: False},
        )()
        browser.page = old
        browser.context = context
        browser._prove_replacement_topology = lambda *_args: None

        with self.assertRaisesRegex(app.OrderRowNotReady, "提交前已关闭"):
            browser.recover_print_page(replace_page=True)

        replacement = context.pages[-1]
        self.assertFalse(old.closed)
        self.assertTrue(replacement.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_final_recovery_closed_replacement_during_old_close_never_commits(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url):
                self.context = context
                self.url = url
                self.closed = False
                self.on_close = None

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True
                if self.on_close is not None:
                    self.on_close()

        class Context:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        old = Page(context, app.JST_HOME_URL)
        context.pages.append(old)
        browser.browser = _BrowserDouble([context])
        browser.page = old
        browser.context = context
        browser._prove_replacement_topology = lambda *_args: None

        original_new_page = context.new_page

        def new_page_with_close_race():
            replacement = original_new_page()
            old.on_close = lambda: setattr(replacement, "closed", True)
            return replacement

        context.new_page = new_page_with_close_race

        with self.assertRaisesRegex(app.OrderRowNotReady, "提交前已关闭"):
            browser.recover_print_page(replace_page=True)

        replacement = context.pages[-1]
        self.assertTrue(old.closed)
        self.assertTrue(replacement.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_final_recovery_failed_proof_never_recaches_closed_old_page(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url, *, closed=False):
                self.context = context
                self.url = url
                self.closed = closed

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        old = Page(context, app.JST_HOME_URL, closed=True)
        browser.browser = _BrowserDouble([context])
        browser.page = old
        browser.context = context
        browser._prove_replacement_topology = lambda *_args: (
            _ for _ in ()
        ).throw(app.OrderRowNotReady("最终 iframe 证明失败"))

        with self.assertRaisesRegex(app.OrderRowNotReady, "最终 iframe"):
            browser.recover_print_page(replace_page=True)

        replacement = context.pages[-1]
        self.assertTrue(old.closed)
        self.assertTrue(replacement.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_missing_page_failed_proof_never_recaches_ordinary_epaas_page(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url):
                self.context = context
                self.url = url
                self.closed = False

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        inventory = Page(context, "https://www.erp321.com/epaas?n=库存")
        context.pages.append(inventory)
        browser.browser = _BrowserDouble([context])
        browser.page = inventory
        browser.context = context
        browser._prove_replacement_topology = lambda *_args: (
            _ for _ in ()
        ).throw(app.OrderRowNotReady("最终 iframe 证明失败"))

        with self.assertRaisesRegex(app.OrderRowNotReady, "最终 iframe"):
            browser.recover_print_page()

        replacement = context.pages[-1]
        self.assertFalse(inventory.closed)
        self.assertTrue(replacement.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_missing_page_closed_after_proof_never_commits_replacement(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url):
                self.context = context
                self.url = url
                self.closed = False

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        closed_old = Page(context, app.JST_HOME_URL)
        closed_old.closed = True
        browser.browser = _BrowserDouble([context])
        browser.page = closed_old
        browser.context = context

        def close_after_proof(_old_page, new_page):
            new_page.closed = True

        browser._prove_replacement_topology = close_after_proof

        with self.assertRaisesRegex(app.OrderRowNotReady, "提交前已关闭"):
            browser.recover_print_page()

        replacement = context.pages[-1]
        self.assertTrue(replacement.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_final_recovery_open_failure_clears_closed_cached_page(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            url = app.JST_HOME_URL

            def __init__(self, context):
                self.context = context

            def is_closed(self):
                return True

        context = type("Context", (), {"pages": []})()
        old = Page(context)
        browser.browser = _BrowserDouble([context])
        browser.page = old
        browser.context = context
        browser._open_jst_page_in_unique_context = lambda: (
            _ for _ in ()
        ).throw(app.OrderRowNotReady("新页打开失败"))

        with self.assertRaisesRegex(app.OrderRowNotReady, "新页打开失败"):
            browser.recover_print_page(replace_page=True)

        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_missing_page_open_failure_does_not_cache_ordinary_epaas_page(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context):
                self.context = context
                self.url = "https://www.erp321.com/epaas?n=库存"
                self.closed = False

            def is_closed(self):
                return self.closed

        context = type("Context", (), {})()
        inventory = Page(context)
        context.pages = [inventory]
        browser.browser = _BrowserDouble([context])
        browser.page = inventory
        browser.context = context
        browser._open_jst_page_in_unique_context = lambda: (
            _ for _ in ()
        ).throw(app.OrderRowNotReady("新页打开失败"))

        with self.assertRaisesRegex(app.OrderRowNotReady, "新页打开失败"):
            browser.recover_print_page()

        self.assertFalse(inventory.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_failed_replacement_reproves_old_page_identity_before_caching(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url):
                self.context = context
                self.url = url
                self.closed = False

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = Page(self, "about:blank")
                self.pages.append(page)
                return page

        context = Context()
        old = Page(context, app.JST_HOME_URL)
        context.pages.append(old)
        browser.browser = _BrowserDouble([context])
        browser.page = old
        browser.context = context

        def fail_after_old_page_navigates(_old_page, _new_page):
            old.url = "https://www.erp321.com/epaas?n=库存"
            raise app.OrderRowNotReady("最终 iframe 证明失败")

        browser._prove_replacement_topology = fail_after_old_page_navigates

        with self.assertRaisesRegex(app.OrderRowNotReady, "最终 iframe"):
            browser.recover_print_page(replace_page=True)

        replacement = context.pages[-1]
        self.assertFalse(old.closed)
        self.assertTrue(replacement.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_reused_print_page_goto_close_failure_clears_cached_page(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context):
                self.context = context
                self.url = app.JST_HOME_URL
                self.closed = False

            def goto(self, *_args, **_kwargs):
                self.closed = True
                raise RuntimeError("target closed")

            def is_closed(self):
                return self.closed

        context = type("Context", (), {})()
        page = Page(context)
        context.pages = [page]
        browser.browser = _BrowserDouble([context])
        browser.page = page
        browser.context = context
        browser._page_has_visible_express_frame = lambda _page: False

        with self.assertRaisesRegex(app.OrderRowNotReady, "页签已关闭"):
            browser.recover_print_page()

        self.assertTrue(page.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_reused_other_print_page_login_failure_clears_ordinary_cache(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, url):
                self.context = context
                self.url = url
                self.closed = False

            def goto(self, *_args, **_kwargs):
                self.url = "https://www.erp321.com/login"

            def is_closed(self):
                return self.closed

        context = type("Context", (), {})()
        inventory = Page(context, "https://www.erp321.com/epaas?n=库存")
        print_page = Page(context, app.JST_HOME_URL)
        context.pages = [inventory, print_page]
        browser.browser = _BrowserDouble([context])
        browser.page = inventory
        browser.context = context
        browser._page_has_visible_express_frame = lambda _page: False

        with self.assertRaisesRegex(app.OrderRowNotReady, "不会自动填写登录"):
            browser.recover_print_page()

        self.assertFalse(inventory.closed)
        self.assertFalse(print_page.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_reused_page_closed_after_iframe_proof_never_returns_success(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context):
                self.context = context
                self.url = app.JST_HOME_URL
                self.closed = False

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

        context = type("Context", (), {})()
        page = Page(context)
        context.pages = [page]
        browser.browser = _BrowserDouble([context])
        browser.page = page
        browser.context = context
        browser._page_has_visible_express_frame = lambda _page: False

        def close_after_iframe_proof():
            page.closed = True
            browser.page = page
            return object()

        browser._express_frame = close_after_iframe_proof

        with self.assertRaisesRegex(app.OrderRowNotReady, "提交前已关闭"):
            browser.recover_print_page()

        self.assertTrue(page.closed)
        self.assertIsNone(browser.page)
        self.assertIs(browser.context, context)

    def test_final_recovery_with_no_epaas_creates_exactly_one_tab(self):
        browser = object.__new__(app.JSTBrowser)

        class Page:
            def __init__(self, context, *, closed=False):
                self.context = context
                self.url = "about:blank"
                self.closed = closed

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []
                self.new_page_calls = 0

            def new_page(self):
                self.new_page_calls += 1
                page = Page(self)
                self.pages.append(page)
                return page

        context = Context()
        browser.browser = _BrowserDouble([context])
        browser.page = Page(context, closed=True)
        browser.context = context
        browser._prove_page_ready = lambda page: None
        browser._express_frame = lambda: object()

        browser.recover_print_page(replace_page=True)

        self.assertEqual(context.new_page_calls, 1)
        self.assertEqual(len(context.pages), 1)
        self.assertIs(browser.page, context.pages[0])

    def test_constructor_failure_stops_playwright_transport(self):
        stopped = []

        class Runtime:
            def __init__(self):
                self.chromium = self

            def connect_over_cdp(self, _endpoint):
                context = type("Context", (), {"pages": []})
                return type(
                    "Browser", (), {"contexts": [context(), context()]}
                )()

            def stop(self):
                stopped.append(True)

        runtime = Runtime()
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = lambda: type(
            "Starter", (), {"start": lambda self: runtime}
        )()
        package = types.ModuleType("playwright")
        package.__path__ = []

        with mock.patch.dict(
            sys.modules,
            {"playwright": package, "playwright.sync_api": sync_api},
        ), mock.patch.object(app, "cdp_endpoint", return_value="ws://127.0.0.1:9222/x"):
            with self.assertRaises(app.SafetyStop):
                app.JSTBrowser(app.Settings())

        self.assertEqual(stopped, [True])

    def test_constructor_closes_soft_login_epaas_tab_without_relogin(self):
        stopped = []

        class Page:
            url = "about:blank"
            closed = False

            def __init__(self, context):
                self.context = context

            def goto(self, *_args, **_kwargs):
                # Some expired sessions keep /epaas while rendering only a
                # soft login shell instead of redirecting to /login.
                self.url = app.JST_HOME_URL

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []
                self.opened = None

            def new_page(self):
                self.opened = Page(self)
                self.pages.append(self.opened)
                return self.opened

        context = Context()

        class Runtime:
            def __init__(self):
                self.chromium = self

            def connect_over_cdp(self, _endpoint):
                return _BrowserDouble([context])

            def stop(self):
                stopped.append(True)

        runtime = Runtime()
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = lambda: type(
            "Starter", (), {"start": lambda self: runtime}
        )()
        package = types.ModuleType("playwright")
        package.__path__ = []

        with mock.patch.dict(
            sys.modules,
            {"playwright": package, "playwright.sync_api": sync_api},
        ), mock.patch.object(
            app, "cdp_endpoint", return_value="ws://127.0.0.1:9222/x"
        ), mock.patch.object(
            app.JSTBrowser,
            "_prove_page_ready",
            side_effect=app.OrderRowNotReady("软登录页没有业务 iframe"),
        ):
            with self.assertRaisesRegex(app.OrderRowNotReady, "软登录"):
                app.JSTBrowser(app.Settings())

        self.assertIsNotNone(context.opened)
        self.assertTrue(context.opened.closed)
        self.assertEqual(stopped, [True])

    def test_constructor_never_commits_page_closed_after_readiness_proof(self):
        stopped = []

        class Page:
            url = "about:blank"

            def __init__(self, context):
                self.context = context
                self.closed = False

            def goto(self, *_args, **_kwargs):
                self.url = app.JST_HOME_URL

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.pages = []
                self.opened = None

            def new_page(self):
                self.opened = Page(self)
                self.pages.append(self.opened)
                return self.opened

        context = Context()

        class Runtime:
            def __init__(self):
                self.chromium = self

            def connect_over_cdp(self, _endpoint):
                return _BrowserDouble([context])

            def stop(self):
                stopped.append(True)

        runtime = Runtime()
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = lambda: type(
            "Starter", (), {"start": lambda self: runtime}
        )()
        package = types.ModuleType("playwright")
        package.__path__ = []

        def close_after_proof(_page):
            context.opened.closed = True

        with mock.patch.dict(
            sys.modules,
            {"playwright": package, "playwright.sync_api": sync_api},
        ), mock.patch.object(
            app, "cdp_endpoint", return_value="ws://127.0.0.1:9222/x"
        ), mock.patch.object(
            app.JSTBrowser,
            "_prove_page_ready",
            side_effect=close_after_proof,
        ):
            with self.assertRaisesRegex(app.OrderRowNotReady, "提交前已关闭"):
                app.JSTBrowser(app.Settings())

        self.assertIsNotNone(context.opened)
        self.assertTrue(context.opened.closed)
        self.assertEqual(stopped, [True])

    def test_other_epaas_tabs_do_not_conflict_with_one_real_print_page(self):
        browser = object.__new__(app.JSTBrowser)
        ordinary = type("Page", (), {"url": "https://www.erp321.com/epaas?n=库存"})()
        print_page = type("Page", (), {"url": app.JST_HOME_URL})()
        browser.browser = type(
            "Browser",
            (),
            {"contexts": [type("Context", (), {"pages": [ordinary, print_page]})()]},
        )()

        with mock.patch.object(
            app.JSTBrowser,
            "_page_has_visible_express_frame",
            side_effect=lambda page: page is print_page,
        ):
            self.assertIs(browser._find_jst_page(), print_page)

    def test_two_real_print_pages_remain_ambiguous(self):
        browser = object.__new__(app.JSTBrowser)
        first = type("Page", (), {"url": app.JST_HOME_URL})()
        second = type("Page", (), {"url": app.JST_HOME_URL + "&copy=1"})()
        browser.browser = type(
            "Browser",
            (),
            {"contexts": [type("Context", (), {"pages": [first, second]})()]},
        )()

        with mock.patch.object(
            app.JSTBrowser,
            "_page_has_visible_express_frame",
            return_value=True,
        ):
            with self.assertRaisesRegex(app.SafetyStop, "多个真实打单拣货页面"):
                browser._find_jst_page()

    def test_cached_cdp_session_is_reused_only_while_healthy(self):
        engine = object.__new__(app.AutomationEngine)
        settings = app.Settings(browser_name="Chrome", debug_port=9222)

        class Session:
            def __init__(self, healthy):
                self.healthy = healthy
                self.disconnects = 0

            def is_healthy(self):
                return self.healthy

            def disconnect(self):
                self.disconnects += 1

        healthy = Session(True)
        engine._browser_session = healthy
        engine._browser_session_key = ("Chrome", 9222)
        self.assertIs(engine._browser_for(settings), healthy)

        stale = Session(False)
        replacement = Session(True)
        engine._browser_session = stale
        engine._browser_session_key = ("Chrome", 9222)
        with mock.patch.object(app, "JSTBrowser", return_value=replacement):
            self.assertIs(engine._browser_for(settings), replacement)
        self.assertEqual(stale.disconnects, 1)

    def test_repeated_dom_miss_recovers_current_page_then_pauses_boundedly(self):
        engine = object.__new__(app.AutomationEngine)
        engine.store = _Store()
        engine.dom_lookup_retries = {}
        engine.dom_page_recoveries = {}
        engine._disconnect_browser = lambda: None
        waits = []
        engine._wait = lambda seconds: waits.append(seconds) or True
        engine._event = lambda *_args, **_kwargs: None
        engine.set_status = lambda _message: None
        pauses = []
        engine._pause_resumable_job = lambda job, reason, pause_kind=None: pauses.append(
            (job, reason, pause_kind)
        )
        recoveries = []

        class Browser:
            def recover_print_page(self, *, replace_page=False):
                recoveries.append(
                    "same-session-new-tab" if replace_page else "same-session-page"
                )

        engine._settings = lambda: object()
        engine._browser_for = lambda _settings: Browser()
        job = {
            "o_id": "6787909",
            "io_id": "13539546",
            "status": "PENDING",
            "step_index": 0,
        }

        first_cycle = [
            engine._retry_missing_dom_row(job, "表格为空")
            for _ in range(app.DOM_LOOKUP_MAX_ATTEMPTS)
        ]
        second_cycle = [
            engine._retry_missing_dom_row(job, "表格为空")
            for _ in range(app.DOM_LOOKUP_MAX_ATTEMPTS)
        ]
        final_cycle = [
            engine._retry_missing_dom_row(job, "表格为空")
            for _ in range(app.DOM_LOOKUP_MAX_ATTEMPTS)
        ]

        self.assertTrue(all(first_cycle))
        self.assertTrue(all(second_cycle))
        self.assertTrue(all(final_cycle[:-1]))
        self.assertFalse(final_cycle[-1])
        self.assertEqual(
            recoveries,
            ["same-session-page", "same-session-new-tab"],
        )
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0][2], "DOM_ENVIRONMENT")
        self.assertEqual(waits[app.DOM_LOOKUP_MAX_ATTEMPTS - 1], 1.5)
        self.assertEqual(waits[app.DOM_LOOKUP_MAX_ATTEMPTS * 2 - 1], 3.0)

    def test_transient_backend_errors_auto_retry_without_global_pause(self):
        engine = object.__new__(app.AutomationEngine)
        engine.store = _Store()
        engine.transient_api_failures = 0
        engine.run_event = __import__("threading").Event()
        engine.run_event.set()
        waits = []
        events = []
        engine._wait = lambda seconds: waits.append(seconds) or True
        engine._event = lambda *args, **kwargs: events.append((args, kwargs))
        engine.set_status = lambda _message: None
        job = {
            "o_id": "6787909",
            "io_id": "13539546",
            "status": "PREPARING",
            "step_index": 0,
        }

        self.assertTrue(engine._retry_transient_api(job, "timeout"))
        self.assertTrue(engine._retry_transient_api(job, "timeout"))

        self.assertTrue(engine.run_event.is_set())
        self.assertEqual(
            waits,
            list(app.TRANSIENT_API_RETRY_DELAYS[:2]),
        )
        self.assertEqual(
            [item[2]["status"] for item in engine.store.updates],
            ["PENDING", "PENDING"],
        )
        self.assertTrue(all(item[0][1] == "API_TRANSIENT_RETRY" for item in events))


if __name__ == "__main__":
    unittest.main()
