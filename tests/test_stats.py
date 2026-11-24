"""The stats engine (report/stats.js) and the site's build.

stats.js started as a port of an earlier Python engine; that engine's output
for the same exports is frozen in tests/fixtures/*.report.json.gz, and stats.js
must still produce exactly it. The cases: the example season, a partial
export, variations built to hit the edge cases (both-fault turnovers,
stall-outs, missing coordinates, shared blame, ties, unusual dates) and no
games. Needs Node 18+.

If a stats change is intended, update the fixtures (see refresh_fixtures in
this file) and say so in the pull request.
"""

import gzip
import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from helpers import ROOT, example_bytes, without_earliest_game

NODE = shutil.which("node")
STATS_JS = os.path.join(ROOT, "report", "stats.js")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

RUNNER = r"""
const fs = require('fs');
const S = require(process.argv[1]);
(async () => {
  const out = {};
  for (const f of process.argv.slice(2)) {
    try { out[f] = await S.reportFromStatto(fs.readFileSync(f)); }
    catch (e) { out[f] = { error: e.message }; }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def rewrite(data, mutate):
    """A copy of a .statto export with its data.json changed by `mutate`."""
    src = zipfile.ZipFile(io.BytesIO(data))
    doc = json.loads(src.read("data.json"))
    mutate(doc["teams"][0]["data"])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in src.namelist():
            z.writestr(name, json.dumps(doc) if name == "data.json" else src.read(name))
    return out.getvalue()


def edge_cases(team):
    rel = team["relations"]
    passes = rel["passes"]
    # shared-blame turnovers, and plain receiver drops
    for pa in passes[::17]:
        pa["isThrowerError"] = True
        pa["isReceiverError"] = True
    for pa in passes[5::23]:
        pa["isReceiverError"] = True
    # missing coordinates fall back to 0.0
    for pa in passes[3::31]:
        pa.pop("startX", None)
        pa.pop("endY", None)
    # a few stall-outs against us
    rel["stallOutsAgainst"] = [
        {"uuid": "SO-%d" % i, "possessionUUID": po["uuid"], "playerUUID": rel["players"][i % len(rel["players"])]["uuid"]}
        for i, po in enumerate(rel["possessions"][::40])
    ]
    # junk (non-string) uuids and a stall-out block
    if rel.get("defensiveBlocks"):
        rel["defensiveBlocks"][0]["isStallOut"] = True
        rel["defensiveBlocks"][-1]["playerUUID"] = 0
    passes[1]["throwerUUID"] = None
    # a tied game and a date with fractional seconds + offset
    rel["games"][0]["date"] = rel["games"][0]["date"].replace("Z", ".123+00:00")
    team["team"]["name"] = "Edge <Case> FC"


def no_games(team):
    team["relations"]["games"] = []


def expected(name):
    with gzip.open(os.path.join(FIXTURES, name + ".report.json.gz"), "rt", encoding="utf-8") as f:
        return json.load(f)


def refresh_fixtures(results):
    """Overwrite the fixtures with stats.js's current output (only when a stats
    change is intended): STATTO_REFRESH_FIXTURES=1 python3 -m unittest tests/test_stats.py"""
    for name, doc in results.items():
        with gzip.GzipFile(os.path.join(FIXTURES, name + ".report.json.gz"), "wb", mtime=0) as f:
            f.write(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())


@unittest.skipUnless(NODE, "Node isn't installed")
class StatsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = example_bytes()
        cls.cases = {
            "example": base,
            "partial": without_earliest_game(base),
            "edge_cases": rewrite(base, edge_cases),
            "no_games": rewrite(base, no_games),
        }
        cls.paths = {}
        for name, data in cls.cases.items():
            path = os.path.join(cls.tmp.name, name + ".statto")
            with open(path, "wb") as f:
                f.write(data)
            cls.paths[name] = path
        result = subprocess.run([NODE, "-e", RUNNER, STATS_JS] + list(cls.paths.values()),
                                capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        cls.js = json.loads(result.stdout)
        if os.environ.get("STATTO_REFRESH_FIXTURES") == "1":
            refresh_fixtures({name: cls.js[path] for name, path in cls.paths.items()})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def assertSame(self, py, js, path="report"):
        """py: the expected value (from the fixture); js: stats.js's."""
        if isinstance(py, dict):
            self.assertIsInstance(js, dict, path)
            self.assertEqual(sorted(py), sorted(js), "keys differ at " + path)
            for k in py:
                self.assertSame(py[k], js[k], path + "." + k)
        elif isinstance(py, list):
            self.assertIsInstance(js, list, path)
            self.assertEqual(len(py), len(js), "length differs at " + path)
            for i, (a, b) in enumerate(zip(py, js)):
                self.assertSame(a, b, "%s[%d]" % (path, i))
        elif isinstance(py, bool) or py is None or isinstance(py, str):
            self.assertEqual(py, js, path)
        else:  # numbers: the fixture may have 1.0 where JSON from JS has 1
            self.assertIsInstance(js, (int, float), path)
            if float(py).is_integer() and float(js).is_integer():
                self.assertEqual(float(py), float(js), path)
            else:
                # Raw distance sums can differ in the last binary digit (the
                # fixtures came from Python's math.hypot, which rounds its final
                # bit differently from Math.hypot). Far below anything shown.
                self.assertTrue(math.isclose(py, js, rel_tol=1e-12, abs_tol=1e-12),
                                "%s: %r != %r" % (path, py, js))

    def check(self, name):
        self.assertNotIn("error", self.js[self.paths[name]], self.js[self.paths[name]])
        self.assertSame(expected(name), self.js[self.paths[name]])

    def test_example_season(self):
        self.check("example")

    def test_partial_export(self):
        self.check("partial")

    def test_edge_cases(self):
        self.check("edge_cases")

    def test_no_games(self):
        self.check("no_games")

    def test_ignored_events_are_left_out(self):
        """Passes and blocks marked "Ignore this event" are left out of every
        stat: they vanish from the report and the counts drop."""
        runner = r"""
const fs = require('fs');
const S = require(process.argv[1]);
(async () => {
  const buf = fs.readFileSync(process.argv[2]);
  const full = (await S.reportFromStatto(buf)).report;
  const pts = full.games.flatMap(g => g.points);
  const pass = pts.flatMap(p => p.passes).find(p => !p.turnover && !p.assist && p.thrower && p.receiver);
  const block = pts.flatMap(p => p.blocks).find(b => b.player);
  const ann = { passes: { [pass.uuid]: { ignored: true, hand: 'Flick' } }, blocks: { [block.uuid]: { ignored: true } }, points: {} };
  const ignored = S.ignoredEventIds(ann);
  const less = (await S.reportFromStatto(buf, { ignored })).report;
  const row = (r, name) => r.seasonLeaderboard.find(x => x.player === name);
  const ids = r => new Set(r.games.flatMap(g => g.points.flatMap(p => p.passes.map(x => x.uuid).concat(p.blocks.map(x => x.uuid)))));
  process.stdout.write(JSON.stringify({
    ignored: [...ignored].sort(), want: [pass.uuid, block.uuid].sort(),
    stillThere: [pass.uuid, block.uuid].filter(u => ids(less).has(u)),
    throws: [row(full, pass.thrower).throws, row(less, pass.thrower).throws],
    completions: [row(full, pass.thrower).throwCompletions, row(less, pass.thrower).throwCompletions],
    catches: [row(full, pass.receiver).catches, row(less, pass.receiver).catches],
    blocks: [row(full, block.player).blocks, row(less, block.player).blocks],
  }));
})();
"""
        r = subprocess.run([NODE, "-e", runner, STATS_JS, self.paths["example"]], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["ignored"], out["want"])
        self.assertEqual(out["stillThere"], [])
        for key in ("throws", "completions", "catches", "blocks"):
            self.assertEqual(out[key][1], out[key][0] - 1, key)
        # Without ignores, the report is exactly the frozen one (test_example_season).

    def test_edits_credit_the_right_player(self):
        """An "Edit" correction (thrower, or who got the D) moves the credit."""
        runner = r"""
const fs = require('fs');
const S = require(process.argv[1]);
(async () => {
  const buf = fs.readFileSync(process.argv[2]);
  const full = (await S.reportFromStatto(buf)).report;
  const pts = full.games.flatMap(g => g.points);
  const pass = pts.flatMap(p => p.passes).find(p => !p.turnover && !p.assist && p.thrower);
  const block = pts.flatMap(p => p.blocks).find(b => b.player);
  const names = full.seasonLeaderboard.map(r => r.player);
  const newThrower = names.find(n => n !== pass.thrower && n !== pass.receiver);
  const newBlocker = names.find(n => n !== block.player);
  const ann = { passes: { [pass.uuid]: { edit: { thrower: newThrower } } }, blocks: { [block.uuid]: { edit: { player: newBlocker } } } };
  const fixed = (await S.reportFromStatto(buf, { edits: S.eventEdits(ann) })).report;
  const row = (r, n) => r.seasonLeaderboard.find(x => x.player === n);
  const after = fixed.games.flatMap(g => g.points).flatMap(p => p.passes).find(p => p.uuid === pass.uuid);
  process.stdout.write(JSON.stringify({
    none: S.eventEdits({ passes: {}, blocks: {} }),
    thrower: after.thrower, want: newThrower,
    oldThrows: [row(full, pass.thrower).throws, row(fixed, pass.thrower).throws],
    newThrows: [row(full, newThrower).throws, row(fixed, newThrower).throws],
    oldBlocks: [row(full, block.player).blocks, row(fixed, block.player).blocks],
    newBlocks: [row(full, newBlocker).blocks, row(fixed, newBlocker).blocks],
  }));
})();
"""
        r = subprocess.run([NODE, "-e", runner, STATS_JS, self.paths["example"]], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIsNone(out["none"])
        self.assertEqual(out["thrower"], out["want"])
        self.assertEqual(out["oldThrows"][1], out["oldThrows"][0] - 1)
        self.assertEqual(out["newThrows"][1], out["newThrows"][0] + 1)
        self.assertEqual(out["oldBlocks"][1], out["oldBlocks"][0] - 1)
        self.assertEqual(out["newBlocks"][1], out["newBlocks"][0] + 1)

    def test_not_a_statto_file(self):
        bad = os.path.join(self.tmp.name, "bad.statto")
        with open(bad, "wb") as f:
            f.write(b"this is not a zip")
        r = subprocess.run([NODE, "-e", RUNNER, STATS_JS, bad], capture_output=True, text=True, timeout=60)
        self.assertIn("isn’t a Statto export", json.loads(r.stdout)[bad]["error"])


@unittest.skipUnless(NODE, "Node isn't installed")
class SiteBuildTests(unittest.TestCase):
    """public/ is generated (build.mjs) but committed, so a deploy needs no
    build step. It must match what the build makes now."""

    def test_site_public_is_up_to_date(self):
        with tempfile.TemporaryDirectory() as out:
            subprocess.run([NODE, os.path.join(ROOT, "build.mjs"), out], check=True, capture_output=True)
            public = os.path.join(ROOT, "public")
            built = sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs)
            committed = sorted(os.path.relpath(os.path.join(d, f), public) for d, _, fs in os.walk(public) for f in fs)
            self.assertEqual(built, committed, "Run `npm run build` and commit public/.")
            for rel in built:
                with open(os.path.join(out, rel), "rb") as a, open(os.path.join(public, rel), "rb") as b:
                    self.assertTrue(a.read() == b.read(), "public/%s is out of date: run `npm run build`." % rel)


if __name__ == "__main__":
    unittest.main()
