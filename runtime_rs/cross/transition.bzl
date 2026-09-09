"""Build the runtime for the Pi (aarch64-unknown-linux-gnu) regardless of the
host running Bazel. A plain rust_binary targets the exec host, so on a macOS
deploy host it produces a Mach-O the Pi can't exec ("Exec format error"). This
transitions the build to the zig-backed linux_arm64 (glibc) cc toolchain via
--extra_toolchains, scoped to this subgraph so it never hijacks host C++.
"""

def _impl(_settings, _attr):
    return {
        # Target the Pi: aarch64 + linux + glibc 2.34 (the Pi runs 2.40, and
        # glibc is forward-compatible, so a 2.34-linked binary runs there). The
        # gnu cc toolchain is libc-version-aware, so the platform must carry the
        # matching glibc constraint.
        "//command_line_option:platforms": ["@zig_sdk//libc_aware/platform:linux_arm64_gnu.2.34"],
        # Link with zig's glibc cc toolchain, ONLY for this transitioned build
        # (higher priority than registered toolchains, so no global effect).
        "//command_line_option:extra_toolchains": ["@zig_sdk//libc_aware/toolchain:linux_arm64_gnu.2.34"],
    }

_linux_transition = transition(
    implementation = _impl,
    inputs = [],
    outputs = [
        "//command_line_option:platforms",
        "//command_line_option:extra_toolchains",
    ],
)

def _binary_impl(ctx):
    src = ctx.attr.src[0][DefaultInfo].files.to_list()[0]

    # Name the output "toxc_runtime" (not the target name) so sbc-deploy keys it
    # as "toxc_runtime" in sbcBuildData for deploy/nix/apps.nix.
    out = ctx.actions.declare_file("toxc_runtime")
    ctx.actions.symlink(output = out, target_file = src, is_executable = True)
    return [DefaultInfo(files = depset([out]), executable = out)]

linux_binary = rule(
    implementation = _binary_impl,
    executable = True,
    attrs = {
        "src": attr.label(cfg = _linux_transition, mandatory = True),
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
    },
)
