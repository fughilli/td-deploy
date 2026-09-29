"""tdhost — run a TouchDesigner project's Python on device, without TouchDesigner.

See host.py for the frame protocol and network.py for the API emulation."""

import os as _os

# numpy's BLAS/LAPACK: one thread. The host's linear algebra is 4x4 matrices;
# OpenBLAS's default pool (a thread per core) turns every np.linalg.inv into a
# cross-core wake-up that costs milliseconds on a busy box (a pose/matting
# sidecar running), where one thread takes microseconds. Must be set before numpy
# loads; child processes (a project's sidecars) inherit it unless they override.
for _v in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    _os.environ.setdefault(_v, "1")

from .host import Host, sop_to_mesh  # noqa: E402,F401
from .tree import load_network  # noqa: E402,F401
