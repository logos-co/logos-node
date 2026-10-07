{
  description = "Storage with configurable private state";

  inputs.upstream.url = "github:logos-co/logos-storage-module/v3.0.2";

  outputs =
    { upstream, ... }:
    {
      packages.x86_64-linux = upstream.packages.x86_64-linux // {
        lib = upstream.packages.x86_64-linux.lib.overrideAttrs (old: {
          patches = (old.patches or [ ]) ++ [ ./storage-home.patch ];
        });
      };
    };
}
