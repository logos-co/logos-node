# Logos Node

> [!NOTE]
> This is an experimental proof of concept, developed with substantial AI assistance.
> It demonstrates that a complete Logos node can run through Nix, but is not yet a supported deployment option.
> The longer-term goal is a reviewed, tested and maintained way to run a Logos node with one command.
> We plan to improve binary cache coverage and simplify dependency management across components.

Run a complete Logos node (testnet v0.3) with one command:

```sh
nix run github:logos-co/logos-node
```

Includes Blockchain 0.3.2, Storage 3.0.2, Delivery 0.3.2 and logosctl 0.3.2.
Nix supplies Python and the other runtime dependencies.
Supports Linux x86_64; validated on Debian 13.

## Setup

Install Nix with flakes enabled using the [official installation instructions](https://nix.dev/install-nix.html).
The flake declares the Logos binary cache; Nix may ask whether to trust it.
On multi-user installations, an administrator must allow the cache and its signing key.
See the [Debian 13 appendix](#appendix-debian-13) for a configuration example.

Uncached builds can be substantial.
Build evaluation and Blockchain recompilation were tested within 12 GiB of memory using Nix 2.34.8, two build jobs and two cores per build.

For repeated use, install both commands once:

```sh
nix profile add github:logos-co/logos-node
```

Then run `logos-node` and use `logosctl` from another terminal.

## Run

The launcher generates private configuration on first run and reuses it afterward.
It starts Blockchain, waits for its chain-info API, then waits for Storage and Delivery readiness.
Blockchain synchronization continues in the background.
Modules come from the Nix store; startup does not download them from a catalogue.

The launcher stays in the foreground. Ctrl+C stops the daemon and its module processes.
No system service is installed.
Mining starts with one search thread once the chain is Online and wallet/reward checks pass.
Funded reward targets and networks without mining rewards skip automatic mining.
Funded Blend enrollment remains manual.

### State and configuration

State defaults to `$XDG_DATA_HOME/logos-node`, or `~/.local/share/logos-node`.
`LOGOS_NODE_DATA_DIR` overrides this default for both commands; `--data-dir` takes precedence.
To choose a different directory and advertise a public IPv4 address on first run:

```sh
nix run github:logos-co/logos-node -- --data-dir /path/to/node --public-ip PUBLIC_IPV4
```

Choose an empty, private directory owned by the current user with mode `0700`.
Keep it outside any source checkout: Nix can copy flake sources into its shared store.
Back up the entire data directory while stopped; it contains private wallet keys and node identities.

Existing unmanaged directories are refused.
After interrupted initialization, preserve the entire data directory for inspection.
Restore missing configuration from a backup; do not delete initialization markers or regenerate keys to bypass recovery checks.
Saved settings are reused, and conflicting options are rejected.

First-run port options are `--blockchain-port`, `--blend-port`, `--api-port`,
`--storage-port`, `--delivery-port` and `--discovery-port`.
Port/configuration migrations are not automated; do not change generated ports independently of `node.json`.

### Networking

| Module | Default ports |
| --- | --- |
| Blockchain | UDP 3000; UDP 3400 for Blend |
| Blockchain HTTP API | TCP 8080, bound to loopback |
| Storage | TCP 8091 |
| Delivery | TCP and UDP 30303; discovery UDP 9000 |

Configure the host firewall and NAT forwarding for public P2P access.
Keep the Blockchain HTTP API private.
The launcher does not configure firewall rules.

Delivery uses its `logos.test` kernel/relay preset and automatic NAT detection unless `--public-ip` is supplied.
Automatic NAT handling may use router port mapping protocols.
Storage retains the release's generated mix defaults.
Blockchain bootstrap peers follow the v0.3 operator guide.

Startup waits for ports to become available so recently closed sockets can clear.
These checks are advisory: another process can claim a port before module startup.

## Operation

Stop with Ctrl+C in the launcher terminal or `logosctl daemon stop` from another terminal.
For a custom directory, use `logosctl --data-dir /path/to/node daemon stop`.

With the profile installed, control the node from another terminal:

```sh
logosctl daemon status
logosctl call blockchain_module get_cryptarchia_info
logosctl call storage_module isRunning
logosctl call delivery_module getConnectionStatus
```

For a custom directory, use `logosctl --data-dir /path/to/node ...`.
Without installation, use `nix run github:logos-co/logos-node -- ctl ...`.
Logs may contain sensitive information.
Within the data directory, `session/logs/` contains combined daemon and module output, with 10 MiB rotation and five-file retention by default.
Blockchain also writes to `logs/`, with hourly rotation and ten-file retention by default.

Readiness has a 300-second deadline per stage, adjustable with `--startup-timeout`.
Startup checks confirm that the Blockchain API responds and Storage and Delivery are running.
Startup readiness does not imply an Online chain, wallet readiness, Delivery peer connectivity or successful private Storage downloads.
Check `mode` in the `get_cryptarchia_info` result for `Bootstrapping` or `Online`.
Waiting for chain, wallet and reward readiness has no fixed deadline; mining confirmation uses `--startup-timeout`.
Mining does not restart automatically after you stop it.

See the [node operator guide](https://docs.logos.co/run-a-node) for mining, Blend enrollment and module operations.


## Development

Build and test from a checkout:

```sh
nix build .
nix flake check .
```

## License

Licensed under either the [MIT License](LICENSE-MIT) or the [Apache License 2.0](LICENSE-APACHE-v2), at your option.
Dependencies retain their respective licenses.

## Appendix: Debian 13

Install the Nix installer prerequisites:

```sh
sudo apt-get update
sudo apt-get install curl xz-utils git ca-certificates
```

Follow the [official Nix installation instructions](https://nix.dev/install-nix.html) for a multi-user installation.
Open a new shell and verify `nix --version`.
For an existing Debian-packaged installation, daemon access also requires membership in `nix-users`.

Add these settings to `/etc/nix/nix.conf`:

```ini
experimental-features = nix-command flakes
extra-substituters = https://cache.nix.logos.co/public
extra-trusted-public-keys = public:l4HrXgL4nw246+LBh2SOJyhz64BoGegOYLheT/iIAPU=
max-jobs = 2
cores = 2
```

Restart the daemon with `sudo systemctl restart nix-daemon`, then run the node as your normal user.
Keep signature verification enabled; these cache settings supplement the default Nix cache.
Accepting the flake's cache prompt alone may be insufficient without administrator configuration.
