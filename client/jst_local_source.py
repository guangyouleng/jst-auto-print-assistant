"""Read-only local planning source; never launches the server or ERP bridge."""
import time
from contextlib import contextmanager
from types import SimpleNamespace

import jst_print_shadow_plan as planner
from jst_openapi import JSTReadonlyClient, JSTQueryError


class LocalSource:
    def __init__(self, config_path, cache_path, *, client=None):
        self.client = client or JSTReadonlyClient(config_path)
        self.cache_path = str(cache_path)

    def check(self):
        return self.client.check()

    @contextmanager
    def _budget(self, seconds):
        deadline = time.monotonic() + seconds
        if isinstance(self.client, JSTReadonlyClient):
            self.client.deadline = deadline
        try:
            yield
            if time.monotonic() > deadline:
                raise JSTQueryError('TIMEOUT')
        finally:
            if isinstance(self.client, JSTReadonlyClient):
                self.client.deadline = None

    def plan(self):
        with self._budget(105):
            return planner.run_live_readonly(SimpleNamespace(
                lookback_hours=168, action_history_days=7, max_candidates=5000,
                candidate_cache=self.cache_path), client=self.client)

    def run(self, arguments, *, timeout=35):
        # These are internal compatibility arguments, never shell commands.
        with self._budget(timeout or 35):
            if '--inspect-o-id' in arguments:
                return planner.run_inspect_order(SimpleNamespace(
                    inspect_o_id=arguments[arguments.index('--inspect-o-id') + 1],
                    inspect_io_id=arguments[arguments.index('--inspect-io-id') + 1],
                    action_history_days=7), client=self.client)
            pairs = [tuple(arguments[i + 1].split(':')) for i, value in enumerate(arguments)
                     if value == '--inspect-pair']
            return planner.run_inspect_batch(SimpleNamespace(
                inspect_pairs=pairs, action_history_days=7), client=self.client)
