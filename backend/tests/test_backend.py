"""Backend core tests. Run from the backend folder: python3 -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interconnect_backend import engine  # noqa: E402
from interconnect_backend.engine import rank_tests, run_experiment, simulate, to2  # noqa: E402
from interconnect_backend.store import ExperimentStore  # noqa: E402

TRANSFERS_PER_INTERVAL = 10
INTERVAL_CYCLES = TRANSFERS_PER_INTERVAL * engine.CYCLES_PER_TRANSFER


def interval_of(event):
    return event["cyc"] // INTERVAL_CYCLES


def without_ids(events):
    return [{k: v for k, v in e.items() if k != "id"} for e in events]


class SimulationTests(unittest.TestCase):
    def test_healthy_baseline_has_no_anomalies(self):
        r = run_experiment("healthy")
        self.assertEqual((r["totals"]["er"], r["totals"]["dr"], r["totals"]["ex"]), (0, 0, 0))
        self.assertEqual(r["totals"]["tr"], 6 * 200)
        self.assertTrue(all(m["st"] == "healthy" for m in r["links"].values()))

    def test_bit_corruption_flips_only_bits_3_and_17_on_L3(self):
        ev = [e for e in run_experiment("bit")["events"] if e["type"] == "data_mismatch"]
        self.assertGreater(len(ev), 0)
        self.assertTrue(all(e["link"] == "L3" for e in ev))
        self.assertTrue(all((int(e["exp"], 16) ^ int(e["obs"], 16)) == ((1 << 3) | (1 << 17)) for e in ev))
        self.assertTrue(all(4 <= interval_of(e) <= 17 for e in ev))

    def test_transfer_delay_exceeds_threshold_only_on_L1_inside_window(self):
        ev = [e for e in run_experiment("delay")["events"] if e["type"] == "latency_excursion"]
        self.assertEqual(len(ev), 16 * TRANSFERS_PER_INTERVAL)
        self.assertTrue(all(e["link"] == "L1" and e["lat"] > 8 for e in ev))
        self.assertTrue(all(2 <= interval_of(e) <= 17 for e in ev))

    def test_drop_policy_drops_every_seventh_transfer_on_L5(self):
        r = run_experiment("drop")
        ev = [e for e in r["events"] if e["type"] == "dropped_transfer"]
        self.assertEqual(len(ev), 200 // 7)
        self.assertTrue(all(e["link"] == "L5" and e["lat"] is None for e in ev))
        self.assertEqual(r["links"]["L5"]["st"], "faulted")  # 28 of 200 is above 10 percent

    def test_intermittent_bursts_stay_inside_their_windows(self):
        ev = run_experiment("inter")["events"]
        self.assertGreater(len(ev), 0)
        self.assertTrue(all(e["link"] == "L2" and e["type"] == "data_mismatch" for e in ev))
        self.assertTrue({interval_of(e) for e in ev} <= {3, 4, 5, 11, 12, 13})

    def test_multiple_faults_hit_L1_and_L4_only(self):
        r = run_experiment("multi")
        kinds = {(e["link"], e["type"]) for e in r["events"]}
        self.assertTrue(kinds <= {("L1", "latency_excursion"), ("L4", "dropped_transfer")})
        self.assertGreater(r["totals"]["dr"], 0)
        self.assertGreater(r["totals"]["ex"], 0)

    def test_same_seed_reproduces_and_new_seed_changes_payloads(self):
        a, b = run_experiment("bit"), run_experiment("bit")
        self.assertEqual(a["links"], b["links"])
        self.assertEqual(without_ids(a["events"]), without_ids(b["events"]))
        c = run_experiment("bit", seed=999)
        self.assertNotEqual(without_ids(a["events"]), without_ids(c["events"]))

    def test_totals_agree_with_per_link_metrics(self):
        r = run_experiment("multi")
        links, t = list(r["links"].values()), r["totals"]
        self.assertEqual(t["tr"], sum(m["tr"] for m in links))
        self.assertEqual(sum(m["rc"] for m in links) + t["dr"], t["tr"])
        self.assertEqual(t["avg"], to2(sum(m["sum"] for m in links) / sum(m["rc"] for m in links)))
        self.assertEqual(to2(4.125), 4.13)  # JavaScript toFixed rounds ties up

    def test_low_rate_fault_is_suspected_not_faulted(self):
        sc = {"id": "probe", "seed": 7,
              "faults": [{"link": "L2", "type": "bit", "bits": [0], "start": 0, "dur": 2}]}
        per_link, _ = simulate(sc)
        self.assertGreater(per_link["L2"]["an_"], 0)
        self.assertEqual(per_link["L2"]["st"], "suspected")


class PlannerTests(unittest.TestCase):
    def test_planner_ranks_the_faulted_link_first(self):
        top = rank_tests(run_experiment("bit"), [])[0]
        self.assertEqual(top["links"], ["L3"])

    def test_planner_adapts_when_the_top_test_passes(self):
        r = run_experiment("bit")
        top = rank_tests(r, [])[0]
        after = rank_tests(r, [{"id": top["id"], "fail": False}])
        self.assertNotEqual(after[0]["id"], top["id"])
        self.assertNotIn(top["id"], [t["id"] for t in after])

    def test_no_anomalies_means_nothing_to_rank(self):
        self.assertEqual(rank_tests(run_experiment("healthy"), []), [])


class ValidationTests(unittest.TestCase):
    def test_bad_requests_are_rejected(self):
        with self.assertRaises(ValueError):
            run_experiment("missing")
        with self.assertRaises(ValueError):
            run_experiment("healthy", chiplets=1)
        with self.assertRaises(ValueError):
            run_experiment("healthy", chiplets=9, preset="full")
        with self.assertRaises(ValueError):
            run_experiment("healthy", preset="torus")


class PersistenceTests(unittest.TestCase):
    def test_completed_run_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.sqlite3")
            store = ExperimentStore(path)
            saved = run_experiment("drop", store)
            store.close()
            reopened = ExperimentStore(path)
            try:
                self.assertEqual(reopened.get(saved["id"]), saved)
                self.assertEqual([s["id"] for s in reopened.list_summaries()], [saved["id"]])
                self.assertIsNone(reopened.get("EXP-MISSING"))
            finally:
                reopened.close()

    def test_failed_run_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            store = ExperimentStore(os.path.join(d, "history.sqlite3"))
            try:
                with mock.patch.object(engine, "simulate", side_effect=RuntimeError("boom")):
                    with self.assertRaises(RuntimeError):
                        run_experiment("bit", store)
                self.assertEqual(store.list_summaries(), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
