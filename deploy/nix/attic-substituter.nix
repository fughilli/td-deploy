# Trusted substituter: the self-hosted Attic nix cache (tailnet node `attic`,
# tag:attic — the same cache led_mapper's rigs use). Baked into every player so
# on-device nix operations — and, crucially, a deploy_live's
# `nix copy --substitute-on-destination` — pull closure paths straight from the
# cache over the tailnet instead of over the deployer's uplink. Pull is anonymous
# (public cache); the tag:attic ACL is the access boundary. The matching build-time
# substituter for the DEPLOYER lives in flake.nix `nixConfig` (honored via
# --accept-flake-config), and the post-build push in //deploy:BUILD.bazel
# (_attic_cache/_attic_endpoint). Keep the three in sync if the node/key changes.
{ lib, ... }:
{
  nix.settings = {
    extra-substituters = [ "http://attic.tail6b8ad3.ts.net:8080/splanc" ];
    extra-trusted-public-keys = [ "splanc:MWmTqIgwyOOGTh2wazhPPnVAsIIAV9pEXqhhorIWdvw=" ];
    # Don't let a down/unreachable cache (off the tailnet, CI) stall a build: fall
    # back to the upstreams quickly instead of hanging on the tailnet endpoint.
    connect-timeout = lib.mkDefault 5;
    fallback = lib.mkDefault true;
  };
}
