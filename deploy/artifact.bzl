"""`toxc_artifact` — compile a committed toxc IR `.json` into the portable,
arch-independent artifact (schedule.json + shaders/ + assets/ + exprs.mlir +
services.json) as a Bazel TreeArtifact, by running //:toxc --emit-artifact.

Hermetic: the input is an IR `.json` (no TouchDesigner bridge). The native,
arch-specific bits (exprs/libexprs.so, GLES shaders) are finished for aarch64
INSIDE the image (deploy/nix/apps.nix). The output directory's basename is the
target name, which is how the sbc_application build_data keys it into
`sbcBuildData` for the flake.
"""

def _toxc_artifact_impl(ctx):
    out = ctx.actions.declare_directory(ctx.label.name)
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
            mandatory = True,
            doc = "The committed toxc IR .json (e.g. //graphs:blur_demo.json).",
        ),
        "target": attr.string(
            default = "gles2",
            values = ["desktop_gl", "gles", "gles2"],
            doc = "Render target the artifact is lowered for.",
        ),
        "res": attr.int(default = 256, doc = "Square output resolution."),
        "_toxc": attr.label(
            default = "//:toxc",
            executable = True,
            cfg = "exec",
        ),
    },
)
