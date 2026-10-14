{
  description = "Logos testnet node: Blockchain, Storage and Delivery";

  nixConfig = {
    extra-substituters = [ "https://cache.nix.logos.co/public" ];
    extra-trusted-public-keys = [ "public:l4HrXgL4nw246+LBh2SOJyhz64BoGegOYLheT/iIAPU=" ];
  };

  # Keep upstream development-only inputs as sources; share identical build graphs.
  inputs = {
    design-system-source = {
      url = "github:logos-co/logos-design-system/88330d04968954be956311b0be429048a41da2ea";
      flake = false;
    };
    test-framework-source = {
      url = "github:logos-co/logos-test-framework/a366934197f5ee2c9009d88d4d0279431bd25c0b";
      flake = false;
    };
    view-runtime-source = {
      url = "github:logos-co/logos-view-module-runtime/42d9140811088f6ac0fcc3ccfc26d647afef1c35";
      flake = false;
    };
    standalone-source = {
      url = "github:logos-co/logos-standalone-app/fdc5ff120a7e493749d7969af6b9f8cd137a0f71";
      flake = false;
    };
    libp2p-cli-source = {
      url = "github:logos-co/logos-logoscore-cli/4e8c7395f631a89c4fb849a27bfda2b4157ed3d2";
      flake = false;
    };
    blockchain = {
      url = "github:logos-blockchain/logos-blockchain-module/0.3.2";
      inputs.logos-module-builder.follows = "module-builder";
    };
    module-builder = {
      url = "github:logos-co/logos-module-builder/16e2f6bd3c06a5119f8c34448933de183205646c";
      inputs = {
        logos-design-system.follows = "design-system-source";
        logos-test-framework.follows = "test-framework-source";
        logos-view-module-runtime.follows = "view-runtime-source";
        logos-standalone-app.follows = "standalone-source";
        nix-bundle-logos-module-install.inputs.logos-package-manager.follows = "cli/logos-package-manager";
      };
    };
    cli = {
      # Published logosctl 0.3.2 source; the release tag points to a different commit.
      url = "github:logos-co/logos-logoscore-cli/96e704df1a8140f069bbf6d5a1cdeab8886c6e41";
      inputs = {
        logos-package-manager.inputs.nix-bundle-appimage.follows = "cli/nix-bundle-appimage";
        logos-package-downloader-module.inputs.logos-package-downloader.inputs.nix-bundle-appimage.follows =
          "cli/nix-bundle-appimage";
        logos-capability-module.inputs.logos-module-builder.inputs = {
          logos-design-system.follows = "design-system-source";
          logos-test-framework.follows = "test-framework-source";
          logos-view-module-runtime.follows = "view-runtime-source";
          logos-plugin-core.follows = "module-builder/logos-plugin-core";
          logos-plugin-qt.follows = "module-builder/logos-plugin-qt";
          logos-qt-sdk.follows = "module-builder/logos-qt-sdk";
          nix-bundle-lgx.follows = "module-builder/nix-bundle-lgx";
          nix-bundle-logos-module-install.follows = "module-builder/nix-bundle-logos-module-install";
        };
        logos-liblogos.inputs = {
          default-module-loader.inputs.logos-module-loader.follows = "cli/logos-liblogos/logos-module-loader";
          logos-capability-module.follows = "cli/logos-capability-module";
          logos-cpp-sdk.follows = "cli/logos-cpp-sdk";
          logos-modules-state-module.follows = "cli/logos-modules-state-module";
        };
        logos-modules-state-module.inputs.logos-module-builder.inputs = {
          logos-design-system.follows = "design-system-source";
          logos-test-framework.follows = "test-framework-source";
          logos-view-module-runtime.follows = "view-runtime-source";
          logos-plugin-core.follows = "module-builder/logos-plugin-core";
          logos-plugin-qt.follows = "module-builder/logos-plugin-qt";
          logos-qt-sdk.follows = "module-builder/logos-qt-sdk";
          nix-bundle-lgx.follows = "module-builder/nix-bundle-lgx";
          nix-bundle-logos-module-install.follows = "module-builder/nix-bundle-logos-module-install";
        };
        logos-package-downloader-module.inputs.logos-module-builder.inputs = {
          nix-bundle-lgx.follows = "cli/nix-bundle-logos-module-install/nix-bundle-lgx";
          logos-design-system.follows = "design-system-source";
          logos-test-framework.follows = "test-framework-source";
          logos-view-module-runtime.follows = "view-runtime-source";
          logos-standalone-app.follows = "standalone-source";
          nix-bundle-logos-module-install.inputs.logos-package-manager.follows = "cli/logos-package-manager";
        };
        logos-package-manager-module.inputs.logos-module-builder.inputs = {
          logos-design-system.follows = "design-system-source";
          logos-test-framework.follows = "test-framework-source";
          logos-view-module-runtime.follows = "view-runtime-source";
          logos-plugin-qt.follows = "cli/logos-package-manager-module/logos-module-builder/logos-plugin-core";
          logos-standalone-app.follows = "standalone-source";
          nix-bundle-lgx.follows = "cli/nix-bundle-logos-module-install/nix-bundle-lgx";
          nix-bundle-logos-module-install.inputs.nix-bundle-lgx.follows = "cli/nix-bundle-logos-module-install/nix-bundle-lgx";
        };
        logos-storage-module.follows = "storage-patched";
      };
    };
    delivery = {
      url = "github:logos-co/logos-delivery-module/v0.3.2";
      inputs = {
        liblogos_rln_module.inputs = {
          liblogos_lez_rln_module.inputs.logos-module-builder.follows = "delivery/liblogos_rln_module/logos-module-builder";
          logos-module-builder.inputs = {
            logos-design-system.follows = "design-system-source";
            logos-test-framework.follows = "test-framework-source";
            logos-view-module-runtime.follows = "view-runtime-source";
            logos-standalone-app.follows = "standalone-source";
            nix-bundle-lgx.follows = "cli/logos-package-downloader-module/logos-module-builder/nix-bundle-lgx";
            nix-bundle-logos-module-install.follows = "cli/logos-package-manager-module/logos-module-builder/nix-bundle-logos-module-install";
          };
        };
        libp2p_module = {
          url = "git+https://github.com/logos-co/logos-libp2p-module";
          inputs = {
            logos-module-builder.follows = "module-builder";
            logoscore-cli.follows = "libp2p-cli-source";
            package-manager.follows = "cli/logos-package-manager";
          };
        };
        logos-module-builder.follows = "module-builder";
      };
    };
    nixpkgs.follows = "cli/nixpkgs";
    storage-patched = {
      url = "path:./nix";
      inputs.upstream.follows = "storage";
    };
    storage = {
      url = "github:logos-co/logos-storage-module/v3.0.2";
      inputs.logos-module-builder.inputs = {
        logos-design-system.follows = "design-system-source";
        logos-test-framework.follows = "test-framework-source";
        logos-view-module-runtime.follows = "view-runtime-source";
        logos-standalone-app.follows = "standalone-source";
        nix-bundle-lgx.follows = "module-builder/nix-bundle-lgx";
        nix-bundle-logos-module-install.follows = "module-builder/nix-bundle-logos-module-install";
      };
    };
  };

  outputs =
    {
      nixpkgs,
      cli,
      blockchain,
      storage-patched,
      delivery,
      ...
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      python = pkgs.python3.withPackages (ps: [ ps.ruamel-yaml ]);
      install = cli.inputs.nix-bundle-logos-module-install.bundlers.${system}.dev;
      storageLib = storage-patched.packages.${system}.lib;
      runtime = cli.packages.${system}.ctl;
      modules = pkgs.symlinkJoin {
        name = "logos-node-modules";
        paths = map install [
          blockchain.packages.${system}.lib
          storageLib
          delivery.packages.${system}.lib
        ];
      };
      node = pkgs.stdenvNoCC.mkDerivation {
        pname = "logos-node";
        version = "0.3.0";
        src = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [
            ./src
            ./config
          ];
        };
        nativeBuildInputs = [ pkgs.makeWrapper ];
        installPhase = ''
          runHook preInstall
          mkdir -p $out/lib/logos-node $out/bin
          cp src/*.py $out/lib/logos-node/
          cp -r config $out/lib/logos-node/
          makeWrapper ${python}/bin/python3 $out/bin/logos-node \
            --add-flags "$out/lib/logos-node/logos_node.py" \
            --set LOGOS_NODE_CTL ${runtime}/bin/logosctl \
            --set LOGOS_NODE_MODULES ${modules}/modules
          makeWrapper ${python}/bin/python3 $out/bin/logosctl \
            --add-flags "$out/lib/logos-node/logosctl.py" \
            --set LOGOS_NODE_CTL ${runtime}/bin/logosctl \
            --set LOGOS_NODE_MODULES ${modules}/modules
          runHook postInstall
        '';
        doInstallCheck = true;
        installCheckPhase = ''
          runHook preInstallCheck
          "$out/bin/logos-node" --help > /dev/null
          test -s "$out/lib/logos-node/config/blockchain-peers.json"
          controlStatus=0
          "$out/bin/logosctl" --data-dir "$TMPDIR/unmanaged-node" daemon status \
            > control.stdout 2> control.stderr || controlStatus=$?
          test "$controlStatus" -eq 1
          test ! -s control.stdout
          printf '%s\n' 'logosctl: no managed node exists in this data directory' | cmp - control.stderr
          test ! -e "$TMPDIR/unmanaged-node"
          runHook postInstallCheck
        '';
        meta.description = "Logos testnet node with Blockchain, Storage and Delivery";
        meta.homepage = "https://github.com/logos-co/logos-node";
        meta.mainProgram = "logos-node";
        meta.license = with pkgs.lib.licenses; [
          mit
          asl20
        ];
        meta.platforms = [ system ];
      };
    in
    {
      packages.${system} = {
        default = node;
        inherit node modules;
      };
      apps.${system} = {
        default = {
          type = "app";
          program = pkgs.lib.getExe node;
          meta.description = "Run a Logos testnet node";
        };
        logosctl = {
          type = "app";
          program = pkgs.lib.getExe' node "logosctl";
          meta.description = "Control a running Logos node";
        };
      };
      formatter.${system} = pkgs.nixfmt-tree;
      devShells.${system}.default = pkgs.mkShellNoCC {
        packages = [ python ];
      };
      checks.${system} = {
        package = node;
        formatting =
          pkgs.runCommand "logos-node-formatting"
            {
              nativeBuildInputs = [ pkgs.nixfmt ];
            }
            ''
              nixfmt --check ${./flake.nix} ${./nix/flake.nix}
              touch $out
            '';
        launcher =
          pkgs.runCommand "logos-node-launcher-tests"
            {
              nativeBuildInputs = [ python ];
            }
            ''
              cp -r ${./src} src
              cp -r ${./tests} tests
              cp -r ${./config} config
              export PYTHONDONTWRITEBYTECODE=1
              python -m unittest discover -s tests -v
              touch $out
            '';
        embedded-storage = pkgs.runCommand "logos-node-embedded-storage-check" { } ''
          grep -aqF LOGOS_STORAGE_HOME ${runtime}/modules-pkg/storage_module/storage_module_plugin.so
          grep -aqF LOGOS_STORAGE_HOME ${modules}/modules/storage_module/storage_module_plugin.so
          touch $out
        '';
      };
    };
}
