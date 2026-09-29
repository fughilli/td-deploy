"""tdhost — run a TouchDesigner project's Python on device, without TouchDesigner.

See host.py for the frame protocol and network.py for the API emulation."""

from .host import Host, sop_to_mesh  # noqa: F401
from .tree import load_network  # noqa: F401
