"""A MINIMAL cc toolchain that LINKS the (pure-Rust) runtime for
aarch64-unknown-linux-gnu using nixpkgs' cross clang wrapper (@aarch64_cc//:cc).

The wrapper is clang (LLVM) with the pkgsCross aarch64 glibc sysroot +
gcc-toolchain (crt/libgcc) baked into its flags, so this toolchain only has to
name it as the linker — no hand-written --target/--sysroot/-B flags. The runtime
compiles no C (deps are pure Rust), so only the link actions run; the other
tool_paths point at /bin/false. Scoped in BUILD to the `:cross` constraint so it
binds ONLY the transitioned runtime build, never host C++.
"""

load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load(
    "@rules_cc//cc:cc_toolchain_config_lib.bzl",
    "action_config",
    "tool",
    "tool_path",
)
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")

_LINK_ACTIONS = [
    ACTION_NAMES.cpp_link_executable,
    ACTION_NAMES.cpp_link_dynamic_library,
    ACTION_NAMES.cpp_link_nodeps_dynamic_library,
]

def _impl(ctx):
    # clang cc-wrapper as the linker driver for every link action. Resolved as a
    # Bazel File so the path works on any exec host; the wrapper's own refs
    # (glibc/gcc-toolchain/resource-dir) are absolute /nix/store paths, resolved
    # locally (build --spawn_strategy=local keeps nix visible, unsandboxed).
    link_configs = [
        action_config(
            action_name = name,
            enabled = True,
            tools = [tool(tool = ctx.file.linker)],
        )
        for name in _LINK_ACTIONS
    ]

    # Bazel requires these to exist; none run (no C is compiled) — point at a no-op.
    tool_paths = [
        tool_path(name = t, path = "/bin/false")
        for t in ("gcc", "ld", "ar", "cpp", "nm", "objdump", "strip")
    ]

    return cc_common.create_cc_toolchain_config_info(
        ctx = ctx,
        toolchain_identifier = "aarch64-linux-gnu-clang",
        host_system_name = "local",
        target_system_name = "aarch64-unknown-linux-gnu",
        target_cpu = "aarch64",
        target_libc = "glibc",
        compiler = "clang",
        abi_version = "unknown",
        abi_libc_version = "unknown",
        action_configs = link_configs,
        tool_paths = tool_paths,
    )

cc_toolchain_config = rule(
    implementation = _impl,
    attrs = {
        "linker": attr.label(allow_single_file = True, mandatory = True),
    },
    provides = [CcToolchainConfigInfo],
)
