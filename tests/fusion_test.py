"""TOP producer/consumer fusion: a crop feeding only a transform composes into one
coordinate-remap pass (lowering/_fuse_coord_remaps)."""
import unittest

from ir.graph import Graph
from lowering.lower import lower


def _graph(crop_shared: bool = False):
    ot = {"w": 256, "h": 256, "fmt": "rgba8"}
    nodes = [
        {"id": "src", "op": "image_in", "family": "TOP", "inputs": [], "out_type": ot},
        {"id": "c", "op": "crop", "family": "TOP", "inputs": ["src"], "out_type": ot},
        {"id": "t", "op": "transform", "family": "TOP", "inputs": ["c"], "out_type": ot},
    ]
    if crop_shared:
        # A second consumer of the crop -> not safe to fuse it away.
        nodes.append({"id": "n", "op": "null", "family": "TOP",
                      "inputs": ["c"], "out_type": ot})
    return Graph.from_json({"output": "t", "nodes": nodes})


class FusionTest(unittest.TestCase):
    def test_crop_into_transform(self):
        plan = lower(_graph(), "desktop_gl")
        shaders = [s for s in plan.steps if s.kind == "shader"]
        self.assertEqual(len(shaders), 1)          # crop fused into transform
        t = shaders[0]
        self.assertEqual(t.node_id, "t")           # transform keeps its id
        self.assertEqual(t.inputs, ["src"])        # now samples the source
        self.assertIn("uCropRect", t.uniforms)     # both uniform sets carried
        self.assertIn("uScale", t.uniforms)
        self.assertIn("mix(uCropRect", t.fragment)  # composed coordinate remap
        self.assertEqual(t.params.get("_fused_from"), "c")

    def test_shared_crop_not_fused(self):
        # When the crop feeds another op too, it must stay its own pass.
        plan = lower(_graph(crop_shared=True), "desktop_gl")
        ids = {s.node_id for s in plan.steps if s.kind == "shader"}
        self.assertEqual(ids, {"c", "t"})


if __name__ == "__main__":
    unittest.main()
