import unittest

from materials import list_feeds, read_feed

FIELDS = {
    'builds': ('region', 'build', 'input_formats', 'emit_by_cap', 'ready_slots'),
    'policy': ('region', 'client_cap', 'window_ms', 'min_each', 'max_excess_pp', 'dependency'),
    'observations': ('at_ms', 'kind', 'item', 'region', 'cap', 'seq', 'value'),
    'measurements': ('arrived_ms', 'region', 'cap', 'route_seq', 'from_ms', 'to_ms', 'served_build', 'total', 'errors', 'dep'),
}


class MaterialTest(unittest.TestCase):
    def test_feed_fields(self):
        self.assertEqual(list_feeds(), sorted(FIELDS))
        for name, fields in FIELDS.items():
            with self.subTest(feed=name):
                records = list(read_feed(name))
                self.assertTrue(records)
                self.assertTrue(all(set(row) == set(fields) for row in records))
