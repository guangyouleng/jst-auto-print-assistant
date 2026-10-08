"""Multi-detail ordering regressions shared by server and client."""
import copy
import itertools
import sys
import unittest
from pathlib import Path

import jst_auto_print_app as app

SERVER_DIR = Path(__file__).resolve().parents[2] / 'server'
sys.path.insert(0, str(SERVER_DIR))
try:
    import jst_print_api_server as api
finally:
    sys.path.remove(str(SERVER_DIR))


def candidate(label, *pairs):
    return {'label': label, 'items': [
        {'product_id': pid, 'sku_id': sid} for pid, sid in pairs]}


class ProductSkuPrintOrderTests(unittest.TestCase):
    def assert_order(self, rows, expected):
        original = copy.deepcopy(rows)
        jobs = [{'label': row['label'], 'plan': {'items': copy.deepcopy(row['items'])}}
                for row in rows]
        original_jobs = copy.deepcopy(jobs)
        for side, ordered in (
            ('server', api.group_candidates_by_product(rows)),
            ('client', app.AutomationEngine._prioritize_product_groups(jobs)),
        ):
            with self.subTest(side=side):
                self.assertEqual([row['label'] for row in ordered], expected)
                self.assertEqual(len(ordered), len(rows))
        self.assertEqual(rows, original)
        self.assertEqual(jobs, original_jobs)

    def test_multiple_skus_use_natural_order_inside_each_order(self):
        rows = [candidate('later', ('A', 'A-3'), ('A', 'A-4')),
                candidate('earlier', ('A', 'A-10'), ('A', 'A-2'))]
        for permutation in itertools.permutations(rows):
            self.assert_order(list(permutation), ['earlier', 'later'])

    def test_complete_product_set_takes_precedence_over_sku(self):
        rows = [candidate('AB-first', ('B', 'B-2'), ('A', 'A-2')),
                candidate('AB-second', ('A', 'A-4'), ('B', 'B-4')),
                candidate('AC', ('A', 'A-3'), ('C', 'C-3'))]
        for permutation in itertools.permutations(rows):
            self.assert_order(list(permutation), ['AB-first', 'AB-second', 'AC'])

    def test_multiple_product_ids_are_naturally_sorted(self):
        rows = [candidate('later-set', ('P3', '0'), ('P4', '0')),
                candidate('earlier-set', ('P10', '99'), ('P2', '99'))]
        for permutation in itertools.permutations(rows):
            self.assert_order(list(permutation), ['earlier-set', 'later-set'])

    def test_equivalent_details_keep_input_order_despite_duplicates(self):
        rows = [candidate('first', (' A ', ' A-10 '), ('A', 'A-2')),
                candidate('second', ('A', 'A-2'), ('A', 'A-10'), ('A', 'A-2')),
                candidate('earlier', ('A', 'A-1'))]
        self.assert_order(rows, ['earlier', 'first', 'second'])

    def test_incomplete_identities_remain_last_in_input_order(self):
        rows = [candidate('empty'), candidate('valid-later', ('B', 'B-1')),
                candidate('partial', ('A', 'A-1'), ('B', '')),
                candidate('valid-first', ('A', 'A-2')),
                candidate('missing-product', ('', 'A-1'))]
        self.assert_order(rows, ['valid-first', 'valid-later', 'empty', 'partial', 'missing-product'])


if __name__ == '__main__':
    unittest.main()
