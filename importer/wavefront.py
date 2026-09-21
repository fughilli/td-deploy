"""Minimal Wavefront .obj reader — the mesh source for the Render TOP path.

TouchDesigner does not expand procedural SOPs (a Torus SOP is just `torus1.n`
with its parameters; there are no vertices on disk), so geometry has to arrive as
a FILE the importer can read, the same way `moviefilein` brings in an image. A
File In SOP pointing at an `.obj` is that file.

Stdlib only, and deliberately small: positions, normals, texture coordinates and
triangles. Quads and higher n-gons are fan-triangulated. Materials, smoothing
groups and free-form surfaces are ignored — the runtime shades from the geometric
normal, so none of that would be honored anyway.
"""

from __future__ import annotations


class Mesh:
    """Indexed triangle mesh, ready to become interleaved GPU buffers."""

    def __init__(self) -> None:
        self.positions: list[tuple[float, float, float]] = []
        self.normals: list[tuple[float, float, float]] = []
        self.uvs: list[tuple[float, float]] = []
        self.indices: list[int] = []

    @property
    def vertex_count(self) -> int:
        return len(self.positions)

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3

    def bounds(self):
        """(min, max) corners — used to frame the object and sanity-check scale."""
        if not self.positions:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        xs, ys, zs = zip(*self.positions)
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _vref(tok: str, n: int) -> int:
    """Resolve one `f` index. .obj is 1-based and allows negatives (relative to
    the end of the list so far)."""
    i = int(tok)
    return i - 1 if i > 0 else n + i


def parse_obj(text: str) -> Mesh:
    """Parse `.obj` text into an indexed triangle mesh.

    A vertex in .obj is a (position, uv, normal) TRIPLE, and the same position may
    appear with different uvs/normals, so the triple is what gets de-duplicated
    into a GPU vertex — not the position.
    """
    pos: list[tuple[float, float, float]] = []
    uv: list[tuple[float, float]] = []
    nrm: list[tuple[float, float, float]] = []
    mesh = Mesh()
    seen: dict[tuple[int, int, int], int] = {}

    def vertex(tok: str) -> int:
        parts = (tok.split("/") + ["", ""])[:3]
        pi = _vref(parts[0], len(pos))
        ti = _vref(parts[1], len(uv)) if parts[1] else -1
        ni = _vref(parts[2], len(nrm)) if parts[2] else -1
        key = (pi, ti, ni)
        idx = seen.get(key)
        if idx is not None:
            return idx
        idx = len(mesh.positions)
        seen[key] = idx
        mesh.positions.append(pos[pi] if 0 <= pi < len(pos) else (0.0, 0.0, 0.0))
        mesh.uvs.append(uv[ti] if 0 <= ti < len(uv) else (0.0, 0.0))
        mesh.normals.append(nrm[ni] if 0 <= ni < len(nrm) else (0.0, 0.0, 0.0))
        return idx

    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] == "#":
            continue
        tag, _, rest = line.partition(" ")
        f = rest.split()
        if tag == "v" and len(f) >= 3:
            pos.append((float(f[0]), float(f[1]), float(f[2])))
        elif tag == "vt" and len(f) >= 2:
            uv.append((float(f[0]), float(f[1])))
        elif tag == "vn" and len(f) >= 3:
            nrm.append((float(f[0]), float(f[1]), float(f[2])))
        elif tag == "f" and len(f) >= 3:
            ring = [vertex(t) for t in f]
            for k in range(1, len(ring) - 1):  # fan-triangulate
                mesh.indices += [ring[0], ring[k], ring[k + 1]]

    _fill_missing_normals(mesh)
    return mesh


def _fill_missing_normals(mesh: Mesh) -> None:
    """Generate area-weighted normals for any vertex the file didn't supply one
    for. Exporters often omit `vn`, and a mesh with zero normals shades flat
    black under any lighting model."""
    missing = [i for i, n in enumerate(mesh.normals) if n == (0.0, 0.0, 0.0)]
    if not missing:
        return
    acc = [[0.0, 0.0, 0.0] for _ in mesh.positions]
    for t in range(0, len(mesh.indices), 3):
        a, b, c = mesh.indices[t], mesh.indices[t + 1], mesh.indices[t + 2]
        pa, pb, pc = mesh.positions[a], mesh.positions[b], mesh.positions[c]
        u = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
        v = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
        # Cross product magnitude is twice the triangle area, so accumulating the
        # raw cross weights each face by its area for free.
        cx = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        for vi in (a, b, c):
            acc[vi][0] += cx[0]
            acc[vi][1] += cx[1]
            acc[vi][2] += cx[2]
    for i in missing:
        x, y, z = acc[i]
        ln = (x * x + y * y + z * z) ** 0.5
        mesh.normals[i] = (x / ln, y / ln, z / ln) if ln > 1e-12 else (0.0, 0.0, 1.0)
