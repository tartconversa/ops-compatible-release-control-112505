import unittest

from materials import read_feed


class InputsTest(unittest.TestCase):
    def test_routing_overlap_and_control_group_sources(self):
        observations = list(read_feed("observations"))
        measurements = list(read_feed("measurements"))
        builds = list(read_feed("builds"))
        for row in measurements:
            self.assertLessEqual(row["from_ms"], row["to_ms"])
            self.assertLessEqual(row["to_ms"], row["arrived_ms"])
            self.assertLessEqual(row["errors"], row["total"])
        observed = [r for r in measurements if r["region"] == "coast" and r["route_seq"] == 4]
        self.assertEqual({r["served_build"] for r in observed}, {"r11", "r12"})
        self.assertEqual({r["dep"] for r in observed}, {"healthy"})
        shared_impairment = [r for r in measurements if r["route_seq"] == 1 and r["dep"] == "impaired"]
        self.assertEqual({r["served_build"] for r in shared_impairment}, {"r11", "r12"})
        self.assertTrue(any(r["kind"] == "claim" and r["value"] == "compact:r12" for r in observations))
        self.assertFalse(any(r["build"] == "r11" and "compact" in r["input_formats"] for r in builds))

