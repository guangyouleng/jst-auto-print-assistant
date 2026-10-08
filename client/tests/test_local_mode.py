"""Single-PC workflow, persistence and read-only transport boundaries."""
import hashlib
import io
import json
import tempfile
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import jst_auto_print_app as app
import jst_local_coordinator as core
import jst_print_shadow_plan as planner
from jst_local_store import LeaseStore
from jst_openapi import JSTReadonlyClient, JSTQueryError, RejectRedirect
from test_backend_batch_inspect import order_row


class Response(io.BytesIO):
    pass


class LocalModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = mock.patch.object(app, 'APP_DIR', self.root)
        self.patch.start()
        db_patch = mock.patch.object(app, 'DATABASE_FILE', self.root / 'events.sqlite3')
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.addCleanup(self.patch.stop)
        self.settings = app.Settings(backend_mode='local')
        self.client = app.PlannerClient(self.settings, 'ws-local-tests')
        self.rows = [order_row('10001', '20001', created=planner.business_now().strftime('%Y-%m-%d %H:%M:%S'))]
        self.actions = []
        parent = self
        class FakeJST:
            def check(self):
                return True
            def query_orders_out(self, **kwargs):
                ids = kwargs.get('o_ids')
                return {'datas': [r for r in parent.rows if not ids or int(r['o_id']) in ids], 'has_next': False}
            def query_order_action(self, **kwargs):
                return {'datas': parent.actions, 'has_next': False}
        self.source_client = FakeJST()
        self.client._local_source.client = self.source_client

    def claim(self):
        return self.client.plan([], app.TARGET_CARRIER_ID)['selected'][0]

    def test_plan_and_readback_need_no_bridge_or_http_service(self):
        with mock.patch.object(planner, '_load_jst_client', side_effect=AssertionError('bridge forbidden')), \
             mock.patch.object(app.urllib.request, 'urlopen', side_effect=AssertionError('HTTP service forbidden')):
            self.assertTrue(self.client.ping())
            selected = self.claim()
            self.assertEqual(selected['o_id'], '10001')
            result = self.client.inspect('10001', '20001', selected['claim_token'])
            self.assertTrue(result['found'])
            self.assertTrue(result['action_history_complete'])
            self.assertEqual(result['actions'], [])
            results = self.client.inspect_batch([('10001', '20001', selected['claim_token'])])
            self.assertEqual(len(results), 1)
            self.client.renew('10001', '20001', selected['claim_token'])

    def test_complete_requires_fresh_print_action_not_print_flag(self):
        selected = self.claim()
        token = selected['claim_token']
        self.rows[0]['is_print_express'] = True
        with self.assertRaises(app.CompletionProofError):
            self.client.complete('10001', '20001', token, 'PRINTED')
        self.actions.append({'o_id': '10001', 'name': '打印快递单',
                             'created': planner.business_now().strftime('%Y-%m-%d %H:%M:%S')})
        self.client.complete('10001', '20001', token, 'PRINTED')
        self.client.complete('10001', '20001', token, 'PRINTED')
        self.assertIsNone(self.client._local_store.claim('10001', '20001', 'ws-local-tests'))

    def test_force_skip_survives_restart_without_api_credentials(self):
        self.client.force_skip('10001', '20001', '人工确认问题单')
        restarted = app.PlannerClient(self.settings, 'ws-local-tests')
        restarted._local_source.client = self.source_client
        self.assertEqual(restarted.plan([], app.TARGET_CARRIER_ID)['selected'], [])
        self.assertIsNotNone(restarted._local_store.claim('10001', '20002', 'ws-local-tests'))

    def test_local_exclusions_are_not_limited_to_200(self):
        for i in range(205):
            self.client.force_skip(str(10001 + i), str(20001 + i), '人工跳过')
        reopened = LeaseStore(self.root / 'local-reservations.sqlite3')
        self.assertIsNone(reopened.claim('10205', '20205', 'ws-local-tests'))

    def test_migration_keeps_old_running_token_and_all_local_exclusions(self):
        from jst_local_store import migrate_event_store
        events = app.EventStore(self.root / 'events.sqlite3', self.root / 'events.jsonl')
        token = 'a' * 32
        plan = {'o_id': '10001', 'io_id': '20001', 'claim_token': token,
                'steps': ['PRINT_EXPRESS', 'STOP_BEFORE_PRESHIP']}
        events.save_job(plan)
        events.update_job('10001', '20001', step_index=0, status='RUNNING')
        for i in range(205):
            events.exclude_job(str(30000+i), str(40000+i), '旧问题单')
        migrate_event_store(self.client._local_store, self.root / 'events.sqlite3', 'ws-local-tests')
        lease = self.client._local_store.require_active('10001', '20001', 'ws-local-tests', token)
        self.assertEqual(lease.claim_token, token)
        self.assertEqual(events.active_jobs()[0]['status'], 'RUNNING')
        self.assertIsNone(self.client._local_store.claim('30204', '40204', 'ws-local-tests'))

    def test_client_runs_with_only_client_files_and_no_server_directory(self):
        isolated = self.root / 'client-only'
        isolated.mkdir()
        for name in ('jst_auto_print_app', 'jst_print_shadow_plan', 'jst_local_store',
                     'jst_local_coordinator', 'jst_local_source', 'jst_openapi', 'jst_credentials'):
            shutil.copyfile(Path(app.__file__).parent / (name + '.py'), isolated / (name + '.py'))
        script = """
import json
from pathlib import Path
import jst_auto_print_app as app
app.APP_DIR = Path.cwd() / 'state'
app.DATABASE_FILE = app.APP_DIR / 'events.sqlite3'
client = app.PlannerClient(app.Settings(backend_mode='local'), 'ws-isolated')
rows = json.loads(ROW_JSON)
class FakeJST:
    def check(self): return True
    def query_orders_out(self, **kwargs): return {'datas': rows, 'has_next': False}
    def query_order_action(self, **kwargs): return {'datas': [], 'has_next': False}
client._local_source.client = FakeJST()
assert client.ping()
plan = client.plan([], app.TARGET_CARRIER_ID)['selected'][0]
assert client.inspect(plan['o_id'], plan['io_id'], plan['claim_token'])['found']
client.force_skip(plan['o_id'], plan['io_id'], 'isolated test')
assert client.plan([], app.TARGET_CARRIER_ID)['selected'] == []
""".replace('ROW_JSON', repr(json.dumps(self.rows)))
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, '-c', script], cwd=isolated,
                                env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_http_outage_is_retryable_but_business_token_error_stops(self):
        for code in ('HTTP_503', 'HTTP_504', 'HTTP_429'):
            with self.subTest(code=code), mock.patch.object(self.client._local_source, 'check', side_effect=JSTQueryError(code)):
                with self.assertRaises(app.TransientAPIError):
                    self.client.ping()
        for code in ('503', '504'):
            with self.subTest(code=code), mock.patch.object(self.client._local_source, 'check', side_effect=JSTQueryError(code)):
                with self.assertRaises(app.SafetyStop):
                    self.client.ping()

    def test_local_does_not_require_backend_token(self):
        self.settings.validate()
        self.assertTrue(app.Settings().local_mode)


class OpenAPIReadTests(unittest.TestCase):
    def client(self, response):
        opener = mock.Mock()
        opener.open.return_value = Response(json.dumps(response).encode())
        client = JSTReadonlyClient(Path('/unused'), credentials={
            'app_key': 'partner', 'appsecret': 'secret', 'access_token': 'token'}, opener=opener)
        return client, opener

    def test_signature_and_pagination_match_supplied_bridge(self):
        client, opener = self.client({'code': 0, 'datas': [], 'has_next': False})
        with mock.patch('jst_openapi.time.time', return_value=1234):
            client.query_orders_out(page_no=2, page_size=100, o_ids=[10001], inout_flds=['labels'])
        request = opener.open.call_args.args[0]
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query['method'], ['orders.out.simple.query'])
        self.assertEqual(query['sign'], [hashlib.md5(b'orders.out.simple.querypartnertokentokents1234secret').hexdigest()])
        self.assertEqual(json.loads(request.data), {'o_ids': [10001], 'inout_flds': ['labels'], 'page_index': 2, 'page_size': 100})

    def test_startup_checks_order_permission_without_shop_query(self):
        client, opener = self.client({'code': 0, 'datas': [], 'has_next': False})
        self.assertTrue(client.check())
        request = opener.open.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)['method'], ['orders.out.simple.query'])
        self.assertEqual(json.loads(request.data)['page_size'], 1)

    def test_http_status_is_distinct_from_business_token_code(self):
        import urllib.error
        client, opener = self.client({'code': 0})
        opener.open.side_effect = urllib.error.HTTPError('https://unused', 504, 'Gateway timeout', {}, None)
        with self.assertRaises(JSTQueryError) as caught:
            client.query_order_action(o_ids=[10001])
        self.assertEqual(caught.exception.code, 'HTTP_504')

    def test_missing_pagination_proof_is_rejected(self):
        for result in ({'code': 0, 'datas': []}, {'code': 0, 'datas': [], 'has_next': 'false'},
                       {'code': 0, 'datas': [1], 'has_next': False}):
            client, _ = self.client(result)
            with self.assertRaises(JSTQueryError):
                client.query_order_action(o_ids=[10001])

    def test_business_error_never_exposes_response_or_credentials(self):
        client, _ = self.client({'code': 503, 'msg': 'secret token address', 'token': 'token'})
        with self.assertRaises(JSTQueryError) as caught:
            client.query_order_action(o_ids=[10001])
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('address', str(caught.exception))
        self.assertEqual(caught.exception.code, '503')

    def test_redirect_and_write_methods_are_blocked(self):
        client, opener = self.client({'code': 0})
        with self.assertRaises(ValueError):
            client._query('orders.send')
        opener.open.assert_not_called()
        with self.assertRaises(JSTQueryError):
            RejectRedirect().redirect_request(None, None, 302, '', {}, 'https://other.test')
