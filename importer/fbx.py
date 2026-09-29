"""Binary FBX reader: meshes, skins and the model hierarchy, dependency-free.

TouchDesigner's FBX COMP imports geometry at load time through the FBX SDK — the
.toe only stores a reference to the .fbx file — so the compiler reads the file
itself and bakes the mesh (and its skin) into the artifact.

Covers the binary FBX 7.x container (the format every DCC exports by default):
    header "Kaydara FBX Binary  \\x00\\x1a\\x00" + u32 version, then node records
    (end offset, property count, property bytes, name) with typed properties —
    scalars (Y C I F D L), arrays (f d l i b; optionally zlib-deflated), strings
    (S) and raw blobs (R) — and nested child records ended by a null record
    (13 bytes before 7.5, 25 bytes from 7.5 on, where offsets become 64-bit).

Extracted:
  * Geometry (Mesh): control points, polygon vertex indices (a negative index
    closes a polygon: ~i), normals and UVs from LayerElementNormal/UV with their
    mapping (ByPolygonVertex / ByControlPoint / ByVertice / AllSame) and
    reference (Direct / IndexToDirect) modes;
  * Model: name, type, Lcl Translation/Rotation/Scaling, PreRotation, RotationOrder;
  * Deformer Skin -> Cluster: control point Indexes + Weights, Transform (mesh
    bind) and TransformLink (bone bind, global);
  * Connections (child -> parent object ids), Material, Texture file names.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

_MAGIC = b"Kaydara FBX Binary  \x00"


@dataclass
class FbxNode:
    name: str
    props: list
    children: list = field(default_factory=list)

    def find(self, name):
        for c in self.children:
            if c.name == name:
                return c
        return None

    def find_all(self, name):
        return [c for c in self.children if c.name == name]

    def prop70(self) -> dict:
        """Properties70 P records -> {name: [values...]}."""
        out = {}
        p70 = self.find("Properties70")
        if p70 is None:
            return out
        for p in p70.find_all("P"):
            if p.props:
                out[p.props[0]] = p.props[4:]
        return out


def _read_array(data, off, dtype, n_code):
    length, encoding, comp_len = struct.unpack_from("<III", data, off)
    off += 12
    raw = data[off : off + comp_len]
    off += comp_len
    if encoding == 1:
        raw = zlib.decompress(raw)
    arr = np.frombuffer(raw, dtype=dtype, count=length).copy()
    return arr, off


_ARRAY_TYPES = {"f": "<f4", "d": "<f8", "l": "<i8", "i": "<i4", "b": "u1"}


def _read_prop(data, off):
    t = chr(data[off])
    off += 1
    if t == "Y":
        return struct.unpack_from("<h", data, off)[0], off + 2
    if t == "C":
        return bool(data[off]), off + 1
    if t == "I":
        return struct.unpack_from("<i", data, off)[0], off + 4
    if t == "F":
        return struct.unpack_from("<f", data, off)[0], off + 4
    if t == "D":
        return struct.unpack_from("<d", data, off)[0], off + 8
    if t == "L":
        return struct.unpack_from("<q", data, off)[0], off + 8
    if t in _ARRAY_TYPES:
        return _read_array(data, off, _ARRAY_TYPES[t], t)
    if t in ("S", "R"):
        (n,) = struct.unpack_from("<I", data, off)
        off += 4
        b = data[off : off + n]
        off += n
        if t == "S":
            # FBX names are "Name\x00\x01Class"; keep the readable name part
            s = b.decode("utf-8", "replace")
            if "\x00\x01" in s:
                a, _, c = s.partition("\x00\x01")
                s = f"{c}::{a}" if c else a
            return s, off
        return bytes(b), off
    raise ValueError(f"unknown FBX property type {t!r} at {off - 1}")


def _read_node(data, off, v75):
    if v75:
        end, nprops, _plen = struct.unpack_from("<QQQ", data, off)
        off += 24
    else:
        end, nprops, _plen = struct.unpack_from("<III", data, off)
        off += 12
    nlen = data[off]
    off += 1
    if end == 0:
        return None, off
    name = data[off : off + nlen].decode("ascii", "replace")
    off += nlen
    props = []
    for _ in range(nprops):
        v, off = _read_prop(data, off)
        props.append(v)
    node = FbxNode(name, props)
    while off < end:
        child, off = _read_node(data, off, v75)
        if child is None:  # null record closes the child list
            break
        node.children.append(child)
    return node, end


def parse(data: bytes) -> tuple[int, list[FbxNode]]:
    if not data.startswith(_MAGIC):
        raise ValueError("not a binary FBX file (ASCII FBX is not supported)")
    (version,) = struct.unpack_from("<I", data, 23)
    v75 = version >= 7500
    off = 27
    top = []
    while off < len(data):
        node, off2 = _read_node(data, off, v75)
        if node is None:
            break
        top.append(node)
        off = off2
    return version, top


# ---------------------------------------------------------------- scene model


@dataclass
class Model:
    id: int
    name: str
    kind: str
    t: tuple = (0.0, 0.0, 0.0)
    r: tuple = (0.0, 0.0, 0.0)
    s: tuple = (1.0, 1.0, 1.0)
    pre_r: tuple = (0.0, 0.0, 0.0)
    post_r: tuple = (0.0, 0.0, 0.0)
    rot_order: int = 0  # FBX eEulerXYZ = 0
    parent: int | None = None


@dataclass
class Cluster:
    bone_id: int
    indexes: np.ndarray
    weights: np.ndarray
    transform: np.ndarray  # mesh global at bind
    transform_link: np.ndarray  # bone global at bind


@dataclass
class Mesh:
    id: int
    name: str
    points: np.ndarray  # (P,3) control points
    poly_index: np.ndarray  # raw PolygonVertexIndex
    normals: np.ndarray | None  # per polygon-vertex (V,3)
    uvs: np.ndarray | None  # per polygon-vertex (V,2)
    model_id: int | None = None
    clusters: list = field(default_factory=list)
    material_ids: list = field(default_factory=list)


class Scene:
    def __init__(self, path: str):
        with open(path, "rb") as fh:
            data = fh.read()
        self.version, top = parse(data)
        self.path = path
        objects = next((n for n in top if n.name == "Objects"), None)
        conns = next((n for n in top if n.name == "Connections"), None)
        settings = next((n for n in top if n.name == "GlobalSettings"), None)
        self.global_settings = settings.prop70() if settings is not None else {}
        self.nodes: dict[int, FbxNode] = {}
        for n in objects.children if objects else []:
            if n.props and isinstance(n.props[0], int):
                self.nodes[n.props[0]] = n
        # child -> [parents], parent -> [children] (object-object links only)
        self.parents: dict[int, list] = {}
        self.children: dict[int, list] = {}
        self.prop_links: list = []
        for c in conns.children if conns else []:
            if len(c.props) >= 3 and c.props[0] == "OO":
                ch, pa = c.props[1], c.props[2]
                self.parents.setdefault(ch, []).append(pa)
                self.children.setdefault(pa, []).append(ch)
            elif len(c.props) >= 4 and c.props[0] == "OP":
                self.prop_links.append((c.props[1], c.props[2], c.props[3]))
        self.models = self._models()
        self.meshes = self._meshes()

    # -- models -----------------------------------------------------------------
    def _models(self) -> dict[int, Model]:
        out = {}
        for oid, n in self.nodes.items():
            if n.name != "Model":
                continue
            name = str(n.props[1]).split("::", 1)[-1]
            kind = n.props[2] if len(n.props) > 2 else ""
            p = n.prop70()

            def v3(key, d):
                val = p.get(key)
                return tuple(float(x) for x in val[:3]) if val and len(val) >= 3 else d

            m = Model(
                oid,
                name,
                kind,
                v3("Lcl Translation", (0.0, 0.0, 0.0)),
                v3("Lcl Rotation", (0.0, 0.0, 0.0)),
                v3("Lcl Scaling", (1.0, 1.0, 1.0)),
                v3("PreRotation", (0.0, 0.0, 0.0)),
                v3("PostRotation", (0.0, 0.0, 0.0)),
                int(p.get("RotationOrder", [0])[0]) if p.get("RotationOrder") else 0,
            )
            for pa in self.parents.get(oid, []):
                if pa in self.nodes and self.nodes[pa].name == "Model":
                    m.parent = pa
                    break
            out[oid] = m
        return out

    def model_path(self, mid: int) -> str:
        parts = []
        cur = self.models.get(mid)
        while cur is not None:
            parts.append(cur.name)
            cur = self.models.get(cur.parent) if cur.parent is not None else None
        return "/" + "/".join(reversed(parts))

    def local_matrix(self, mid: int) -> np.ndarray:
        """FBX node local transform: T * Roff * Rp * Rpre * R * Rpost^-1 * Rp^-1 *
        Soff * Sp * S * Sp^-1 (pivots/offsets are rare in exports; honoured when set)."""
        m = self.models[mid]
        p = self.nodes[mid].prop70()

        def v3(key, d=(0.0, 0.0, 0.0)):
            val = p.get(key)
            return (
                np.array([float(x) for x in val[:3]])
                if val and len(val) >= 3
                else np.array(d, float)
            )

        T = _trans(np.array(m.t))
        Roff, Rp, Soff, Sp = (
            _trans(v3(k))
            for k in ("RotationOffset", "RotationPivot", "ScalingOffset", "ScalingPivot")
        )
        Rpre = _euler_fbx(m.pre_r, 0)
        R = _euler_fbx(m.r, m.rot_order)
        Rpost = _euler_fbx(m.post_r, 0)
        S = np.diag([*m.s, 1.0])
        inv = np.linalg.inv
        return T @ Roff @ Rp @ Rpre @ R @ inv(Rpost) @ inv(Rp) @ Soff @ Sp @ S @ inv(Sp)

    def global_matrix(self, mid: int) -> np.ndarray:
        m = self.local_matrix(mid)
        cur = self.models[mid].parent
        while cur is not None:
            m = self.local_matrix(cur) @ m
            cur = self.models[cur].parent
        return m

    # -- meshes -------------------------------------------------------------------
    def _meshes(self) -> dict[int, Mesh]:
        out = {}
        for oid, n in self.nodes.items():
            if n.name != "Geometry" or (len(n.props) > 2 and n.props[2] != "Mesh"):
                continue
            verts = n.find("Vertices")
            pvi = n.find("PolygonVertexIndex")
            if verts is None or pvi is None:
                continue
            pts = np.asarray(verts.props[0], np.float64).reshape(-1, 3)
            pidx = np.asarray(pvi.props[0], np.int64)
            cp_of_pv = np.where(pidx < 0, ~pidx, pidx)
            poly_of_pv = np.cumsum(np.concatenate([[0], (pidx < 0)[:-1].astype(np.int64)]))
            normals = _layer(
                n.find("LayerElementNormal"), "Normals", "NormalsIndex", cp_of_pv, poly_of_pv, 3
            )
            uvs = _layer(n.find("LayerElementUV"), "UV", "UVIndex", cp_of_pv, poly_of_pv, 2)
            name = str(n.props[1]).split("::", 1)[-1]
            mesh = Mesh(oid, name, pts, pidx, normals, uvs)
            for pa in self.parents.get(oid, []):
                if pa in self.models:
                    mesh.model_id = pa
            for ch in self.children.get(oid, []):
                dn = self.nodes.get(ch)
                if (
                    dn is not None
                    and dn.name == "Deformer"
                    and len(dn.props) > 2
                    and dn.props[2] == "Skin"
                ):
                    for cl_id in self.children.get(ch, []):
                        cl = self.nodes.get(cl_id)
                        if cl is None or cl.name != "Deformer":
                            continue
                        bone = next(
                            (c for c in self.children.get(cl_id, []) if c in self.models), None
                        )
                        idx = cl.find("Indexes")
                        wts = cl.find("Weights")
                        tr = cl.find("Transform")
                        tl = cl.find("TransformLink")
                        if bone is None or tr is None or tl is None:
                            continue
                        mesh.clusters.append(
                            Cluster(
                                bone,
                                (
                                    np.asarray(idx.props[0], np.int64)
                                    if idx
                                    else np.zeros(0, np.int64)
                                ),
                                np.asarray(wts.props[0], np.float64) if wts else np.zeros(0),
                                np.asarray(tr.props[0], np.float64).reshape(4, 4).T,
                                np.asarray(tl.props[0], np.float64).reshape(4, 4).T,
                            )
                        )
            if mesh.model_id is not None:
                mesh.material_ids = [
                    c
                    for c in self.children.get(mesh.model_id, [])
                    if self.nodes.get(c, FbxNode("", [])).name == "Material"
                ]
            out[oid] = mesh
        return out

    def mesh_by_path(self, geopath: str) -> Mesh | None:
        """TD addresses FBX geometry as '/<model path>/<geometry name>' (e.g.
        '/output_unwrapped/output_unwrapped'). Match on the model path first."""
        want = geopath.rstrip("/")
        for mesh in self.meshes.values():
            if mesh.model_id is None:
                continue
            mp = self.model_path(mesh.model_id)
            if want in (mp, mp + "/" + mesh.name, mp + "/" + mp.rsplit("/", 1)[-1]):
                return mesh
        # fall back on the last path segment naming the model or the geometry
        leaf = want.rsplit("/", 1)[-1]
        for mesh in self.meshes.values():
            if mesh.name == leaf or (mesh.model_id and self.models[mesh.model_id].name == leaf):
                return mesh
        return next(iter(self.meshes.values()), None)

    def texture_files(self) -> list[str]:
        out = []
        for n in self.nodes.values():
            if n.name == "Texture":
                for key in ("RelativeFilename", "FileName"):
                    f = n.find(key)
                    if f is not None and f.props and f.props[0]:
                        out.append(str(f.props[0]))
                        break
        return out


def _trans(v) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = v
    return m


# FBX EFbxRotationOrder: XYZ, XZY, YZX, YXZ, ZXY, ZYX (application order)
_FBX_ORDER = ["xyz", "xzy", "yzx", "yxz", "zxy", "zyx"]


def _euler_fbx(r, order: int) -> np.ndarray:
    import math

    def rx(a):
        c, s = math.cos(a), math.sin(a)
        return np.array([[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1]])

    def ry(a):
        c, s = math.cos(a), math.sin(a)
        return np.array([[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]])

    def rz(a):
        c, s = math.cos(a), math.sin(a)
        return np.array([[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])

    ang = {"x": math.radians(r[0]), "y": math.radians(r[1]), "z": math.radians(r[2])}
    f = {"x": rx, "y": ry, "z": rz}
    m = np.eye(4)
    for ax in _FBX_ORDER[order % 6]:
        m = f[ax](ang[ax]) @ m
    return m


def _layer(layer, data_key, index_key, cp_of_pv, poly_of_pv, width):
    """Resolve a LayerElement to per-polygon-vertex values (V, width)."""
    if layer is None:
        return None
    dn = layer.find(data_key)
    if dn is None:
        return None
    vals = np.asarray(dn.props[0], np.float64).reshape(-1, width)
    mapping = (layer.find("MappingInformationType") or FbxNode("", ["ByPolygonVertex"])).props[0]
    ref = (layer.find("ReferenceInformationType") or FbxNode("", ["Direct"])).props[0]
    idx_node = layer.find(index_key)
    if mapping in ("ByPolygonVertex",):
        base = np.arange(len(cp_of_pv))
    elif mapping in ("ByControlPoint", "ByVertice", "ByVertex"):
        base = cp_of_pv
    elif mapping == "ByPolygon":
        base = poly_of_pv
    elif mapping == "AllSame":
        base = np.zeros(len(cp_of_pv), np.int64)
    else:
        base = np.arange(len(cp_of_pv))
    if ref in ("IndexToDirect", "Index") and idx_node is not None:
        ind = np.asarray(idx_node.props[0], np.int64)
        base = ind[base]
    base = np.clip(base, 0, len(vals) - 1)
    return vals[base]


# ---------------------------------------------------------------- baking


def triangulate(mesh: Mesh):
    """Per-polygon-vertex attributes -> deduplicated vertices + triangle indices.

    Returns (cp_index (V,), normals (V,3), uvs (V,2), tris (T,3)). `cp_index`
    maps each output vertex to its control point (for positions and skin)."""
    pidx = mesh.poly_index
    cp = np.where(pidx < 0, ~pidx, pidx)
    n = mesh.normals if mesh.normals is not None else np.zeros((len(cp), 3))
    uv = mesh.uvs if mesh.uvs is not None else np.zeros((len(cp), 2))
    # dedupe (cp, normal, uv) — quantized so tiny float noise still merges
    key = np.concatenate([cp[:, None].astype(np.float64), np.round(n, 5), np.round(uv, 6)], axis=1)
    _uniq, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    tris = []
    start = 0
    ends = np.nonzero(pidx < 0)[0]
    for e in ends:
        poly = inv[start : e + 1]
        for k in range(1, len(poly) - 1):
            tris.append((poly[0], poly[k], poly[k + 1]))
        start = e + 1
    return cp[first], n[first], uv[first], np.asarray(tris, np.int64).reshape(-1, 3)


def skin_weights(mesh: Mesh, n_points: int, max_influences: int = 4):
    """Per control point: top-`max_influences` (cluster index, weight), renormalized."""
    per = [[] for _ in range(n_points)]
    for ci, cl in enumerate(mesh.clusters):
        for i, w in zip(cl.indexes, cl.weights):
            if 0 <= i < n_points and w > 0:
                per[i].append((w, ci))
    idx = np.zeros((n_points, max_influences), np.float32)
    wts = np.zeros((n_points, max_influences), np.float32)
    for i, lst in enumerate(per):
        lst.sort(reverse=True)
        lst = lst[:max_influences]
        s = sum(w for w, _ in lst) or 1.0
        for k, (w, ci) in enumerate(lst):
            idx[i, k] = ci
            wts[i, k] = w / s
    return idx, wts


def tangents(pos, nrm, uv, tris):
    """Per-vertex tangents (xyz + handedness w) for normal mapping (Lengyel)."""
    t = np.zeros_like(pos)
    b = np.zeros_like(pos)
    p0, p1, p2 = pos[tris[:, 0]], pos[tris[:, 1]], pos[tris[:, 2]]
    w0, w1, w2 = uv[tris[:, 0]], uv[tris[:, 1]], uv[tris[:, 2]]
    e1, e2 = p1 - p0, p2 - p0
    d1, d2 = w1 - w0, w2 - w0
    r = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    r = np.where(np.abs(r) < 1e-12, 1e-12, r)
    sdir = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) / r[:, None]
    tdir = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) / r[:, None]
    for k in range(3):
        np.add.at(t, tris[:, k], sdir)
        np.add.at(b, tris[:, k], tdir)
    t = t - nrm * np.sum(nrm * t, axis=1, keepdims=True)
    t /= np.linalg.norm(t, axis=1, keepdims=True) + 1e-12
    w = np.where(np.sum(np.cross(nrm, t) * b, axis=1) < 0, -1.0, 1.0)
    return np.concatenate([t, w[:, None]], axis=1)
