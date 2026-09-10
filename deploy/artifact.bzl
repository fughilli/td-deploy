"""`toxc_artifact` — provide the PORTABLE artifact (schedule.json + shaders/ +
assets/ + exprs.mlir + services.json) to the image, as a Bazel TreeArtifact.

Two sources (pick one):

  * `src` = a committed toxc IR `.json` — compiled HERMETICALLY here by running
    //:toxc --emit-artifact (no TouchDesigner bridge). Good for asset-free /
    testcard graphs (e.g. graphs/blur_demo.json).

  * `prebuilt` = the files of an already-emitted artifact dir. Use this for a
    real project that needs the Mac TD bridge to expand + fetch assets (and to
    carry OSC/MIDI services). Produce it once with, e.g.:
        bazel run //:toxc -- /path/ascii_project.toe \\
            --emit-artifact deploy/prebuilt/ascii --target gles2 [--set-file MOVIE=/path]
    then `prebuilt = glob(["prebuilt/ascii/**"])`.

The output directory's basename is the target name — which is how the
sbc_application build_data keys it into `sbcBuildData` for the flake, so keep the
target named `toxc_artifact` (what deploy/nix/apps.nix reads).
"""

def _toxc_artifact_impl(ctx):
    out = ctx.actions.declare_directory(ctx.label.name)

    if ctx.files.prebuilt:
        # Stage the prebuilt artifact files into the TreeArtifact, stripping the
        # workspace-relative prefix so schedule.json lands at the tree root.
        prefix = ctx.attr.prebuilt_strip
        pairs = []
        for f in ctx.files.prebuilt:
            sp = f.short_path
            rel = sp[len(prefix):].lstrip("/") if prefix and sp.startswith(prefix) else sp
            pairs.append(f.path + "\t" + rel)
        manifest = ctx.actions.declare_file(ctx.label.name + ".manifest")
        ctx.actions.write(manifest, "".join([p + "\n" for p in pairs]))
        ctx.actions.run_shell(
            inputs = ctx.files.prebuilt + [manifest],
            outputs = [out],
            arguments = [out.path, manifest.path],
            command = """
set -euo pipefail
out="$1"; manifest="$2"
tab="$(printf '\\t')"
while IFS="$tab" read -r src dest; do
  [ -n "${dest:-}" ] || continue
  mkdir -p "$out/$(dirname "$dest")"
  cp -f "$src" "$out/$dest"
done < "$manifest"
""",
            mnemonic = "ToxcStageArtifact",
            progress_message = "staging prebuilt toxc artifact %{label}",
        )
    else:
        if not ctx.file.src:
            fail("toxc_artifact requires either `src` (an IR .json) or `prebuilt` (artifact files)")
        ctx.actions.run(
            executable = ctx.executable._toxc,
            arguments = [
                ctx.file.src.path,
                "--emit-artifact",
                out.path,
                "--target",
                ctx.attr.target,
                "--res",
                str(ctx.attr.res),
            ],
            inputs = [ctx.file.src],
            outputs = [out],
            mnemonic = "ToxcEmitArtifact",
            progress_message = "toxc emit-artifact %{label}",
        )
    return [DefaultInfo(files = depset([out]))]

toxc_artifact = rule(
    implementation = _toxc_artifact_impl,
    attrs = {
        "src": attr.label(
            allow_single_file = [".json"],
            doc = "A committed toxc IR .json, compiled hermetically via //:toxc.",
        ),
        "prebuilt": attr.label_list(
            allow_files = True,
            doc = "Files of an already-emitted artifact dir (bridge-compiled).",
        ),
        "prebuilt_strip": attr.string(
            default = "deploy/",
            doc = "Workspace-relative prefix stripped from each `prebuilt` path " +
                  "so the artifact tree is rooted at schedule.json.",
        ),
        "target": attr.string(
            default = "desktop_gl",
            values = ["desktop_gl", "gles", "gles2"],
            doc = "Render target the artifact is lowered for (src mode only). " +
                  "Must match the image's softwareGL (desktop_gl=llvmpipe, " +
                  "gles2=VC4 hardware + shaders_gles/).",
        ),
        "res": attr.int(default = 256, doc = "Square output resolution (src mode)."),
        "_toxc": attr.label(
            default = "//:toxc",
            executable = True,
            cfg = "exec",
        ),
    },
)
