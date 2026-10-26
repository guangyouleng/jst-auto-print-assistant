"""Exercise actual BAT validation and offline bundled-module checks."""
import ast
import contextlib
import io
import json
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app

ROOT = Path(__file__).resolve().parents[1]


class PackagingRuntimeTests(unittest.TestCase):
    def test_both_bat_config_validators_accept_local_and_reject_bad_configs(self):
        valid = [
            dict(backend_mode='local', debug_port=9222, loop_seconds=5),
            dict(debug_port=9222, loop_seconds=5),
            dict(backend_mode='auto', debug_port=9222, loop_seconds=5),
            dict(api_url='https://example.test/api', api_token='a' * 32,
                 debug_port=9222, loop_seconds=5),
            dict(backend_mode='remote', api_url='https://example.test/api',
                 api_token='a' * 32, debug_port=9222, loop_seconds=5),
        ]
        invalid = [
            dict(backend_mode='remote', debug_port=9222, loop_seconds=5),
            dict(backend_mode='other', debug_port=9222, loop_seconds=5),
            dict(backend_mode='local', debug_port=80, loop_seconds=5),
            dict(backend_mode='local', debug_port=9222, loop_seconds=1),
            dict(backend_mode='local'),
            dict(api_url='https://example.test/api', debug_port=9222, loop_seconds=5),
            dict(backend_mode='remote', api_url='http://example.test/api',
                 api_token='a' * 32, debug_port=9222, loop_seconds=5),
        ]
        for filename in ('build_jst_auto_print_win10_x64.bat', 'Win10_21H1_源码直接启动.bat'):
            lines = (ROOT / filename).read_text(encoding='utf-8-sig').splitlines()
            line = next(line for line in lines if "c=json.loads(pathlib.Path('jst_operator_config.json')" in line)
            code = line.split(' -c "', 1)[1].rsplit('"', 1)[0]
            nodes = ast.parse(code).body
            index = next(i for i, node in enumerate(nodes)
                         if isinstance(node, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == 'c' for t in node.targets))
            validation = compile(ast.Module(body=nodes[index:], type_ignores=[]), filename, 'exec')
            for config in valid + invalid:
                with self.subTest(filename=filename, config=config), \
                     mock.patch.object(Path, 'read_text', return_value=json.dumps(config)), \
                     contextlib.redirect_stdout(io.StringIO()):
                    globals_ = dict(json=json, pathlib=__import__('pathlib'), re=re, struct=struct)
                    if config in valid:
                        exec(validation, globals_)
                    else:
                        with self.assertRaises((AssertionError, ValueError)):
                            exec(validation, globals_)

    def test_local_runtime_checks_only_temporary_state_without_network(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(app, 'APP_DIR', Path(directory)), \
             mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')):
            app.check_local_runtime()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_self_test_rejects_any_missing_single_pc_module(self):
        for name in ('jst_print_shadow_plan', 'jst_local_coordinator', 'jst_local_source',
                     'jst_local_store', 'jst_openapi'):
            with self.subTest(module=name), tempfile.TemporaryDirectory() as directory, \
                 mock.patch.dict(sys.modules, {name: None}), \
                 mock.patch.object(app, 'load_settings', return_value=app.Settings(backend_mode='local')), \
                 mock.patch.object(app, 'tk', mock.Mock()), \
                 mock.patch.object(app, '_module_available', return_value=True), \
                 mock.patch.object(app, 'browser_executable', return_value=''), \
                 mock.patch.object(app, 'print_service_online', return_value=False), \
                 contextlib.redirect_stdout(io.StringIO()):
                output = Path(directory) / 'result.json'
                self.assertEqual(app.self_test(output), 2)
                result = json.loads(output.read_text())
                self.assertFalse(result['local_runtime'])
                self.assertEqual(result['self_test'], 'FAIL')
                self.assertTrue(any('单机模块' in error for error in result['errors']))

    def test_self_test_reports_offline_local_runtime_success(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(app, 'load_settings', return_value=app.Settings(backend_mode='local')), \
             mock.patch.object(app, 'tk', mock.Mock()), \
             mock.patch.object(app, '_module_available', return_value=True), \
             mock.patch.object(app, 'browser_executable', return_value=''), \
             mock.patch.object(app, 'print_service_online', return_value=False), \
             mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')), \
             contextlib.redirect_stdout(io.StringIO()):
            output = Path(directory) / 'result.json'
            self.assertEqual(app.self_test(output), 0)
            result = json.loads(output.read_text())
            self.assertTrue(result['local_runtime'])
            self.assertEqual(result['self_test'], 'PASS')
            self.assertEqual(result['errors'], [])
