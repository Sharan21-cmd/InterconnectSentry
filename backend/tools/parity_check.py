"""Check that the backend engine reproduces the browser Demo Lab model exactly.

Extracts the simulation code from the frontend HTML, runs it under node for every
built-in scenario, and compares per-link metrics and event lists with engine.simulate().
Usage: python3 backend/tools/parity_check.py interconnectsentry-preview.html   (needs node)
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from interconnect_backend.engine import SCENARIOS, simulate  # noqa: E402

JS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const cut = (a, b) => { const i = html.indexOf(a), j = html.indexOf(b, i + 1);
  if (i < 0 || j < 0) throw new Error('missing ' + a); return html.slice(i, j); };
const body = cut('const rng=', 'function netChanged(') + '\n' + cut('function sim(', 'function runSim(');
const s = html.indexOf('{id:"healthy"');
const scenarios = eval('[' + html.slice(s, html.indexOf('];', s)) + ']');
const { sim, LK } = new Function(body + '\nreturn { sim, LK };')();
const out = scenarios.map(sc => {
  const mapped = { ...sc, faults: sc.faults.map(f => ({ ...f, link: LK[(parseInt(f.link.slice(1)) - 1) % LK.length].id })) };
  const r = sim(mapped);
  return { id: sc.id, L: r.L, ev: r.ev };
});
console.log(JSON.stringify(out));
"""


def main(html_path):
    with tempfile.TemporaryDirectory() as d:
        js_file = os.path.join(d, "dump.js")
        with open(js_file, "w", encoding="utf-8") as fh:
            fh.write(JS)
        proc = subprocess.run(["node", js_file, html_path], capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr)
        return 2
    ref = {r["id"]: r for r in json.loads(proc.stdout)}
    bad, event_count = 0, 0
    for sc in SCENARIOS:
        per_link, events = simulate(sc)
        want = ref[sc["id"]]
        event_count += len(events)
        same_links, same_events = per_link == want["L"], events == want["ev"]
        if not (same_links and same_events):
            bad += 1
            print(f"MISMATCH in {sc['id']}: links equal={same_links}, events equal={same_events}")
    if not bad:
        print(f"PARITY OK: {len(SCENARIOS)} scenarios, {event_count} events identical to the browser model")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
