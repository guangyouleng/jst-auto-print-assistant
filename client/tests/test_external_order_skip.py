import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app
import jst_print_shadow_plan as planner
import test_privacy_remark_routing as privacy_tests
from test_server_empty_plan_diagnostics import api, _raw, _candidate, _Store


class ExternalOrderSkipTests(unittest.TestCase):
    def test_label_only_matching_and_live_readback(self):
        for label, remark, expected in [
            ('外部系统订单', '', True),
            ('重点,外部系统订单‼,活动', '', True),
            ('普通订单', '外部系统订单', False),
            ('', '', False),
        ]:
            with self.subTest(label=label, remark=remark):
                row = privacy_tests.PrivacyRemarkRoutingTests._row(remark, label)
                plan = planner.make_plan(row, [], action_history_complete=True)
                self.assertIs(plan.external_system_order, expected)
                api._validate_plan(asdict(plan))
                readback = planner._found_inspect_result(row, [], True, generated_at='2026-09-18T00:00:00Z')
                self.assertIs(readback['external_system_order'], expected)

    def test_filter_before_claim_and_toggle_off(self):
        raw = _raw()
        external = dict(_candidate(), external_system_order=True)
        normal = dict(_candidate(), o_id='102', io_id='202', external_system_order=False)
        raw['selected'] = [external, normal]
        for enabled, ids in [(True, ['102']), (False, ['101', '102'])]:
            store = _Store()
            with mock.patch.object(api, 'get_plan_pool', return_value=raw):
                result = api._plan(dict(workstation_id='workstation-123', exclude=[], max_candidates=10,
                                        print_profile=api.TARGET_CARRIER_ID, skip_external_orders=enabled), store)
            self.assertEqual([x[0] for x in store.claimed], ids)
            self.assertIs(result['skip_external_orders'], enabled)

    def test_preflight_blocks_external_and_unknown_before_any_action(self):
        for step in ['GET_WAYBILL', 'RESET_CARRIER_AND_GET_WAYBILL:ZTO.1:中通速递-山东', 'PRINT_EXPRESS']:
            with self.subTest(step=step):
                with self.assertRaises(app.ExternalSystemOrderSkipped):
                    app.AutomationEngine._preflight({'external_system_order': True}, step, {'skip_external_orders': True})
                with self.assertRaises(app.BackendSchemaError):
                    app.AutomationEngine._preflight({}, step, {'skip_external_orders': True})

    def test_disabled_does_not_exclude_external(self):
        with self.assertRaisesRegex(app.PermanentJobError, '后台未找到'):
            app.AutomationEngine._preflight({'external_system_order': True}, 'PRINT_EXPRESS', {'skip_external_orders': False})

    def test_preference_roundtrip_and_invalid_types(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            deployment = root / 'deployment.json'
            deployment.write_text('{"backend_mode": "local"}')
            with mock.patch.object(app, 'ensure_app_dirs'), mock.patch.object(app, 'CONFIG_FILE', root / 'settings.json'), mock.patch.object(app, 'deployment_config_path', return_value=deployment):
                self.assertFalse(app.load_settings().skip_external_orders)
                settings = app.load_settings()
                settings.skip_external_orders = True
                app.save_settings(settings)
                self.assertTrue(app.load_settings().skip_external_orders)
                settings.skip_external_orders = False
                app.save_settings(settings)
                self.assertFalse(app.load_settings().skip_external_orders)
        for invalid in ['false', 1, None]:
            with self.assertRaises(ValueError):
                app.Settings(skip_external_orders=invalid).validate()

    def test_skip_release_remains_reclaimable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = app.EventStore(root / 'events.db', root / 'events.jsonl')
            plan = {'o_id': '101', 'io_id': '201'}
            self.assertTrue(store.save_job(plan))
            store.update_job('101', '201', step_index=0, status='RELEASED_EXTERNAL_ORDER')
            self.assertNotIn({'o_id': '101', 'io_id': '201'}, store.excluded_pairs())
            self.assertTrue(store.save_job(plan))


class ExternalOrderProtocolTests(unittest.TestCase):
    def test_old_clients_receive_unchanged_candidate_fields(self):
        from types import SimpleNamespace
        lease = SimpleNamespace(claim_token='x' * 32, expires_epoch=2_000_000_000, lease_ttl_seconds=300)
        raw = _raw()
        raw['selected'][0]['external_system_order'] = False
        for enabled in (False, True):
            store = _Store(claim_result=lease)
            body = dict(workstation_id='workstation-123', exclude=[], max_candidates=10,
                        print_profile=api.TARGET_CARRIER_ID)
            if enabled:
                body['skip_external_orders'] = True
            with mock.patch.object(api, 'get_plan_pool', return_value=raw):
                result = api._plan(body, store)
            self.assertEqual('external_system_order' in result['selected'][0], enabled)
        self.assertIs(raw['selected'][0]['external_system_order'], False)

    def test_old_single_and_batch_inspect_responses_keep_legacy_schema(self):
        from test_backend_batch_inspect import LeaseStore, order_row
        generated = planner.utc_now_text()
        row = order_row('101', '201')
        row['labels'] = '外部系统订单'
        raw = planner._found_inspect_result(row, [], True, generated_at=generated)
        with tempfile.TemporaryDirectory() as folder:
            store = LeaseStore(Path(folder) / 'leases.db')
            owner = api._workstation_id({'workstation_id': 'workstation-123'})
            lease = store.claim('101', '201', owner)
            identity = dict(o_id='101', io_id='201', claim_token=lease.claim_token)
            for enabled in (False, True):
                option = {'include_external_order_status': True} if enabled else {}
                body = dict(workstation_id='workstation-123', **identity, **option)
                with mock.patch.object(api, 'run_planner', return_value=raw):
                    result = api._inspect(body, store)
                self.assertEqual('external_system_order' in result, enabled)
                app.PlannerClient._validate_inspect_schema(result, '101', '201')
                batch = dict(mode='ORDER_BATCH_READBACK_V5', schema_version=5,
                             generated_at=generated, results=[raw])
                body = dict(workstation_id='workstation-123', orders=[identity], **option)
                with mock.patch.object(api, 'run_planner', return_value=batch):
                    result = api._inspect_batch(body, store)
                self.assertEqual('external_system_order' in result['results'][0], enabled)
            self.assertTrue(raw['external_system_order'])

    def test_new_client_requests_recognition_and_rejects_old_backend(self):
        import test_client_security_boundaries as fixtures
        client = object.__new__(app.PlannerClient)
        client.settings = app.Settings(skip_external_orders=True)
        client.workstation_id = 'ws-00000000-0000-0000-0000-000000000000'
        response = fixtures._plan_response([])
        client._post = mock.Mock(return_value=response)
        with self.assertRaisesRegex(app.BackendSchemaError, '后台尚未支持'):
            client.plan([], app.TARGET_CARRIER_ID)
        self.assertTrue(client._post.call_args.args[1]['skip_external_orders'])
        response['skip_external_orders'] = True
        self.assertEqual(client.plan([], app.TARGET_CARRIER_ID)['selected'], [])
        for method, args in [('inspect', ('101', '201', 'x' * 32)),
                             ('inspect_batch', ([('101', '201', 'x' * 32)],))]:
            client._post = mock.Mock(side_effect=RuntimeError('request captured'))
            with self.assertRaisesRegex(RuntimeError, 'request captured'):
                getattr(client, method)(*args)
            self.assertTrue(client._post.call_args.args[1]['include_external_order_status'])

    def test_missing_or_invalid_recognition_never_claims_when_enabled(self):
        for value in (None, 'false', 0):
            raw = _raw()
            raw['selected'][0]['external_system_order'] = value
            store = _Store()
            with mock.patch.object(api, 'get_plan_pool', return_value=raw):
                with self.assertRaises(api.APIError):
                    api._plan(dict(workstation_id='workstation-123', exclude=[], max_candidates=10,
                                   print_profile=api.TARGET_CARRIER_ID, skip_external_orders=True), store)
            self.assertEqual(store.claimed, [])
        for value in ('false', 1, None):
            with self.assertRaises(api.APIError):
                api._inspect_feature_request({'include_external_order_status': value})


class ExternalOrderBatchTests(unittest.TestCase):
    def test_second_order_tag_change_before_commit_blocks_entire_batch(self):
        import hashlib
        import test_batch_exception_attribution as fixtures
        for kind in ('print', 'waybill'):
            for late_change in (False, True):
                with self.subTest(kind=kind, late_change=late_change):
                    jobs = [(fixtures._print_job if kind == 'print' else fixtures._waybill_job)(i) for i in range(2)]
                    engine = fixtures._base_engine(jobs)
                    engine._settings = lambda: app.Settings(allow_write=True, allow_print=True,
                        print_profile=app.SOURCE_CARRIER_ID, skip_external_orders=True)
                    # Use the real preflight, retaining only a route mock because
                    # these fixtures intentionally omit unrelated SKU/weight data.
                    engine._preflight = app.AutomationEngine._preflight
                    engine._validate_final_print_readback = lambda plan, rb, *_: app.AutomationEngine._preflight(rb, 'PRINT_EXPRESS', plan)
                    clicked = []
                    processed = []
                    engine._process_job = lambda _, job: processed.append(job)

                    class Planner:
                        calls = 0
                        def inspect_batch(self, credentials):
                            self.calls += 1
                            return [dict(found=True, o_id=job['o_id'], io_id=job['io_id'],
                                status='WaitConfirm', action_history_complete=True, delivery_hold_reasons=[],
                                external_system_order=(i == 1 and (not late_change or self.calls == 2)),
                                has_waybill=(kind == 'print'), carrier_id=app.TARGET_CARRIER_ID,
                                carrier_name=app.TARGET_CARRIER_NAME, privacy_required=False,
                                privacy_source='remark', waybill_suffix=job['io_id'][-4:],
                                waybill_fingerprint=hashlib.sha256(job['io_id'].encode()).hexdigest())
                                for i, job in enumerate(jobs)]
                    class Browser:
                        def act(self, *args, **kwargs):
                            (kwargs.get('before_click') or kwargs['before_confirm'])()
                            kwargs['mark_running']()
                            clicked.append(True)
                        print_express_batch = act
                        reset_carrier_batch = act
                    engine._browser_for = lambda _: Browser()
                    with mock.patch.object(app, 'print_service_online', return_value=True), mock.patch.object(app.AutomationEngine, '_validate_live_route'):
                        method = engine._process_print_batch if kind == 'print' else engine._process_waybill_batch
                        if late_change:
                            method(Planner(), jobs)
                            self.assertEqual(processed, [jobs[1]])
                        else:
                            with self.assertRaises(app.ExternalSystemOrderSkipped) as raised:
                                method(Planner(), jobs)
                            self.assertEqual(raised.exception._jst_job_identity, (jobs[1]['o_id'], jobs[1]['io_id']))
                    self.assertEqual(clicked, [])
                    self.assertTrue(all(engine.store.get_job(j['o_id'], j['io_id'])['status'] == 'PENDING' for j in jobs))
