"""Credential storage and desktop lifecycle boundaries; no live credentials."""
import base64
import ctypes
import json
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app
import jst_credentials as secrets
from jst_credentials_ui import verify_and_save
from jst_openapi import JSTReadonlyClient, JSTQueryError

VALUES = dict(app_key='test-app', appsecret='test-secret', access_token='test-token')


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'jst_openapi_config.json'
        # Reversible fixture for storage tests only; NOT production encryption.
        self.store = secrets.CredentialStore(self.path, protect=base64.b64encode,
                                             unprotect=base64.b64decode)

    def test_roundtrip_and_plaintext_migration(self):
        self.path.write_text(json.dumps(VALUES))
        self.assertEqual(self.store.save(VALUES), '')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.load(), VALUES)
        for value in VALUES.values():
            self.assertNotIn(value.encode(), self.store.path.read_bytes())
        self.assertEqual(list(self.store.path.parent.glob('.jst-credentials-*')), [])

    def test_missing_corrupt_and_undecryptable_do_not_fallback(self):
        self.path.write_text(json.dumps(VALUES))
        with self.assertRaises(secrets.CredentialError):
            self.store.load()
        self.store.path.write_bytes(b'broken')
        with self.assertRaises(secrets.CredentialError):
            self.store.load()
        self.store.save(VALUES)
        self.store.unprotect = mock.Mock(side_effect=secrets.CredentialError('无法解密'))
        with self.assertRaisesRegex(secrets.CredentialError, '无法解密'):
            self.store.load()

    def test_failed_encryption_or_replace_preserves_original(self):
        self.store.save(VALUES)
        original = self.store.path.read_bytes()
        with mock.patch.object(self.store, 'protect', side_effect=secrets.CredentialError('失败')):
            with self.assertRaises(secrets.CredentialError):
                self.store.save(dict(VALUES, access_token='replacement'))
        self.assertEqual(self.store.path.read_bytes(), original)
        with mock.patch.object(secrets.os, 'replace', side_effect=PermissionError):
            with self.assertRaises(secrets.CredentialError):
                self.store.save(dict(VALUES, access_token='replacement'))
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(list(self.store.path.parent.glob('.jst-credentials-*')), [])

    def test_roundtrip_check_failure_preserves_original(self):
        self.store.save(VALUES)
        original = self.store.path.read_bytes()
        self.store.unprotect = lambda data: json.dumps(dict(VALUES, access_token='wrong')).encode()
        with self.assertRaises(secrets.CredentialError):
            self.store.save(VALUES)
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_validation_failures_never_save_or_leak_values(self):
        for code in ('503', '504', 'NETWORK', 'HTTP_503'):
            with self.subTest(code=code):
                client = mock.Mock()
                client.check.side_effect = JSTQueryError(code)
                with mock.patch.object(self.store, 'save') as save:
                    with self.assertRaises(JSTQueryError) as raised:
                        verify_and_save(self.store, VALUES, client_factory=lambda *a, **k: client)
                    save.assert_not_called()
                    for value in VALUES.values():
                        self.assertNotIn(value, str(raised.exception))

    def test_validation_precedes_save(self):
        client = mock.Mock()
        order = []
        client.check.side_effect = lambda: order.append('query')
        with mock.patch.object(self.store, 'save', side_effect=lambda values: order.append('save')):
            verify_and_save(self.store, VALUES, client_factory=lambda *a, **k: client)
        self.assertEqual(order, ['query', 'save'])

    def test_invalid_fields_never_reach_query(self):
        factory = mock.Mock()
        with self.assertRaises(secrets.CredentialError):
            verify_and_save(self.store, dict(VALUES, access_token=' '), client_factory=factory)
        factory.assert_not_called()

    def test_windows_uses_encrypted_store_even_with_environment(self):
        self.store.save(VALUES)
        self.path.write_text(json.dumps(dict(VALUES, access_token='plaintext')))
        with mock.patch('jst_openapi.sys.platform', 'win32'), \
             mock.patch.object(secrets, 'dpapi', side_effect=lambda data, **kw: base64.b64decode(data)), \
             mock.patch.dict(os.environ, {'JST_ACCESS_TOKEN': 'environment'}):
            self.assertEqual(JSTReadonlyClient(self.path).credentials(), VALUES)
            self.store.path.unlink()
            with self.assertRaises(secrets.CredentialError):
                JSTReadonlyClient(self.path).credentials()

    def desktop(self):
        desktop = object.__new__(app.DesktopApp)
        desktop.settings = app.Settings(backend_mode='local')
        desktop._credential_dialog = None
        desktop._credentials_pending = False
        desktop._closing = False
        desktop._start_check_running = False
        desktop.engine = None
        desktop._credential_store = lambda: self.store
        desktop._open_credentials = mock.Mock()
        return desktop

    def test_missing_or_new_machine_prompts_and_blocks_start(self):
        desktop = self.desktop()
        with mock.patch.object(app.sys, 'platform', 'win32'):
            self.assertFalse(desktop._check_credentials())
        desktop._open_credentials.assert_called_once()
        self.store.save(VALUES)
        desktop._credentials_pending = False
        self.store.unprotect = mock.Mock(side_effect=secrets.CredentialError('已换电脑'))
        with mock.patch.object(app.sys, 'platform', 'win32'):
            self.assertFalse(desktop._check_credentials())
        self.assertIn('已换电脑', desktop._open_credentials.call_args.args[0])

    def test_cancelled_expiry_dialog_reopens_on_next_start(self):
        self.store.save(VALUES)
        desktop = self.desktop()
        desktop._credentials_pending = True
        with mock.patch.object(app.sys, 'platform', 'win32'):
            self.assertFalse(desktop._check_credentials())
        desktop._open_credentials.assert_called_once()

    def test_cannot_edit_with_running_or_paused_engine(self):
        desktop = self.desktop()
        desktop.engine = mock.Mock(is_alive=lambda: True)
        with mock.patch.object(app.sys, 'platform', 'win32'), \
             mock.patch.object(app.messagebox, 'showwarning') as warning:
            app.DesktopApp._open_credentials(desktop)
        warning.assert_called_once()

    def test_expiry_waits_for_stop_before_prompt_and_never_resumes(self):
        desktop = self.desktop()
        desktop.messages = queue.Queue()
        desktop.status_var = mock.Mock()
        desktop.engine = mock.Mock()
        desktop._run_async = lambda task: task()
        desktop._request_credential_update('凭据已失效')
        desktop.engine.stop.assert_called_once_with(wait=True)
        desktop.engine.resume.assert_not_called()
        desktop._open_credentials.assert_not_called()
        self.assertEqual(desktop.messages.get_nowait(), ('credentials_stopped', '凭据已失效'))
        self.assertTrue(desktop._credentials_pending)

    def test_dpapi_does_not_fallback_on_other_platforms(self):
        with mock.patch.object(secrets.sys, 'platform', 'darwin'):
            with self.assertRaises(secrets.CredentialError):
                secrets.dpapi(b'input')

    def test_native_binding_uses_user_scope_and_frees_buffers(self):
        crypt32, kernel32 = mock.Mock(), mock.Mock()
        buffer = ctypes.create_string_buffer(b'protected')

        def protect(incoming, description, entropy, reserved, prompt, flags, outgoing):
            self.assertEqual(ctypes.string_at(incoming._obj.data, incoming._obj.size), b'secret')
            self.assertEqual(flags, 1)  # No machine scope or OS dialogs.
            self.assertIsNone(entropy)
            self.assertIsNone(prompt)
            outgoing._obj.size = len(b'protected')
            outgoing._obj.data = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            return True

        crypt32.CryptProtectData.side_effect = protect
        with mock.patch.object(secrets.sys, 'platform', 'win32'), \
             mock.patch.object(secrets.ctypes, 'WinDLL', create=True,
                               side_effect=[crypt32, kernel32]):
            self.assertEqual(secrets.dpapi(b'secret'), b'protected')
        kernel32.LocalFree.assert_called_once()
        self.assertEqual(buffer.raw[:9], b'\0' * 9)

    @unittest.skipUnless(sys.platform == 'win32', 'requires real Windows DPAPI')
    def test_native_windows_dpapi_roundtrip_and_tamper(self):
        store = secrets.CredentialStore(self.path)
        store.save(VALUES)
        self.assertEqual(store.load(), VALUES)
        for value in VALUES.values():
            self.assertNotIn(value.encode(), store.path.read_bytes())
        store.path.write_bytes(secrets.MAGIC + b'invalid DPAPI data')
        with self.assertRaises(secrets.CredentialError):
            store.load()
