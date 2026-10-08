"""Windows current-user DPAPI storage. Never falls back to plaintext on failure."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
from pathlib import Path

FIELDS = ('app_key', 'appsecret', 'access_token')
MAGIC = b'JST-DPAPI-1\n'
MAX_BYTES = 65536


class CredentialError(RuntimeError):
    """Safe, user-visible messages only; never include secrets or native errors."""


def validate_credentials(values):
    if not isinstance(values, dict) or any(
        not isinstance(values.get(field), str) or not values[field].strip()
        or len(values[field]) > 8192 for field in FIELDS
    ):
        raise CredentialError('请完整填写 app_key、appsecret 和 access_token')
    return {field: values[field].strip() for field in FIELDS}


def dpapi(data: bytes, *, decrypt=False) -> bytes:
    if sys.platform != 'win32':
        raise CredentialError('DPAPI 加密存储仅支持 Windows；此系统请使用原本机配置方式')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                         ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    # UI_FORBIDDEN only (1), never LOCAL_MACHINE (4): protect the current user.
    success = function(ctypes.byref(incoming), None if decrypt else 'JST OpenAPI credentials',
                       None, None, None, 1, ctypes.byref(outgoing))
    try:
        if not success:
            raise CredentialError(
                '凭据无法解密：可能已更换电脑、Windows 用户或配置已损坏，请重新填写'
                if decrypt else 'Windows 凭据加密失败，未保存配置')
        if outgoing.size > MAX_BYTES:
            raise CredentialError('凭据数据无效，请重新填写')
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if outgoing.data:
            ctypes.memset(outgoing.data, 0, outgoing.size)
            kernel32.LocalFree(outgoing.data)


class CredentialStore:
    def __init__(self, config_path: Path, *, protect=None, unprotect=None):
        self.legacy_path = Path(config_path)
        self.path = self.legacy_path.with_suffix('.dpapi')
        self.protect = protect or dpapi
        self.unprotect = unprotect or (lambda data: dpapi(data, decrypt=True))

    def load(self):
        try:
            with self.path.open('rb') as source:
                data = source.read(MAX_BYTES + 1)
        except FileNotFoundError:
            raise CredentialError('尚未配置加密凭据，请填写聚水潭凭据') from None
        except OSError:
            raise CredentialError('无法读取加密凭据文件，请检查本机权限') from None
        if len(data) > MAX_BYTES or not data.startswith(MAGIC):
            raise CredentialError('加密凭据文件已损坏，请重新填写')
        try:
            return validate_credentials(json.loads(self.unprotect(data[len(MAGIC):]).decode('utf-8')))
        except (UnicodeError, ValueError):
            raise CredentialError('加密凭据内容无效，请重新填写') from None

    def legacy_values(self):
        """For explicit UI migration only; not a Windows runtime fallback."""
        try:
            with self.legacy_path.open('r', encoding='utf-8') as source:
                values = json.loads(source.read(MAX_BYTES + 1))
            if not isinstance(values, dict):
                values = {}
        except (OSError, ValueError):
            values = {}
        for field in FIELDS:
            env = {'app_key': 'JST_APP_KEY', 'appsecret': 'JST_APP_SECRET',
                   'access_token': 'JST_ACCESS_TOKEN'}[field]
            if os.environ.get(env):
                values[field] = os.environ[env]
        return {field: values.get(field, '') if isinstance(values.get(field, ''), str) else ''
                for field in FIELDS}

    def save(self, values):
        values = validate_credentials(values)
        ciphertext = self.protect(json.dumps(values, ensure_ascii=False).encode('utf-8'))
        # Verify the new blob before replacing a previously working configuration.
        try:
            recovered = validate_credentials(json.loads(self.unprotect(ciphertext).decode('utf-8')))
        except (ValueError, UnicodeError):
            raise CredentialError('加密凭据校验失败，未替换原配置') from None
        if recovered != values or len(ciphertext) + len(MAGIC) > MAX_BYTES:
            raise CredentialError('加密凭据校验失败，未替换原配置')
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='.jst-credentials-',
                                             delete=False) as target:
                temporary = Path(target.name)
                target.write(MAGIC + ciphertext)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise CredentialError('加密凭据保存失败，请检查本机目录权限') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        # Saving is an explicit migration: remove the old plaintext copy only
        # after the encrypted replacement is durable. Report failed cleanup.
        try:
            self.legacy_path.unlink(missing_ok=True)
        except OSError:
            return '凭据已加密保存，但旧明文文件未能删除，请手动删除 jst_openapi_config.json'
        return ''
