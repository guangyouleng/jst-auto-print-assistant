"""Read-only JST OpenAPI queries migrated from the supplied ERP bridge.

Uses the bridge's existing gateway, signing algorithm and pagination names.
No ERP writes, token refresh, database framework or web service is included.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

GATEWAY = 'https://open.erp321.com/api/open/query.aspx'
FIELDS = {'app_key': 'JST_APP_KEY', 'appsecret': 'JST_APP_SECRET',
          'access_token': 'JST_ACCESS_TOKEN'}


class JSTQueryError(RuntimeError):
    def __init__(self, code):
        value = str(code)
        self.code = value if re.fullmatch(r"[0-9]{1,6}|HTTP_[0-9]{3}|NETWORK|TIMEOUT|REDIRECT|OVERSIZE|INVALID_JSON|INCOMPLETE_PAGINATION|INVALID_RESPONSE", value) else "INVALID_RESPONSE"
        # Remote messages and URLs may contain credentials or order PII.
        super().__init__(f'聚水潭只读查询失败（错误码 {self.code}）；请检查凭据、权限及网络')


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise JSTQueryError('REDIRECT')


class JSTReadonlyClient:
    def __init__(self, config_path: Path, *, credentials=None, opener=None):
        self.config_path = Path(config_path)
        self.deadline = None
        self._credentials = credentials
        self._opener = opener or urllib.request.build_opener(RejectRedirect())

    def credentials(self):
        if self._credentials is not None:
            values = dict(self._credentials)
        else:
            values = {}
            if self.config_path.exists():
                from jst_auto_print_app import _tighten_private_permissions
                _tighten_private_permissions(self.config_path)
                try:
                    values = json.loads(self.config_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    raise RuntimeError('本机聚水潭凭据文件无法读取') from None
                if not isinstance(values, dict):
                    raise RuntimeError('本机聚水潭凭据文件必须为 JSON 对象')
            for field, env in FIELDS.items():
                if os.environ.get(env):
                    values[field] = os.environ[env]
        if any(not isinstance(values.get(field), str) or not values[field].strip()
               for field in FIELDS):
            raise RuntimeError(f'请在 {self.config_path} 配置 app_key、appsecret、access_token，或设置对应 JST 环境变量')
        return {field: values[field] for field in FIELDS}

    def _query(self, method, *, page_no=1, page_size=100, **kwargs):
        if method not in {'shops.query', 'orders.out.simple.query', 'order.action.query'}:
            raise ValueError('只允许店铺、订单和操作历史的只读查询')
        remaining = 30.0 if self.deadline is None else min(30.0, self.deadline - time.monotonic())
        if remaining <= 0:
            raise JSTQueryError('TIMEOUT')
        credentials = self.credentials()
        ts = str(int(time.time()))
        sign = hashlib.md5((method + credentials['app_key'] + 'token'
            + credentials['access_token'] + 'ts' + ts
            + credentials['appsecret']).encode('utf-8')).hexdigest()
        params = {'method': method, 'partnerid': credentials['app_key'],
                  'token': credentials['access_token'], 'ts': ts, 'sign': sign}
        body = {key: value for key, value in kwargs.items() if value is not None}
        if method != 'shops.query':
            body.update(page_index=page_no, page_size=page_size)
        request = urllib.request.Request(GATEWAY + '?' + urlencode(params),
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'), method='POST',
            headers={'Content-Type': 'application/json'})
        try:
            with self._opener.open(request, timeout=remaining) as response:
                raw = response.read(8_000_001)
        except urllib.error.HTTPError as exc:
            raise JSTQueryError(f'HTTP_{exc.code}') from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise JSTQueryError('NETWORK') from None
        if len(raw) > 8_000_000:
            raise JSTQueryError('OVERSIZE')
        try:
            result = json.loads(raw.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise JSTQueryError('INVALID_JSON') from None
        if not isinstance(result, dict) or str(result.get('code')) != '0':
            raise JSTQueryError(result.get('code', 'INVALID_RESPONSE')
                                if isinstance(result, dict) else 'INVALID_RESPONSE')
        if method != 'shops.query':
            if (not isinstance(result.get('datas'), list)
                    or type(result.get('has_next')) is not bool
                    or any(not isinstance(row, dict) for row in result['datas'])):
                raise JSTQueryError('INCOMPLETE_PAGINATION')
        return result

    def query_orders_out(self, **kwargs):
        return self._query('orders.out.simple.query', **kwargs)

    def query_order_action(self, **kwargs):
        return self._query('order.action.query', **kwargs)

    def check(self):
        # Check only a permission actually required by printing. Shops may
        # have a separate grant even when order/history queries are allowed.
        from datetime import timedelta
        from jst_print_shadow_plan import business_now
        now = business_now()
        self.query_orders_out(
            page_no=1, page_size=1,
            modified_begin=(now - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'),
            modified_end=now.strftime('%Y-%m-%d %H:%M:%S'),
        )
        return True
