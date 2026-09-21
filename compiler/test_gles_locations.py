"""Regression: explicit uniform locations must account for array size.

`inject_locations` gives every default-block uniform an explicit location so
glslang can emit OpenGL SPIR-V for the GLES ES 1.00 translation. An array
declaration consumes one location PER ELEMENT, so advancing the counter by one
makes the NEXT uniform collide and glslang rejects the shader with "overlapping
use of location N".

This is not hypothetical: every TouchDesigner GLSL TOP is wrapped with
`uniform TDInfo uTD2DInfos[n]`, so any such TOP that also declared a uniform of
its own failed to translate.
"""

import re
import unittest

from translate_gles import inject_locations, struct_locations


def _locs(src: str) -> dict[str, int]:
    """name -> assigned location, for the default-block uniforms."""
    out = {}
    for line in inject_locations(src).splitlines():
        m = re.match(r"^layout\(location=(\d+)\)\s+uniform\s+\w+\s+(\w+)", line)
        if m:
            out[m.group(2)] = int(m.group(1))
    return out


class StructLocationsTest(unittest.TestCase):
    def test_counts_one_per_member(self):
        self.assertEqual(struct_locations("struct TDInfo { vec4 res; };")["TDInfo"], 1)
        self.assertEqual(struct_locations("struct S { vec4 a; float b; vec2 c; };")["S"], 3)

    def test_matrix_member_spans_its_rows(self):
        self.assertEqual(struct_locations("struct M { mat4 m; };")["M"], 4)

    def test_empty_struct_still_takes_a_slot(self):
        self.assertEqual(struct_locations("struct E { };")["E"], 1)


class InjectLocationsTest(unittest.TestCase):
    def test_struct_array_does_not_collide_with_the_next_uniform(self):
        src = (
            "#version 330 core\n"
            "struct TDInfo { vec4 res; };\n"
            "uniform TDInfo uTD2DInfos[2];\n"
            "uniform float time;\n"
        )
        locs = _locs(src)
        # The array covers 0 and 1, so `time` must land at 2, not 1.
        self.assertEqual(locs["uTD2DInfos"], 0)
        self.assertEqual(locs["time"], 2)

    def test_plain_array_advances_by_its_length(self):
        src = "#version 330 core\nuniform float k[4];\nuniform float after;\n"
        locs = _locs(src)
        self.assertEqual(locs["after"] - locs["k"], 4)

    def test_scalars_still_advance_by_one(self):
        src = "#version 330 core\nuniform float a;\nuniform float b;\n"
        locs = _locs(src)
        self.assertEqual((locs["a"], locs["b"]), (0, 1))

    def test_no_two_uniforms_share_a_location(self):
        src = (
            "#version 330 core\n"
            "struct TDInfo { vec4 res; };\n"
            "uniform TDInfo uTD2DInfos[3];\n"
            "uniform float time;\n"
            "uniform vec4 tint[2];\n"
            "uniform float last;\n"
        )
        assigned = sorted(_locs(src).values())
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_sampler_array_advances_the_binding_counter(self):
        src = (
            "#version 330 core\n" "uniform sampler2D sTD2DInputs[2];\n" "uniform sampler2D other;\n"
        )
        got = re.findall(
            r"layout\(binding=(\d+)\)\s+uniform\s+sampler2D\s+(\w+)", inject_locations(src)
        )
        self.assertEqual(got, [("0", "sTD2DInputs"), ("2", "other")])


if __name__ == "__main__":
    unittest.main()
