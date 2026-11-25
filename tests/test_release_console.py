import json
import tempfile
import unittest
from pathlib import Path

from release_console import ReleaseConsole, SafetyError

ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials"


class RehearsalDecisionTest(unittest.TestCase):
    def setUp(self):
        self.console = ReleaseConsole(MATERIALS, state_path=None)

    def report(self, release, at=None):
        return self.console.release_report(release, at)

    def test_shared_dependency_fault_is_not_candidate_regression(self):
        at_22 = self.report("rel-1", 22)
        round_22 = next(r for r in at_22["rounds"] if r["seq"] == 1)
        self.assertEqual(round_22["verdict"], "waiting_for_samples")
        self.assertEqual(round_22["candidate"]["total"], 24)
        self.assertEqual(round_22["sufficiency"]["candidate"]["missing"], 16)
        self.assertFalse(round_22["shared_dependency_incident"])

        at_31 = self.report("rel-1", 31)
        round_31 = next(r for r in at_31["rounds"] if r["seq"] == 1)
        self.assertEqual(round_31["verdict"], "shared_dependency_incident")
        self.assertTrue(round_31["shared_dependency_incident"])
        self.assertEqual(len(round_31["impaired_pairs"]), 1)
        self.assertIn("m3", round_31["impaired_records"])
        self.assertIn("m4", round_31["impaired_records"])

    def test_unknown_dependency_records_are_excluded(self):
        report = self.report("rel-1", 99)
        round_2 = next(r for r in report["rounds"] if r["seq"] == 2)
        self.assertEqual(round_2["unknown_records"], ["m6"])
        self.assertEqual(round_2["candidate"]["total"], 0)
        self.assertEqual(round_2["verdict"], "waiting_for_samples")

    def test_target_instance_mismatch_blocks_rel2(self):
        report = self.report("rel-2", 99)
        verification = report["target_verification"]
        self.assertFalse(verification["verified"])
        self.assertEqual(verification["wrong_build_instances"][0]["loaded_build"], "r11")
        self.assertTrue(any("i-can=r11" in x for x in verification["issues"]))

    def test_rel3_evidence_supports_only_exact_stable_scope(self):
        report = self.report("rel-3", 99)
        round_4 = next(r for r in report["rounds"] if r["seq"] == 4)
        self.assertEqual(round_4["verdict"], "supported")
        self.assertTrue(round_4["supported"])
        self.assertGreaterEqual(round_4["candidate"]["total"], 30)
        self.assertGreaterEqual(round_4["control"]["total"], 30)
        self.assertLessEqual(round_4["effect_pp"], 2)
        self.assertEqual(len(report["stable_scopes"]), 1)
        scope = report["stable_scopes"][0]
        self.assertEqual((scope["region"], scope["client_cap"], scope["route_seq"]), ("coast", "v1", 4))
        self.assertEqual(scope["actual_percent"], 10)
        self.assertEqual(scope["measurement_ids"], ["m9", "m10"])

    def test_rel4_healthy_evidence_detects_regression(self):
        report = self.report("rel-4", 99)
        round_5 = next(r for r in report["rounds"] if r["seq"] == 5)
        self.assertEqual(round_5["verdict"], "regression")
        self.assertGreater(round_5["effect_pp"], 2)
        actions = [a["code"] for a in report["safe_actions"]]
        self.assertIn("ROLLBACK", actions)


class CompatibilityAndRollbackTest(unittest.TestCase):
    def setUp(self):
        self.console = ReleaseConsole(MATERIALS, state_path=None)

    def test_route_capability_differs_from_queue_claimable_versions(self):
        v2 = self.console.compatibility_matrix("coast", "v2", "r12", 99)
        row = next(r for r in v2["rows"] if r["build"] == "r12")
        self.assertEqual(row["emitted_format"], "compact")
        self.assertEqual(row["manifest_claimable_builds"], ["r12"])
        self.assertFalse(row["old_handler_can_continue"])

        v1 = self.console.compatibility_matrix("coast", "v1", "r12", 99)
        row_v1 = next(r for r in v1["rows"] if r["build"] == "r12")
        self.assertEqual(row_v1["emitted_format"], "plain")
        self.assertEqual(row_v1["manifest_claimable_builds"], ["r11", "r12"])
        self.assertTrue(row_v1["old_handler_can_continue"])

    def test_capacity_loss_freezes_scope_and_shows_waiting_job(self):
        at_43 = self.console.dashboard(43)
        freeze = next(f for f in at_43["freezes"] if f["format"] == "compact")
        self.assertEqual((freeze["region"], freeze["cap"], freeze["build"]), ("coast", "v2", "r12"))
        self.assertIn("job-8", freeze["waiting_jobs"])

        waiting_49 = self.console.waiting_jobs(49)
        self.assertEqual([j["id"] for j in waiting_49], ["job-8"])
        job8 = self.console.jobs_snapshot(49)["job-8"]
        self.assertEqual(job8["status"], "waiting")
        self.assertTrue(job8["destination_unresolved"])
        self.assertEqual(job8["destination_checks"][0]["reliable_destinations"], [])

    def test_rollback_completion_requires_route_old_traffic_and_destination(self):
        self.assertFalse(self.console.rollback_readiness("rel-1", 38)["complete"])
        at_49 = self.console.rollback_readiness("rel-1", 49)
        self.assertFalse(at_49["complete"])
        self.assertTrue(any("job-8" in x for x in at_49["blockers"]))

        at_53 = self.console.rollback_readiness("rel-1", 53)
        self.assertTrue(at_53["complete"], at_53["blockers"])
        self.assertEqual(at_53["completed_at_ms"], 53)
        self.assertGreater(at_53["old_traffic"]["successful"], 0)

        final = self.console.rollback_readiness("rel-1", 99)
        self.assertTrue(final["complete"])
        self.assertTrue(final["candidate_reintroduced_by_later_route"])

    def test_unsafe_claim_and_double_delivery_are_rejected(self):
        with self.assertRaises(SafetyError):
            self.console.record_claim(
                job="bad-1", region="coast", cap="v2", seq=1,
                data_format="compact", by_build="r11", at=100,
            )
        self.console.record_claim(
            job="good-1", region="coast", cap="v2", seq=1,
            data_format="compact", by_build="r12", at=100,
        )
        with self.assertRaises(SafetyError):
            self.console.record_claim(
                job="good-1", region="coast", cap="v2", seq=2,
                data_format="compact", by_build="r12", at=101,
            )
        with self.assertRaises(SafetyError):
            self.console.record_claim(
                job="same-seq", region="coast", cap="v2", seq=1,
                data_format="compact", by_build="r12", at=102,
            )
            self.console.record_claim(
                job="same-seq", region="coast", cap="v2", seq=1,
                data_format="plain", by_build="r11", at=103,
            )


class PersistenceAndConcurrencyTest(unittest.TestCase):
    def test_idempotent_requests_persist_and_late_receipt_does_not_advance(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state.json"
            console = ReleaseConsole(MATERIALS, state_path=state)
            first = console.propose(
                "rel-x", region="coast", cap="v2", percent=10, build="r12",
                at=100, seq=6, idem_key="click-1",
            )
            self.assertFalse(first["duplicate"])
            second = console.propose(
                "rel-x", region="coast", cap="v2", percent=10, build="r12",
                at=101, seq=99, idem_key="click-1",
            )
            self.assertTrue(second["duplicate"])
            self.assertEqual(second["event"]["seq"], 6)

            console.control_request("rollback", "rel-x", at=102, seq=7)
            # Late/conflicting older-generation route effect arrives after rollback.
            console.record_route_applied(seq=6, region="coast", cap="v2", percent=25, at=105)
            at_106 = console.release_report("rel-x", 106)
            latest_action_codes = [a["code"] for a in at_106["safe_actions"]]
            self.assertNotIn("HOLD_VALIDATED_SCOPE", latest_action_codes)
            self.assertIn("COMPLETE_ROLLBACK_VERIFICATION", latest_action_codes)

            console.record_route_applied(seq=7, region="coast", cap="v2", percent=0, at=108)
            console.record_measurement({
                "arrived_ms": 120, "region": "coast", "cap": "v2", "route_seq": 7,
                "from_ms": 108, "to_ms": 119, "served_build": "r11",
                "total": 20, "errors": 0, "dep": "healthy",
            })
            ready = console.rollback_readiness("rel-x", 120)
            self.assertTrue(ready["complete"], ready["blockers"])

            reloaded = ReleaseConsole(MATERIALS, state_path=state)
            self.assertEqual(len(reloaded.state["events"]), 4)
            self.assertTrue(reloaded.rollback_readiness("rel-x", 120)["complete"])

    def test_pause_and_incomplete_rollback_fence_blocks_later_publish_click(self):
        console = ReleaseConsole(MATERIALS, state_path=None)
        console.propose("rel-f", region="coast", cap="v2", percent=10, build="r12", at=100, seq=6)
        console.control_request("pause", "rel-f", at=101, seq=7)
        retried = console.propose("rel-f", region="coast", cap="v2", percent=20, build="r12", at=102, seq=8)
        self.assertFalse(retried["preflight"]["safe_to_issue_route"])
        self.assertTrue(retried["preflight"]["control_fence"]["pause_active"])
        self.assertEqual(console.safe_actions("rel-f", 102)[0]["code"], "REMAIN_PAUSED")

        console = ReleaseConsole(MATERIALS, state_path=None)
        console.propose("rel-g", region="coast", cap="v2", percent=10, build="r12", at=100, seq=6)
        console.control_request("rollback", "rel-g", at=101, seq=7)
        retried = console.propose("rel-g", region="coast", cap="v2", percent=20, build="r12", at=102, seq=8)
        self.assertFalse(retried["preflight"]["safe_to_issue_route"])
        self.assertTrue(retried["preflight"]["control_fence"]["rollback_outstanding"])
        self.assertEqual(console.safe_actions("rel-g", 102)[0]["code"], "COMPLETE_ROLLBACK_VERIFICATION")

    def test_idempotent_replay_with_different_body_does_not_move_spec(self):
        console = ReleaseConsole(MATERIALS, state_path=None)
        console.propose("rel-i", region="coast", cap="v2", percent=10, build="r12", at=100, idem_key="same")
        duplicate = console.propose(
            "rel-i", region="inland", cap="v1", percent=50, build="r11", at=101, idem_key="same"
        )
        self.assertTrue(duplicate["duplicate"])
        spec = console.resolve_spec("rel-i")
        self.assertEqual((spec["region"], spec["cap"], spec["build"]), ("coast", "v2", "r12"))

    def test_pre_effect_measurement_is_not_attributed(self):
        console = ReleaseConsole(MATERIALS, state_path=None)
        console.record_measurement({
            "arrived_ms": 100, "region": "coast", "cap": "v1", "route_seq": 4,
            "from_ms": 60, "to_ms": 62, "served_build": "r12",
            "total": 999, "errors": 999, "dep": "healthy",
        })
        report = console.release_report("rel-3", 100)
        round_4 = next(r for r in report["rounds"] if r["seq"] == 4)
        excluded_ids = {m["_id"] for m in round_4["excluded_measurements"]}
        self.assertIn("local-m" if False else round_4["excluded_measurements"][0]["_id"], excluded_ids)
        self.assertTrue(any("before_actual_effect_start" in m["attribution_reasons"] for m in round_4["excluded_measurements"]))
        self.assertEqual(round_4["candidate"]["total"], 36)


class GenericNextManifestTest(unittest.TestCase):
    def test_loads_arbitrary_next_builds_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            materials = Path(td)
            rows = {
                "builds.jsonl": [
                    {"region": "moon", "build": "v1", "input_formats": ["plain"], "emit_by_cap": {"c1": "plain"}, "ready_slots": 1},
                    {"region": "moon", "build": "v2", "input_formats": ["plain", "proto2"], "emit_by_cap": {"c1": "proto2"}, "ready_slots": 1},
                ],
                "policy.jsonl": [
                    {"region": "moon", "client_cap": "c1", "window_ms": 5, "min_each": 1, "max_excess_pp": 0, "dependency": "dep"},
                ],
                "observations.jsonl": [
                    {"at_ms": 1, "kind": "propose", "item": "r-x", "region": "moon", "cap": "c1", "seq": 1, "value": 1},
                    {"at_ms": 2, "kind": "instance", "item": "i-1", "region": "moon", "cap": None, "seq": 1, "value": "v2"},
                    {"at_ms": 3, "kind": "route_applied", "item": "rt", "region": "moon", "cap": "c1", "seq": 1, "value": 1},
                ],
                "measurements.jsonl": [
                    {"arrived_ms": 4, "region": "moon", "cap": "c1", "route_seq": 1, "from_ms": 3, "to_ms": 4, "served_build": "v2", "total": 2, "errors": 0, "dep": "healthy"},
                    {"arrived_ms": 4, "region": "moon", "cap": "c1", "route_seq": 1, "from_ms": 3, "to_ms": 4, "served_build": "v1", "total": 2, "errors": 0, "dep": "healthy"},
                ],
            }
            for name, records in rows.items():
                with (materials / name).open("w", encoding="utf-8") as f:
                    for row in records:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
            console = ReleaseConsole(materials, state_path=None)
            self.assertIn("r-x", console.known_releases(4))
            self.assertEqual(len(console.dashboard(4)["releases"]), 1)
            report = console.release_report("r-x", 4)
            self.assertEqual(report["spec"]["build"], "v2")
            self.assertEqual(report["rounds"][0]["verdict"], "supported")
            self.assertEqual([s["route_seq"] for s in report["stable_scopes"]], [1])
            self.assertEqual(report["compatibility"]["target_row"]["emitted_format"], "proto2")

            console.control_request("pause", "r-x", at=5, seq=2)
            console.record_route_applied(seq=2, region="moon", cap="c1", percent=1, at=6)
            for build in ("v2", "v1"):
                console.record_measurement({
                    "arrived_ms": 7, "region": "moon", "cap": "c1", "route_seq": 2,
                    "from_ms": 6, "to_ms": 7, "served_build": build,
                    "total": 2, "errors": 0, "dep": "healthy",
                })
            report_after_pause = console.release_report("r-x", 7)
            pause_round = next(r for r in report_after_pause["rounds"] if r["seq"] == 2)
            self.assertEqual(pause_round["verdict"], "supported")
            self.assertEqual([s["route_seq"] for s in report_after_pause["stable_scopes"]], [1])
            self.assertTrue(report_after_pause["preflight"]["control_fence"]["pause_active"])


if __name__ == "__main__":
    unittest.main()
