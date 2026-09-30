"""A numpy-backed subset of TouchDesigner's `tdu` module.

Only what projects commonly touch from scripts: Matrix (4x4, column-vector
convention like TD), Vector/Position, and a few helpers. TouchDesigner's
Matrix constructor takes 16 values in COLUMN-major order (the OpenGL layout);
`.rows` returns the row-major rows.
"""

from __future__ import annotations

import math

import numpy as np


class Matrix:
    __slots__ = ("m",)

    def __init__(self, *args):
        if not args:
            self.m = np.eye(4)
        elif len(args) == 1 and isinstance(args[0], Matrix):
            self.m = args[0].m.copy()
        elif len(args) == 1 and isinstance(args[0], np.ndarray) and args[0].shape == (4, 4):
            self.m = np.array(args[0], dtype=float)
        else:
            vals = list(args[0]) if len(args) == 1 else list(args)
            if len(vals) == 4 and hasattr(vals[0], "__len__"):
                # a list of rows
                self.m = np.array(vals, dtype=float)
            else:
                if len(vals) != 16:
                    raise ValueError("tdu.Matrix needs 16 values")
                self.m = np.array(vals, dtype=float).reshape(4, 4).T  # column-major in

    # -- accessors ---------------------------------------------------------------
    @property
    def rows(self):
        return [list(map(float, r)) for r in self.m]

    @property
    def cols(self):
        return [list(map(float, c)) for c in self.m.T]

    @property
    def vals(self):
        return [float(v) for v in self.m.T.flatten()]

    def __getitem__(self, idx):
        r, c = idx
        return float(self.m[r, c])

    def __setitem__(self, idx, v):
        r, c = idx
        self.m[r, c] = v

    def numpyArray(self):
        return self.m.copy()

    # -- ops ---------------------------------------------------------------------
    def copy(self):
        return Matrix(self)

    def identity(self):
        self.m = np.eye(4)

    def invert(self):
        self.m = np.linalg.inv(self.m)

    def transpose(self):
        self.m = self.m.T.copy()

    def inverse(self):  # not in TD, but harmless and handy
        return Matrix(np.linalg.inv(self.m))

    def translate(self, tx, ty=0.0, tz=0.0):
        t = np.eye(4)
        t[:3, 3] = [tx, ty, tz]
        self.m = t @ self.m

    def scale(self, sx, sy=None, sz=None):
        sy = sx if sy is None else sy
        sz = sx if sz is None else sz
        self.m = np.diag([sx, sy, sz, 1.0]) @ self.m

    def rotate(self, rx, ry=0.0, rz=0.0):
        self.m = euler_matrix(rx, ry, rz, "xyz") @ self.m

    def decompose(self):
        s, r, t = decompose(self.m, "xyz")
        return tuple(s), tuple(r), tuple(t)

    def __mul__(self, other):
        if isinstance(other, Matrix):
            return Matrix(self.m @ other.m)
        if isinstance(other, (Position, Vector)):
            v = np.append(other.v, 1.0 if isinstance(other, Position) else 0.0)
            r = self.m @ v
            if isinstance(other, Position):
                w = r[3] if abs(r[3]) > 1e-12 else 1.0
                return Position(*(r[:3] / w))
            return Vector(*r[:3])
        if isinstance(other, (int, float)):
            return Matrix(self.m * other)
        return NotImplemented

    def __repr__(self):
        return "tdu.Matrix(%s)" % self.rows


class _Vec3:
    __slots__ = ("v",)

    def __init__(self, *args):
        if len(args) == 1 and hasattr(args[0], "__len__"):
            args = tuple(args[0])
        a = list(args) + [0.0] * (3 - len(args))
        self.v = np.array(a[:3], dtype=float)

    x = property(lambda s: float(s.v[0]), lambda s, val: s.v.__setitem__(0, val))
    y = property(lambda s: float(s.v[1]), lambda s, val: s.v.__setitem__(1, val))
    z = property(lambda s: float(s.v[2]), lambda s, val: s.v.__setitem__(2, val))

    def __getitem__(self, i):
        return float(self.v[i])

    def __iter__(self):
        return iter(float(x) for x in self.v)

    def __len__(self):
        return 3

    def length(self):
        return float(np.linalg.norm(self.v))

    def normalize(self):
        n = np.linalg.norm(self.v)
        if n > 0:
            self.v = self.v / n

    def __add__(self, o):
        return type(self)(*(self.v + np.asarray(list(o), dtype=float)))

    def __sub__(self, o):
        return Vector(*(self.v - np.asarray(list(o), dtype=float)))

    def __mul__(self, k):
        return type(self)(*(self.v * k))

    __rmul__ = __mul__

    def __repr__(self):
        return "%s(%g, %g, %g)" % (type(self).__name__, *self.v)


class Vector(_Vec3):
    def dot(self, o):
        return float(np.dot(self.v, list(o)))

    def cross(self, o):
        return Vector(*np.cross(self.v, list(o)))


class Position(_Vec3):
    pass


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def remap(v, a0, a1, b0, b1):
    return b0 + (v - a0) * (b1 - b0) / (a1 - a0) if a1 != a0 else b0


def rand(seed):
    return (math.sin(seed * 12.9898) * 43758.5453) % 1.0


# -- transform helpers shared with the object-COMP emulation -----------------------


def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def _ry(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)


_AXIS = {"x": _rx, "y": _ry, "z": _rz}


def euler3(rx, ry, rz, order: str = "xyz") -> np.ndarray:
    """3x3 rotation, angles in degrees. `order` is the ORDER OF APPLICATION (TD's
    rord 'xyz' rotates about x first), so the matrix is R_last @ ... @ R_first."""
    ang = {"x": rx, "y": ry, "z": rz}
    m = None
    for ax in order:
        a = ang[ax]
        if not a:
            continue
        a = math.radians(a)
        c, s = math.cos(a), math.sin(a)
        if ax == "x":
            r = ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))
        elif ax == "y":
            r = ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
        else:
            r = ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))
        # small 3x3 products in plain floats: far cheaper than numpy temporaries
        m = (
            r
            if m is None
            else tuple(
                tuple(r[i][0] * m[0][j] + r[i][1] * m[1][j] + r[i][2] * m[2][j] for j in range(3))
                for i in range(3)
            )
        )
    return np.eye(3) if m is None else np.array(m, dtype=float)


def euler_matrix(rx, ry, rz, order="xyz") -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = euler3(rx, ry, rz, order)
    return m


def euler_from3(r: np.ndarray, order: str = "xyz") -> tuple[float, float, float]:
    """Inverse of euler3 for the 6 Tait-Bryan orders (degrees)."""
    # Solve generically: R = A3 @ A2 @ A1 where A_k rotates about order[k].
    i, j, k = ("xyz".index(order[0]), "xyz".index(order[1]), "xyz".index(order[2]))
    parity = (j - i) % 3 == 1  # even permutation
    # R = R_k(c) R_j(b) R_i(a); element R[k][i] = -sin(b) for even parity
    if parity:
        sb = -r[k, i]
    else:
        sb = r[k, i]
    sb = max(-1.0, min(1.0, sb))
    b = math.asin(sb)
    if abs(sb) < 0.999999:
        if parity:
            a = math.atan2(r[k, j], r[k, k])
            c = math.atan2(r[j, i], r[i, i])
        else:
            a = math.atan2(-r[k, j], r[k, k])
            c = math.atan2(-r[j, i], r[i, i])
    else:  # gimbal lock
        c = 0.0
        if parity:
            a = math.atan2(-r[j, k], r[j, j])
        else:
            a = math.atan2(r[j, k], r[j, j])
    out = [0.0, 0.0, 0.0]
    out[i], out[j], out[k] = math.degrees(a), math.degrees(b), math.degrees(c)
    return out[0], out[1], out[2]


def decompose(m: np.ndarray, order="xyz"):
    """4x4 (T @ R @ S, no shear) -> (scale xyz, rotate xyz degrees, translate xyz).
    Plain floats (setTransform runs it a dozen times a frame on 4x4s)."""
    (a00, a01, a02, t0), (a10, a11, a12, t1), (a20, a21, a22, t2) = np.asarray(m, dtype=float)[
        :3
    ].tolist()
    s0 = math.sqrt(a00 * a00 + a10 * a10 + a20 * a20)
    s1 = math.sqrt(a01 * a01 + a11 * a11 + a21 * a21)
    s2 = math.sqrt(a02 * a02 + a12 * a12 + a22 * a22)
    s0 = 1.0 if s0 < 1e-12 else s0
    s1 = 1.0 if s1 < 1e-12 else s1
    s2 = 1.0 if s2 < 1e-12 else s2
    r = [
        [a00 / s0, a01 / s1, a02 / s2],
        [a10 / s0, a11 / s1, a12 / s2],
        [a20 / s0, a21 / s1, a22 / s2],
    ]
    det = (
        r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
        - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
        + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0])
    )
    if det < 0:
        s0 = -s0
        for row in r:
            row[0] = -row[0]
    return np.array([s0, s1, s2]), euler_from3(_Rows(r), order), np.array([t0, t1, t2])


class _Rows:
    """r[i, j] indexing over nested lists (euler_from3 takes numpy arrays too)."""

    __slots__ = ("r",)

    def __init__(self, r):
        self.r = r

    def __getitem__(self, ij):
        return self.r[ij[0]][ij[1]]
