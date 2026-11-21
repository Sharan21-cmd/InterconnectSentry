"""Chiplet-interconnect simulation, diagnosis, and adaptive test ranking.

Python port of the browser Demo Lab model in interconnectsentry-preview.html
(sim, susp, cand and TESTS). tools/parity_check.py verifies that the two agree.

Units: time is counted in intervals. Latency and event timestamps are model cycles,
not measured hardware clock cycles. Link status and planner scores are heuristics.
"""
import datetime
import math
import uuid
from decimal import ROUND_HALF_UP, Decimal

M32 = 0xFFFFFFFF
INTERVALS = 20
CYCLES_PER_TRANSFER = 8
MAX_CHIPLETS = 16
NAMES = "ABCDEFGHIJKLMNOP"
PRESETS = ("full", "ring", "star", "mesh")

SCENARIOS = [
    {"id": "healthy", "name": "Healthy Baseline", "seed": 101,
     "desc": "No fault injected.", "faults": []},
    {"id": "bit", "name": "Bit Corruption", "seed": 202,
     "desc": "Bits 3 and 17 flipped on L3 (intervals 4–17).",
     "faults": [{"link": "L3", "type": "bit", "bits": [3, 17], "start": 4, "dur": 14}]},
    {"id": "delay", "name": "Transfer Delay", "seed": 303,
     "desc": "+14 cycles on L1 (intervals 2–17).",
     "faults": [{"link": "L1", "type": "delay", "delay": 14, "start": 2, "dur": 16}]},
    {"id": "drop", "name": "Dropped Transfer", "seed": 404,
     "desc": "Every 7th transfer on L5 dropped.",
     "faults": [{"link": "L5", "type": "drop", "every": 7, "start": 0, "dur": 20}]},
    {"id": "inter", "name": "Intermittent Link Fault", "seed": 505,
     "desc": "Corruption bursts on L2 (intervals 3–5 and 11–13).",
     "faults": [{"link": "L2", "type": "bit", "bits": [0], "start": 3, "dur": 3},
                {"link": "L2", "type": "bit", "bits": [0], "start": 11, "dur": 3}]},
    {"id": "multi", "name": "Multiple Faults", "seed": 606,
     "desc": "Delay on L1 plus drops on L4.",
     "faults": [{"link": "L1", "type": "delay", "delay": 9, "start": 5, "dur": 12},
                {"link": "L4", "type": "drop", "every": 5, "start": 3, "dur": 14}]},
]
SCENARIO_BY_ID = {s["id"]: s for s in SCENARIOS}


def to2(x):
    """Round like JavaScript's toFixed(2): exact binary value, ties round up."""
    return float(Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def hx(x):
    return f"0x{x & M32:08x}"


class Rng:
    """mulberry32, the same generator as the browser model's rng()."""

    def __init__(self, seed):
        self.a = seed & M32

    def __call__(self):
        self.a = (self.a + 0x6D2B79F5) & M32
        t = self.a
        t = ((t ^ (t >> 15)) * (t | 1)) & M32
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & M32)) & M32
        return ((t ^ (t >> 14)) & M32) / 4294967296


def validate(chiplets, preset, tpi, lat):
    if not 2 <= chiplets <= MAX_CHIPLETS:
        raise ValueError(f"chiplets must be between 2 and {MAX_CHIPLETS}")
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {', '.join(PRESETS)}")
    if preset == "full" and chiplets > 8:
        raise ValueError("the fully connected preset is limited to 8 chiplets")
    if not 1 <= lat <= 32:
        raise ValueError("base latency must be between 1 and 32 cycles")
    if not 1 <= tpi <= 50:
        raise ValueError("transfers per interval must be between 1 and 50")


def pairs(n, preset):
    if preset == "ring":
        return [(i, (i + 1) % n) for i in range(n)]
    if preset == "star":
        return [(0, i) for i in range(1, n)]
    if preset == "full":
        return [(i, j) for i in range(n) for j in range(i + 1, n)]
    c = math.ceil(math.sqrt(n))  # mesh
    out = []
    for i in range(n):
        if i % c < c - 1 and i + 1 < n:
            out.append((i, i + 1))
        if i + c < n:
            out.append((i, i + c))
    return out


def topology(chiplets, preset, lat):
    return [{"id": f"L{k + 1}", "s": NAMES[a], "d": NAMES[b], "lat": lat}
            for k, (a, b) in enumerate(pairs(chiplets, preset))]


def simulate(scenario, chiplets=4, preset="full", tpi=10, lat=4):
    """Run one scenario and return (per_link, events). Same seed and config give the same output."""
    links = topology(chiplets, preset, lat)
    ids = [link["id"] for link in links]
    faults = [{**f, "link": ids[(int(f["link"][1:]) - 1) % len(ids)]} for f in scenario["faults"]]
    rng = Rng(scenario["seed"])
    per_link, events = {}, []
    for link in links:
        m = {"tr": 0, "er": 0, "dr": 0, "ex": 0, "sum": 0, "rc": 0, "mx": 0,
             "lat": [], "an": [], "rcv": []}
        for i in range(INTERVALS):
            ls = a = rc = 0
            for k in range(tpi):
                idx = i * tpi + k
                exp = int(rng() * 4294967296)
                lt = link["lat"] + int(rng() * 3)
                kind, sev, obs, dropped = None, "", None, False
                f = next((f for f in faults if f["link"] == link["id"]
                          and f["start"] <= i < f["start"] + f["dur"]), None)
                if f is not None:
                    if f["type"] == "bit" and rng() < 0.6:
                        mask = 0
                        for b in f["bits"]:
                            mask |= 1 << b
                        obs, kind, sev = (exp ^ mask) & M32, "data_mismatch", "error"
                        m["er"] += 1
                    elif f["type"] == "delay":
                        lt += f["delay"]
                        if lt > link["lat"] + 4:
                            kind = "protocol_violation" if lt > 32 else "latency_excursion"
                            sev = "critical" if lt > 32 else "warning"
                            m["ex"] += 1
                    elif f["type"] == "drop" and (idx + 1) % f["every"] == 0:
                        dropped, kind, sev = True, "dropped_transfer", "error"
                        m["dr"] += 1
                m["tr"] += 1
                if not dropped:
                    m["sum"] += lt
                    m["rc"] += 1
                    rc += 1
                    ls += lt
                    m["mx"] = max(m["mx"], lt)
                if kind:
                    a += 1
                    events.append({"cyc": idx * CYCLES_PER_TRANSFER, "link": link["id"], "type": kind,
                                   "sev": sev, "exp": hx(exp), "obs": "—" if obs is None else hx(obs),
                                   "lat": None if dropped else lt})
            m["lat"].append(to2(ls / max(1, rc)))
            m["an"].append(a)
            m["rcv"].append(rc)
        m["avg"] = to2(m["sum"] / m["rc"]) if m["rc"] else None
        m["an_"] = m["er"] + m["dr"] + m["ex"]
        m["st"] = "healthy" if m["an_"] == 0 else ("faulted" if m["an_"] / m["tr"] > 0.1 else "suspected")
        per_link[link["id"]] = m
    events.sort(key=lambda e: (e["cyc"], e["link"]))
    return per_link, events


def totals(per_link):
    m = list(per_link.values())
    rc = sum(x["rc"] for x in m)
    return {"tr": sum(x["tr"] for x in m), "er": sum(x["er"] for x in m),
            "dr": sum(x["dr"] for x in m), "ex": sum(x["ex"] for x in m),
            "avg": to2(sum(x["sum"] for x in m) / rc) if rc else None,
            "mx": max(x["mx"] for x in m), "bad": sum(x["st"] != "healthy" for x in m)}


def tests(topo, chiplets):
    """Candidate tests, in the same order as the browser TESTS()."""
    ids = [link["id"] for link in topo]
    out = [{"id": "T-SWEEP", "name": "Full-fabric sweep", "L": ids}]
    out += [{"id": f"T-EP-{c}", "name": f"Endpoint fan-out: Chiplet {c}",
             "L": [link["id"] for link in topo if c in (link["s"], link["d"])]}
            for c in NAMES[:chiplets]]
    out += [{"id": f"T-{i}", "name": f"Link probe: {i}", "L": [i]} for i in ids]
    return out


def suspicion(result, plan):
    """Per-link weights summing to 1, updated by prior test outcomes. None if nothing was observed."""
    topo = result["topology"]
    counts = [result["links"][link["id"]]["an_"] for link in topo]
    if not sum(counts):
        return None
    by_id = {t["id"]: t for t in tests(topo, result["config"]["chiplets"])}
    w = [c + 0.5 for c in counts]
    for step in plan:
        if step["id"] not in by_id:
            raise ValueError(f"unknown test '{step['id']}'")
        t = by_id[step["id"]]
        w = [v * ((4 if step["fail"] else 0.1) if link["id"] in t["L"] else 1)
             for v, link in zip(w, topo)]
    total = sum(w)
    return [v / total for v in w]


def rank_tests(result, plan):
    """Heuristic information-split ranking. Scores are not probabilities."""
    s = suspicion(result, plan)
    if s is None:
        return []
    topo = result["topology"]
    pos = {link["id"]: i for i, link in enumerate(topo)}
    done = {step["id"] for step in plan}
    ranked = []
    for t in tests(topo, result["config"]["chiplets"]):
        if t["id"] in done:
            continue
        mass = sum(s[pos[x]] for x in t["L"])
        ranked.append({"id": t["id"], "name": t["name"], "links": t["L"], "mass": mass,
                       "score": mass * (1 - mass) / (1 + 0.1 * len(t["L"]))})
    ranked.sort(key=lambda t: (-t["score"], t["id"]))
    return ranked


def run_experiment(scenario_id, store=None, *, seed=None, chiplets=4, preset="full", tpi=10, lat=4):
    """Run a built-in scenario. If a store is given, persist the completed result."""
    if scenario_id not in SCENARIO_BY_ID:
        raise ValueError(f"unknown scenario '{scenario_id}'")
    validate(chiplets, preset, tpi, lat)
    sc = SCENARIO_BY_ID[scenario_id]
    config = {"scenario_id": sc["id"], "seed": sc["seed"] if seed is None else seed,
              "chiplets": chiplets, "preset": preset, "tpi": tpi, "lat": lat, "faults": sc["faults"]}
    per_link, events = simulate({**sc, "seed": config["seed"]}, chiplets, preset, tpi, lat)
    run_id = "EXP-" + uuid.uuid4().hex[:8].upper()
    for n, e in enumerate(events, 1):
        e["id"] = f"{run_id}-EV{n:03d}"
    result = {"id": run_id,
              "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "scenario": {"id": sc["id"], "name": sc["name"], "desc": sc["desc"]},
              "config": config, "topology": topology(chiplets, preset, lat),
              "links": per_link, "events": events, "totals": totals(per_link)}
    if store is not None:
        store.save(result)
    return result
