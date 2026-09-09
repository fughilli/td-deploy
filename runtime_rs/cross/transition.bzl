"""Build the runtime for the Pi (aarch64-unknown-linux-gnu) regardless of the
host running Bazel. A plain rust_binary targets the exec host, so on a macOS
deploy host it produces a Mach-O the Pi can't exec ("Exec format error"). This
transitions the build to the zig-backed linux_arm64 (glibc) cc toolchain via
--extra_toolchains, scoped to this subgraph so it never hijacks host C++.
"""

def _impl(_settings, _attr):
    return {
        # Target the Pi via our own platform (aarch64 + linux + :cross). The
        # :cross constraint is what binds the nixpkgs clang cc toolchain (see
        # BUILD), so no --extra_toolchains and no host hijack.
        "//command_line_option:platforms": ["//runtime_rs/cross:aarch64_linux"],
    }

_linux_transition = transition(
    implementation = _impl,
    inputs = [],
    outputs = [
        "//command_line_option:platforms",
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
