"""The Python host (pyhost/tdhost): TouchDesigner API emulation for deployed projects.

Covers the network loader (toeexpand .n/.parm/.cparm/.text/.table), parameter
semantics (constant vs expression mode, custom parameter styles, Par-as-value),
storage, object transforms (checked against numbers taken from TouchDesigner),
the callback/frame protocol and the Script CHOP/SOP/TOP APIs.
"""

import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyhost")
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tdhost import tdu  # noqa: E402
from tdhost import Host, load_network  # noqa: E402
from tdhost.tree import parse_cparm, parse_parm  # noqa: E402
from toetree import ToeTree  # noqa: E402

FLOAT = "772804865"
TOGGLE = "772804867"
MENU = "772804879"
RGB = "772809473"
XYZ = "772813569"


class ParmParseTest(unittest.TestCase):
    def test_value_expression_and_mode(self):
        p = parse_parm(
            "?\n"
            "file 16 /Users/me/x.fbx app.samplesFolder+'/FBX/TDLogo.fbx'\n"
            'geometry 0 "char/geo handleR handleL"\n'
            "tz 17 400 op('/project1/rig').par.Camdist\n"
            "render 17 on \"op('/project1/rig').fetch('show', {}).get('h', 1.0) > 0.001\"\n"
            "?\n"
        )
        self.assertEqual(
            p["file"],
            {"mode": 16, "val": "/Users/me/x.fbx", "expr": "app.samplesFolder+'/FBX/TDLogo.fbx'"},
        )
        self.assertEqual(p["geometry"]["val"], "char/geo handleR handleL")
        self.assertIsNone(p["geometry"]["expr"])
        self.assertEqual(p["tz"]["expr"], "op('/project1/rig').par.Camdist")
        self.assertTrue(p["render"]["expr"].startswith("op('/project1/rig').fetch"))

    def test_custom_parameter_styles(self):
        pages, defs = parse_cparm(
            "?\npages 2 Main Show\n"
            f'{FLOAT} Size "Size (px)" 1 1 0 0 1 1 10 2 2.5 "" "" Main 0\n'
            f'{TOGGLE} Solve Solve 1 1 0 0 1 1 1 2 1 "" "" Main 1\n'
            f'{MENU} Mode "Show Mode" 1 1 0 0 1 1 1 2 0 auto "" Show 4097 3 '
            'auto "Auto (Presence)" in "Force In" out "Force Out" 2\n'
            f'{RGB} Tint Tint 1 1 0 0 1 1 1 1 0 0 1 1 1 1 0 0 1 1 1 2 0.9 "" "" 2 0.8 "" "" 2 0.7 "" "" Main 3\n'
            f'{XYZ} Dir "Key Dir" 1 1 0 0 1 1 1 1 0 0 1 1 1 1 0 0 1 1 1 2 -0.05 "" "" 2 0.15 "" "" 2 1 "" "" Main 4\n'
            "?\n"
        )
        self.assertEqual(pages, ["Main", "Show"])
        by = {d["name"]: d for d in defs}
        self.assertEqual(by["Size"]["style"], "Float")
        self.assertEqual(by["Size"]["default"], [2.5])
        self.assertEqual((by["Size"]["min"], by["Size"]["max"]), (0.0, 10.0))
        self.assertEqual(by["Solve"]["style"], "Toggle")
        self.assertEqual(by["Mode"]["menuNames"], ["auto", "in", "out"])
        self.assertEqual(by["Mode"]["default"], "auto")
        self.assertEqual(by["Tint"]["names"], ["Tintr", "Tintg", "Tintb"])
        self.assertEqual(by["Tint"]["default"], [0.9, 0.8, 0.7])
        self.assertEqual(by["Dir"]["names"], ["Dirx", "Diry", "Dirz"])


class TduTest(unittest.TestCase):
    def test_matrix_is_column_major_in_rows_out(self):
        m = tdu.Matrix([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 5, 6, 7, 1])
        self.assertEqual(m.rows[0], [1.0, 0.0, 0.0, 5.0])
        self.assertEqual(m.rows[2][3], 7.0)

    def test_euler_round_trip_all_orders(self):
        rng = np.random.default_rng(0)
        for order in ("xyz", "xzy", "yxz", "yzx", "zxy", "zyx"):
            for _ in range(50):
                a = rng.uniform(-170, 170, 3)
                a[1] = rng.uniform(-85, 85)
                r = tdu.euler3(*a, order=order)
                b = tdu.euler_from3(r, order)
                np.testing.assert_allclose(tdu.euler3(*b, order=order), r, atol=1e-9)


def _net(tmp):
    t = ToeTree(tmp)
    t.op(
        "project1/rig",
        "COMP:base",
        params={"Solve": (67108928, "on"), "Speed": (67108945, "0", "absTime.seconds * 2")},
        custom=[
            f'{TOGGLE} Solve Solve 1 1 0 0 1 1 1 2 1 "" "" Main 0',
            f'{FLOAT} Speed Speed 1 1 0 0 1 1 10 2 1 "" "" Main 1',
            f'{FLOAT} Gain Gain 1 1 0 0 1 1 10 2 3 "" "" Main 2',
            f'{MENU} Mode Mode 1 1 0 0 1 1 1 2 0 auto "" Main 4097 2 auto Auto in In 3',
        ],
    )
    t.text(
        "project1/rig/core",
        "import math\n"
        "def step():\n"
        "    rig = op('/project1/rig')\n"
        "    n = rig.fetch('n', 0) + 1\n"
        "    rig.store('n', n)\n"
        "    g = op('/project1/geo1')\n"
        "    g.par.tx = n * 1.5\n"
        "    return n\n",
    )
    t.text(
        "project1/rig/exec",
        "def onStart():\n    parent().store('started', True)\n"
        "def onFrameStart(frame):\n    op('core').module.step()\n"
        "def onFrameEnd(frame):\n    op('/project1/top1').numpyArray(delayed=True)\n",
        kind="DAT:execute",
        params={"start": "on", "framestart": "on", "frameend": "on"},
    )
    t.op("project1/geo1", "COMP:geo", params={"ty": "2", "rz": "90", "scale": "2"})
    t.op("project1/geo1/child", "COMP:null", params={"tx": "1"})
    t.op("project1/nullA", "COMP:null", params={"tx": "10"})
    t.op("project1/nullB", "COMP:null", inputs=["nullA"], params={"ty": "5"})
    t.op(
        "project1/top1",
        "TOP:glsl",
        params={
            "resolutionw": "64",
            "resolutionh": "32",
            "vec0valuex": (17, "0", "op('/project1/rig').par.Gain * 2 + me.par.resolutionw"),
            "vec0valuey": (17, "0", "op('/project1/rig').fetch('n', -1)"),
            "vec0valuez": (17, "0", "op('/project1/rig').par.Mode in ('in', 'x')"),
        },
    )
    t.text(
        "project1/chop_cb",
        "def onCook(scriptOp):\n"
        "    scriptOp.clear()\n"
        "    scriptOp.numSamples = 3\n"
        "    c = scriptOp.appendChan('tx')\n"
        "    c.vals = [1, 2, 3]\n",
    )
    t.op("project1/chop1", "CHOP:script", params={"callbacks": "chop_cb"})
    t.text(
        "project1/sop_cb",
        "def onCook(scriptOp):\n"
        "    scriptOp.clear()\n"
        "    k = float(op('/project1/rig').par.Gain)\n"
        "    pts = [scriptOp.appendPoint() for _ in range(4)]\n"
        "    for i, p in enumerate(pts):\n"
        "        p.P = (i * k, 0.0, 0.0)\n"
        "    poly = scriptOp.appendPoly(4, closed=True, addPoints=False)\n"
        "    for i in range(4):\n"
        "        poly[i].point = pts[i]\n",
    )
    t.op("project1/sop1", "SOP:script", params={"callbacks": "sop_cb"})
    t.table("project1/rest", [["bone", "tx"], ["Head", "0.5"]])
    return t.dir


class HostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = Host(load_network(_net(self.tmp)), self.tmp)

    def test_parameters_behave_like_values(self):
        rig = self.host.ops["/project1/rig"]
        self.assertIs(rig.par.Solve.eval(), True)
        self.assertEqual(rig.par.Gain + 1, 4.0)
        self.assertEqual(2 * rig.par.Gain, 6.0)
        self.assertTrue(rig.par.Mode == "auto")
        self.assertIn(rig.par.Mode, ("auto", "in"))
        rig.par.Gain = 5
        self.assertEqual(float(rig.par.Gain), 5.0)
        rig.par.Mode = "in"
        self.assertEqual(self.host.eval_par("/project1/top1", "vec0valuez"), True)

    def test_expression_mode_and_storage(self):
        rig = self.host.ops["/project1/rig"]
        self.host.absTime.seconds = 3.0
        self.assertEqual(rig.par.Speed.eval(), 6.0)
        self.assertEqual(self.host.eval_par("/project1/top1", "vec0valuex"), 3 * 2 + 64)
        self.assertEqual(self.host.eval_par("/project1/top1", "vec0valuey"), -1)
        rig.store("n", 7)
        self.assertEqual(self.host.eval_par("/project1/top1", "vec0valuey"), 7)
        # search=True walks up to parents; storeDefault writes the default
        child = self.host.ops["/project1/rig/core"]
        self.assertEqual(child.fetch("n"), 7)
        self.assertEqual(child.fetch("zz", 3, storeDefault=True), 3)
        self.assertEqual(child.storage["zz"], 3)

    def test_table_dat(self):
        t = self.host.ops["/project1/rest"]
        self.assertEqual(t.numRows, 2)
        self.assertEqual(t[1, 0].val, "Head")
        self.assertEqual(float(t["Head", "tx"]), 0.5)

    def test_object_transforms(self):
        h = self.host
        g = h.ops["/project1/geo1"]
        # T(0,2,0) @ Rz(90) @ S(2): the child's local (1,0,0) lands at (0, 4, 0)
        w = h.world_matrix(h.ops["/project1/geo1/child"])
        np.testing.assert_allclose(w @ [0, 0, 0, 1], [0, 4, 0, 1], atol=1e-12)
        # parenting through the input wire
        wb = h.world_matrix(h.ops["/project1/nullB"])
        np.testing.assert_allclose(wb[:3, 3], [10, 5, 0])
        # setTransform() writes parameters that reproduce the matrix
        m = tdu.Matrix(g.worldTransform)
        g.setTransform(m)
        np.testing.assert_allclose(h.local_matrix(g), m.m, atol=1e-9)
        self.assertAlmostEqual(float(g.par.sx), 2.0)
        self.assertAlmostEqual(float(g.par.rz), 90.0)

    def test_snapshot_matrices_match_uncached_and_track_changes(self):
        h = self.host
        paths = ["/project1/geo1", "/project1/geo1/child", "/project1/nullB"]
        h.bind({"mats": paths})
        h.start()
        got = h.frame(0.0)["mats"].reshape(-1, 4, 4).transpose(0, 2, 1)
        want = [h.world_matrix(h.ops[p]) for p in paths]  # after onFrameStart moved geo1
        np.testing.assert_allclose(got, want, atol=1e-12)
        self.assertIsNone(h._xf_cache)  # the cache lives only inside a snapshot
        # a parent moved between frames: the child follows (nothing stale)
        h.ops["/project1/geo1"].par.ty = 7
        got = h.frame(1 / 60)["mats"].reshape(-1, 4, 4).transpose(0, 2, 1)
        np.testing.assert_allclose(got[1], h.world_matrix(h.ops["/project1/geo1/child"]))
        self.assertAlmostEqual(got[0][1, 3], 7.0)

    def test_native_xf_sends_changed_locals_only(self):
        h = self.host
        h.native_xf = True
        paths = ["/project1/geo1/child", "/project1/nullB", "/project1/geo1"]
        h.bind({"mats": paths})
        h.start()
        locals_ = {}

        def apply(xf):  # what the renderer does (runtime_rs compose_xf)
            if "parent" in xf:
                apply.parent, apply.nodes = xf["parent"], xf["mat_nodes"]
            m = np.asarray(xf["m"]).reshape(-1, 16)
            for k, i in enumerate(xf["idx"]):
                locals_[int(i)] = m[k].reshape(4, 4).T  # column-major -> rows
            world = []
            for i, p in enumerate(apply.parent):
                world.append(locals_[i] if p < 0 else world[p] @ locals_[i])
            return [world[n] for n in apply.nodes]

        out = h.frame(0.0)
        self.assertEqual(len(out["mats"]), 0)
        got = apply(out["xf"])
        for p, w in zip(paths, got):
            np.testing.assert_allclose(w, h.world_matrix(h.ops[p]), atol=1e-12)
        # next frame: onFrameStart moved geo1 (tx) only; its child's local is unchanged
        out = h.frame(1 / 60)
        self.assertNotIn("parent", out["xf"])
        sent = {apply.parent and h._xf["nodes"][int(i)][0].path for i in out["xf"]["idx"]}
        self.assertEqual(sent, {"/project1/geo1"})
        got = apply(out["xf"])
        for p, w in zip(paths, got):
            np.testing.assert_allclose(w, h.world_matrix(h.ops[p]), atol=1e-12)

    def test_bound_constants_skip_eval_until_written(self):
        h = self.host
        h.bind({"pars": [["/project1/geo1", "ty"], ["/project1/top1", "vec0valuey"]]})
        out = h.frame(0.0)
        self.assertEqual(out["floats"][0], 2.0)
        h.ops["/project1/geo1"].par.ty = 5
        self.assertEqual(h.frame(1 / 60)["floats"][0], 5.0)
        self.assertEqual(h.frame(2 / 60)["floats"][0], 5.0)

    def test_expressions_are_kept_until_what_they_read_changes(self):
        h = self.host
        top = h.ops["/project1/top1"]
        rig = h.ops["/project1/rig"]
        px = top.par.vec0valuex  # op('/project1/rig').par.Gain * 2 + me.par.resolutionw
        rig.par.Gain = 3
        self.assertEqual(px.eval(), 70.0)
        memo = px._memo
        self.assertTrue(memo)
        self.assertEqual(px.eval(), 70.0)
        self.assertIs(px._memo, memo)  # kept: nothing it read changed
        rig.par.Gain = 4  # a parameter it read
        self.assertEqual(px.eval(), 72.0)
        top.par.resolutionw = 10  # its own op's parameter
        self.assertEqual(px.eval(), 18.0)
        # storage: per key (another key changing doesn't re-evaluate)
        py = top.par.vec0valuey  # op('/project1/rig').fetch('n', -1)
        self.assertEqual(py.eval(), -1)
        rig.store("n", 5)
        self.assertEqual(py.eval(), 5)
        m = py._memo
        rig.store("other", 1)
        self.assertEqual(py.eval(), 5)
        self.assertIs(py._memo, m)
        rig.storage["n"] = 6  # direct writes count too
        self.assertEqual(py.eval(), 6)
        rig.unstore("n")
        self.assertEqual(py.eval(), -1)
        # time-dependent expressions are never kept
        sp = rig.par.Speed  # absTime.seconds * 2
        h.absTime.seconds = 1.0
        self.assertEqual(sp.eval(), 2.0)
        self.assertIs(sp._memo, False)
        h.absTime.seconds = 2.0
        self.assertEqual(sp.eval(), 4.0)
        # ... nor what reads them
        top.par.vec0valuez.expr = "op('/project1/rig').par.Speed + 1"
        self.assertEqual(top.par.vec0valuez.eval(), 5.0)
        h.absTime.seconds = 3.0
        self.assertEqual(top.par.vec0valuez.eval(), 7.0)

    def test_unchanged_script_top_array_is_not_resent(self):
        h = self.host
        o = h.ops["/project1/top1"]
        img = np.zeros((4, 8, 4), np.uint8)
        h._script_top_data(o, img)
        self.assertIn(o.path, h._script_top_dirty)
        h._script_top_dirty.clear()
        h._script_top_data(o, img)  # same array, same pixels: nothing to send
        self.assertNotIn(o.path, h._script_top_dirty)
        img[0, 0, 0] = 9  # modified in place: sent
        h._script_top_data(o, img)
        self.assertIn(o.path, h._script_top_dirty)
        h._script_top_dirty.clear()
        h._script_top_data(o, img.copy())  # a new array: sent
        self.assertIn(o.path, h._script_top_dirty)
        ro = img.copy()
        ro.flags.writeable = False  # read-only: kept by reference (no copy)
        h._script_top_data(o, ro)
        self.assertIs(h._script_top_src[o.path][1], ro)
        h._script_top_dirty.clear()
        h._script_top_data(o, ro)
        self.assertNotIn(o.path, h._script_top_dirty)

    def test_local_matrix_kept_until_a_parameter_changes(self):
        h = self.host
        g = h.ops["/project1/geo1"]
        m1 = h.local_matrix(g)
        self.assertIs(h.local_matrix(g), m1)  # constant transform: reused
        g.par.tx = 5
        m2 = h.local_matrix(g)
        self.assertIsNot(m2, m1)
        self.assertAlmostEqual(m2[0, 3], 5.0)
        g.setTransform(tdu.Matrix())  # direct writes invalidate too
        np.testing.assert_allclose(h.local_matrix(g), np.eye(4), atol=1e-12)
        # an expression-driven transform is never kept
        g.par.ty.expr = "absTime.seconds"
        h.absTime.seconds = 1.0
        self.assertAlmostEqual(h.local_matrix(g)[1, 3], 1.0)
        h.absTime.seconds = 2.0
        self.assertAlmostEqual(h.local_matrix(g)[1, 3], 2.0)

    def test_matches_touchdesigner_bone_transforms(self):
        # Bone local matrices exactly as TouchDesigner 2025.33070 reported them for
        # these parameter values (srt order, rotate xyz; captured from the FBX
        # skeleton in the trickster project).
        cases = [
            (
                dict(
                    ty=0.10505381971597672,
                    rx=19.055404663085938,
                    ry=0.7247234582901001,
                    rz=-0.4363647699356079,
                    sy=1.0000001192092896,
                    sz=1.0000001192092896,
                ),
                [
                    [0.999890985426596, 0.011327985344867468, 0.009468566972763203, 0.0],
                    [
                        -0.007615319344335902,
                        0.9451445301076823,
                        -0.3265639390801132,
                        0.10505381971597672,
                    ],
                    [-0.012648473493754864, 0.3264562495727067, 0.9451278106821202, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ],
            ),
            (
                dict(
                    ty=0.16152144968509674,
                    rx=-103.54019927978516,
                    ry=-20.77931785583496,
                    rz=10.10748291015625,
                    sx=0.9999999403953552,
                    sy=0.9999999403953552,
                    sz=0.9999998807907104,
                ),
                [
                    [0.9204435155828442, 0.38064431632115825, -0.08884535197414986, 0.0],
                    [
                        0.16407998630910278,
                        -0.1699639835483247,
                        0.9716942457679689,
                        0.16152144968509674,
                    ],
                    [0.354769447161587, -0.9089675337294183, -0.21889836402310384, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ],
            ),
        ]
        h = self.host
        n = h.ops["/project1/nullA"]
        for params, td_rows in cases:
            for k in ("tx", "ty", "tz", "rx", "ry", "rz"):
                n.par[k] = params.get(k, 0.0)
            for k in ("sx", "sy", "sz"):
                n.par[k] = params.get(k, 1.0)
            np.testing.assert_allclose(h.local_matrix(n), td_rows, atol=1e-7)

    def test_frame_protocol(self):
        h = self.host
        h.bind(
            {
                "pars": [["/project1/top1", "vec0valuey"], ["/project1/geo1", "tx"]],
                "mats": ["/project1/geo1"],
                "flags": [["/project1/geo1", "render"]],
                "chops": [["/project1/chop1", ["tx"]]],
                "sops": ["/project1/sop1"],
            }
        )
        h.start()
        self.assertTrue(h.ops["/project1/rig"].storage.get("started"))
        out = h.frame(0.0)
        # onFrameStart ran core.step() before the bindings were evaluated
        self.assertEqual(list(out["floats"]), [1.0, 1.5, 1.0])
        self.assertEqual(out["mats"].reshape(-1, 16)[0][12], 1.5)  # column-major translate x
        np.testing.assert_array_equal(out["chops"]["/project1/chop1"]["tx"], [1, 2, 3])
        mesh = out["sops"]["/project1/sop1"]
        self.assertEqual(len(mesh["pos"]), 4)
        self.assertEqual(list(mesh["idx"]), [0, 1, 2, 0, 2, 3])
        self.assertEqual(h.frame_end(), ["/project1/top1"])  # numpyArray -> readback request
        # the Script SOP recooks only when a parameter it read changes
        self.assertNotIn("/project1/sop1", h.frame(1 / 60)["sops"])
        h.ops["/project1/rig"].par.Gain = 2
        self.assertEqual(h.frame(2 / 60)["sops"]["/project1/sop1"]["pos"][3][0], 6.0)
        # readbacks come back as numpyArray results
        px = np.full((32, 64, 4), 0.5, np.float32)
        h.frame(3 / 60, {"/project1/top1": px})
        self.assertIs(h.ops["/project1/top1"].numpyArray(delayed=True), px)

    def test_run_is_deferred(self):
        h = self.host
        h.start()
        h.run("op('/project1/rig').store('ran', absTime.frame)", delayFrames=2)
        h.frame(0.0)
        self.assertNotIn("ran", h.ops["/project1/rig"].storage)
        h.frame(1 / 60)
        h.frame(2 / 60)
        self.assertIn("ran", h.ops["/project1/rig"].storage)


if __name__ == "__main__":
    unittest.main()
