"""importer/fbx.py — the dependency-free binary FBX reader behind FBX COMP support.

A tiny binary FBX 7.4 file (a skinned quad + one bone) is written in-test, then
read back: control points, polygon triangulation, normals/UVs through their
mapping + reference modes, the model hierarchy's local transforms, and the skin
cluster (indexes, weights, bind matrices).
"""

import os
import struct
import sys
import tempfile
import unittest
import zlib

import numpy as np

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "importer")
)

import fbx as FBX  # noqa: E402

# ------------------------------------------------------------- a minimal writer


def _prop(v):
    if isinstance(v, str):
        b = v.encode()
        return b"S" + struct.pack("<I", len(b)) + b
    if isinstance(v, bool):
        return b"C" + bytes([1 if v else 0])
    if isinstance(v, int):
        return b"L" + struct.pack("<q", v)
    if isinstance(v, float):
        return b"D" + struct.pack("<d", v)
    if isinstance(v, tuple) and v[0] in ("d", "i"):
        kind, vals, compress = v[0], v[1], v[2] if len(v) > 2 else False
        raw = struct.pack(f"<{len(vals)}{'d' if kind == 'd' else 'i'}", *vals)
        enc = 0
        if compress:
            raw, enc = zlib.compress(raw), 1
        return kind.encode() + struct.pack("<III", len(vals), enc, len(raw)) + raw
    raise TypeError(v)


def _node(name, props=(), children=(), offset=0):
    pbytes = b"".join(_prop(p) for p in props)
    head_len = 13 + len(name)
    body = b""
    pos = offset + head_len + len(pbytes)
    for c in children:
        cb = _node(*c, offset=pos + len(body))
        body += cb
    if children:
        body += b"\x00" * 13
    end = offset + head_len + len(pbytes) + len(body)
    return (
        struct.pack("<IIIB", end, len(props), len(pbytes), len(name))
        + name.encode()
        + pbytes
        + body
    )


def write_fbx(path, top):
    out = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7400)
    for n in top:
        out += _node(*n, offset=len(out))
    out += b"\x00" * 13
    with open(path, "wb") as fh:
        fh.write(out)


def _p70(*ps):
    return ("Properties70", (), [("P", p) for p in ps])


def _mat(t):
    m = np.eye(4)
    m[:3, 3] = t
    return tuple(m.T.reshape(-1).tolist())  # FBX stores column-major


def scene_file(tmp):
    quad = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0]
    geo = (
        "Geometry",
        (100, "Quad\x00\x01Geometry", "Mesh"),
        [
            ("Vertices", (("d", quad, True),)),  # deflated array
            ("PolygonVertexIndex", (("i", [0, 1, 2, -4]),)),
            (
                "LayerElementNormal",
                (0,),
                [
                    ("MappingInformationType", ("ByPolygonVertex",)),
                    ("ReferenceInformationType", ("Direct",)),
                    ("Normals", (("d", [0.0, 0.0, 1.0] * 4),)),
                ],
            ),
            (
                "LayerElementUV",
                (0,),
                [
                    ("MappingInformationType", ("ByPolygonVertex",)),
                    ("ReferenceInformationType", ("IndexToDirect",)),
                    ("UV", (("d", [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]),)),
                    ("UVIndex", (("i", [0, 1, 2, 3]),)),
                ],
            ),
        ],
    )
    model = (
        "Model",
        (200, "Quad\x00\x01Model", "Mesh"),
        [
            _p70(
                ("Lcl Rotation", "Lcl Rotation", "", "A", -90.0, 0.0, 0.0),
                ("Lcl Scaling", "Lcl Scaling", "", "A", 100.0, 100.0, 100.0),
            )
        ],
    )
    bone = (
        "Model",
        (300, "Bone\x00\x01Model", "LimbNode"),
        [_p70(("Lcl Translation", "Lcl Translation", "", "A", 1.0, 2.0, 3.0))],
    )
    skin = ("Deformer", (400, "Skin\x00\x01Deformer", "Skin"), [])
    cluster = (
        "Deformer",
        (500, "Cl\x00\x01SubDeformer", "Cluster"),
        [
            ("Indexes", (("i", [0, 1, 2]),)),
            ("Weights", (("d", [1.0, 0.5, 0.25]),)),
            ("Transform", (("d", list(_mat([0, 0, 0]))),)),
            ("TransformLink", (("d", list(_mat([1, 2, 3]))),)),
        ],
    )
    conns = (
        "Connections",
        (),
        [
            ("C", ("OO", 100, 200)),
            ("C", ("OO", 200, 0)),
            ("C", ("OO", 300, 0)),
            ("C", ("OO", 400, 100)),
            ("C", ("OO", 500, 400)),
            ("C", ("OO", 300, 500)),
        ],
    )
    path = os.path.join(tmp, "quad.fbx")
    write_fbx(path, [("Objects", (), [geo, model, bone, skin, cluster]), conns])
    return path


class FbxTest(unittest.TestCase):
    def setUp(self):
        self.sc = FBX.Scene(scene_file(tempfile.mkdtemp()))

    def test_mesh_and_layers(self):
        mesh = self.sc.mesh_by_path("/Quad/Quad")
        self.assertIsNotNone(mesh)
        np.testing.assert_array_equal(mesh.points[2], [1.0, 1.0, 0.0])
        cp, nrm, uv, tris = FBX.triangulate(mesh)
        self.assertEqual(tris.shape, (2, 3))  # the quad fans into two triangles
        self.assertEqual(sorted(cp.tolist()), [0, 1, 2, 3])
        np.testing.assert_allclose(nrm, np.tile([0.0, 0.0, 1.0], (4, 1)))
        # UV of control point 2 went through UVIndex -> UV
        np.testing.assert_allclose(uv[list(cp).index(2)], [1.0, 1.0])

    def test_hierarchy_transforms(self):
        mid = next(m.id for m in self.sc.models.values() if m.name == "Quad")
        g = self.sc.global_matrix(mid)
        # rx -90 then scale 100: +Y maps to -Z
        np.testing.assert_allclose(g[:3, :3] @ [0, 1, 0], [0, 0, -100], atol=1e-9)
        self.assertEqual(self.sc.model_path(mid), "/Quad")

    def test_skin_cluster(self):
        mesh = self.sc.mesh_by_path("/Quad/Quad")
        self.assertEqual(len(mesh.clusters), 1)
        cl = mesh.clusters[0]
        self.assertEqual(self.sc.models[cl.bone_id].name, "Bone")
        np.testing.assert_allclose(cl.transform_link[:3, 3], [1, 2, 3])
        # the bind matrix matches the bone's rest global (its Lcl chain)
        np.testing.assert_allclose(cl.transform_link, self.sc.global_matrix(cl.bone_id))
        idx, w = FBX.skin_weights(mesh, len(mesh.points))
        self.assertEqual(w[0, 0], 1.0)  # renormalized per point
        self.assertEqual(w[3].sum(), 0.0)  # point 3 unweighted

    def test_tangents_follow_uv_u(self):
        mesh = self.sc.mesh_by_path("/Quad/Quad")
        cp, nrm, uv, tris = FBX.triangulate(mesh)
        tan = FBX.tangents(mesh.points[cp], nrm, uv, tris)
        np.testing.assert_allclose(tan[:, :3], np.tile([1.0, 0.0, 0.0], (4, 1)), atol=1e-9)
        self.assertTrue(np.all(tan[:, 3] == 1.0))


if __name__ == "__main__":
    unittest.main()
