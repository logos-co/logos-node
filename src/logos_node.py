"""Foreground supervisor for a pinned Logos testnet node."""

import argparse
import contextlib
import fcntl
import ipaddress
import io
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from ruamel.yaml import YAML, YAMLError

SCHEMA = 1
PORTS = dict(blockchain=3000, blend=3400, api=8080, storage=8091,
             delivery=30303, discovery=9000)
MODULES = ("blockchain_module", "storage_module", "delivery_module")
CONFIG = Path(__file__).resolve().parent / "config"
if not CONFIG.exists():
    CONFIG = Path(__file__).resolve().parent.parent / "config"


class NodeError(Exception):
    pass


class MethodTimeout(NodeError):
    pass


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    write_text(path, json.dumps(value, indent=2) + "\n")


def write_text(path, value):
    # Rename only complete documents; keys must never be world-readable.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def result_value(doc):
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        raise NodeError("logosctl returned an error")
    value = doc.get("result")
    if isinstance(value, dict) and "success" in value:
        if value["success"] is not True:
            raise NodeError("module rejected the operation")
        value = value.get("value")
    if value is False:
        raise NodeError("module returned false")
    return value


@contextlib.contextmanager
def data_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise NodeError("data directory must be owned by you and have mode 0700")
    with (root / ".launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NodeError("another launcher owns this data directory") from None
        yield


def check_ports(ports):
    endpoints = [("blockchain", socket.SOCK_DGRAM), ("blend", socket.SOCK_DGRAM),
                 ("api", socket.SOCK_STREAM), ("storage", socket.SOCK_STREAM),
                 ("delivery", socket.SOCK_STREAM), ("delivery", socket.SOCK_DGRAM),
                 ("discovery", socket.SOCK_DGRAM)]
    with contextlib.ExitStack() as stack:
        for name, kind in endpoints:
            sock = stack.enter_context(socket.socket(socket.AF_INET, kind))
            if kind == socket.SOCK_STREAM:
                # Allow restart while closed TCP connections remain in TIME_WAIT.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", ports[name]))
            except OSError:
                raise NodeError(f"{name} port {ports[name]} is unavailable") from None


class Node:
    def __init__(self, root, ctl, modules, timeout=300):
        self.root = root
        self.ctl = ctl
        self.modules = modules
        self.timeout = timeout
        self.process = None
        self.deadline = None
        self.env = dict(os.environ, LOGOSCTL_CONFIG_DIR=str(root / "session"),
                        LOGOS_STORAGE_HOME=str(root / "storage-config"))

    def command_timeout(self, limit=25):
        if self.deadline is None:
            return limit
        return min(limit, max(0.1, self.deadline - time.monotonic()))

    def rpc(self, *args, timeout=25):
        try:
            proc = subprocess.run([self.ctl, *args], env=self.env, cwd=self.root,
                                  capture_output=True, text=True, timeout=self.command_timeout(timeout))
        except subprocess.TimeoutExpired:
            raise NodeError(f"logosctl {' '.join(args[:3])} timed out") from None
        # CLI output may contain keys/configuration; never echo it on failure.
        try:
            doc = json.loads(proc.stdout)
        except ValueError:
            doc = None
        if proc.returncode:
            error = doc.get("error") if isinstance(doc, dict) else None
            if (len(args) >= 3 and args[0] == "call" and isinstance(error, dict)
                    and doc.get("status") == "error" and doc.get("code") == "METHOD_FAILED"
                    and error.get("code") == "timeout" and error.get("origin") == args[1]):
                raise MethodTimeout(f"logosctl {' '.join(args[:3])} timed out")
            raise NodeError(f"logosctl {' '.join(args[:3])} failed")
        try:
            return result_value(doc)
        except (ValueError, NodeError):
            raise NodeError(f"logosctl {' '.join(args[:3])} returned an error") from None

    def wait_for(self, label, predicate, deadline=None):
        self.deadline = time.monotonic() + self.timeout if deadline is None else deadline
        last_error = None
        try:
            while time.monotonic() < self.deadline:
                if self.process is not None and self.process.poll() is not None:
                    raise NodeError("daemon exited during startup; inspect session/logs/")
                try:
                    if predicate():
                        return
                except NodeError as error:
                    last_error = str(error)
                except subprocess.TimeoutExpired:
                    last_error = "logosctl readiness check timed out"
                time.sleep(min(2, max(0, self.deadline - time.monotonic())))
            detail = f": {last_error}" if last_error else ""
            hint = "; inspect session/logs/" if self.process is not None else ""
            raise NodeError(f"{label} did not become ready{detail}{hint}")
        finally:
            self.deadline = None

    def settings(self, overrides, public_ip):
        path = self.root / "node.json"
        if path.exists():
            settings = read_json(path)
            if not isinstance(settings, dict) or settings.get("schema") != SCHEMA:
                raise NodeError("unsupported data-directory schema")
            ports = settings.get("ports")
            if not isinstance(ports, dict) or set(ports) != set(PORTS) or any(
                type(v) is not int or not 1 <= v <= 65535 for v in ports.values()
            ):
                raise NodeError("invalid saved port settings")
            if any(ports.get(k) != v for k, v in overrides.items()):
                raise NodeError("port options differ from the existing configuration")
            if public_ip and settings.get("public_ip") != public_ip:
                raise NodeError("public IP differs from the existing configuration")
            if settings.get("public_ip") is not None:
                try:
                    ipaddress.IPv4Address(settings["public_ip"])
                except (ValueError, TypeError):
                    raise NodeError("invalid saved public IP") from None
            return settings
        if any(p.name != ".launcher.lock" for p in self.root.iterdir()):
            raise NodeError("refusing to initialize a nonempty, unmanaged directory")
        settings = dict(schema=SCHEMA, ports=PORTS | overrides, public_ip=public_ip)
        write_json(path, settings)
        return settings

    def initialize(self, settings):
        marker = self.root / ".initializing"
        complete = self.root / ".initialized"
        expected = [self.root / "blockchain.yaml", self.root / "storage.json",
                    self.root / "delivery.json", self.root / "keystore.yaml"]
        if marker.exists():
            raise NodeError("initialization was interrupted; preserve and inspect this directory before retrying")
        if complete.exists():
            if not all(p.is_file() and p.stat().st_size for p in expected):
                raise NodeError("configuration is missing; restore a backup, do not regenerate keys")
            return
        if any(p.exists() for p in expected):
            raise NodeError("unmarked configuration exists; refusing to replace it")
        marker.touch(mode=0o600)
        ports = settings["ports"]
        for name in ("blockchain", "storage", "logs"):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        peers = read_json(CONFIG / "blockchain-peers.json")
        args = peers | dict(output=str(expected[0]), net_port=ports["blockchain"],
                            blend_port=ports["blend"], http_addr=f"127.0.0.1:{ports['api']}",
                            state_path=str(self.root / "blockchain/state"),
                            storage_path=str(self.root / "blockchain/storage"),
                            logs_path=str(self.root / "logs"), kms_file=str(expected[3]))
        write_json(self.root / "blockchain-init.json", args)
        self.rpc("call", "blockchain_module", "generate_user_config",
                 "@" + str(self.root / "blockchain-init.json"))
        for path in (expected[0], expected[3]):
            if not path.is_file() or not path.stat().st_size:
                raise NodeError("Blockchain did not write its configuration and keystore")
            path.chmod(0o600)
        storage = self.rpc("call", "storage_module", "loadConfigOrDefault")
        if isinstance(storage, str):
            storage = json.loads(storage)
        if not isinstance(storage, dict) or not storage:
            raise NodeError("Storage returned invalid default configuration")
        storage.update({"data-dir": str(self.root / "storage"), "listen-port": ports["storage"]})
        write_json(expected[1], storage)
        # Delivery's nodekey is a secp256k1 scalar encoded as 64 hex digits.
        order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
        kernel = dict(preset="logos.test", relay=True, logLevel="INFO",
                      tcpPort=ports["delivery"], discv5UdpPort=ports["discovery"],
                      discv5Discovery=True, nodekey=f"{secrets.randbelow(order - 1) + 1:064x}",
                      nat="extip:" + settings["public_ip"] if settings["public_ip"] else "any")
        write_json(expected[2], dict(entryLayer="kernel", kernelConf=kernel))
        os.replace(marker, complete)

    def start(self, settings):
        if (self.root / ".initializing").exists():
            raise NodeError("initialization was interrupted; preserve and inspect the data directory")
        # Check the session independently of our lock: a manually started daemon
        # may be using it without having acquired the launcher lock.
        try:
            status = subprocess.run([self.ctl, "daemon", "status"], env=self.env,
                                    cwd=self.root, capture_output=True, timeout=25)
        except subprocess.TimeoutExpired:
            raise NodeError("logosctl daemon status timed out; cannot establish whether this session is unused") from None
        if status.returncode == 0:
            raise NodeError("a daemon is already using this session")
        try:
            state = json.loads(status.stdout).get("daemon", {}).get("status")
        except (ValueError, AttributeError):
            state = None
        if state not in ("not_running", "not_configured"):
            raise NodeError("cannot establish whether this session is unused")
        self.wait_for("network ports", lambda: check_ports(settings["ports"]) is None)
        write_json(self.root / "daemon.json", {"modules_dirs": [self.modules],
                   "persistence_path": str(self.root / "module-data")})
        self.rpc("daemon", "config", "set", str(self.root / "daemon.json"))
        self.process = subprocess.Popen([self.ctl, "daemon", "start"],
                                        env=self.env, cwd=self.root, start_new_session=True)
        self.wait_for("daemon", lambda: self.daemon_ready())
        # The downloader auto-initializes Storage; release it before configuring our node.
        self.rpc("module", "unload", "package_downloader")
        self.rpc("module", "unload", "storage_module")
        for module in MODULES:
            self.rpc("module", "load", module)
        self.initialize(settings)
        self.prepare_blockchain()
        self.start_blockchain()
        print("Blockchain started; synchronization continues in the background.", flush=True)
        self.rpc("call", "storage_module", "init", "@" + str(self.root / "storage.json"))
        self.rpc("call", "storage_module", "start")
        self.wait_for("Storage", lambda: self.rpc("call", "storage_module", "isRunning") is True)
        self.rpc("module", "load", "package_downloader")
        self.rpc("call", "delivery_module", "createNode", "@" + str(self.root / "delivery.json"))
        self.rpc("call", "delivery_module", "start")
        self.wait_for("Delivery", lambda: self.rpc("call", "delivery_module", "getNodeInfo", "IsRunning") in (True, "true"))
        print("Storage and Delivery running. Ctrl+C stops this node.", flush=True)

    def prepare_blockchain(self):
        # Preserve the saved configuration and YAML tags; override only the
        # mining pool in a private runtime copy, never the whole node's CPU use.
        yaml = YAML()
        yaml.preserve_quotes = True
        try:
            config = yaml.load((self.root / "blockchain.yaml").read_text())
            config.setdefault("pow", {}).setdefault("mining", {})["max_threads"] = 1
            output = io.StringIO()
            yaml.dump(config, output)
        except (YAMLError, AttributeError, TypeError):
            raise NodeError("invalid Blockchain configuration; preserve and inspect blockchain.yaml") from None
        write_text(self.root / "blockchain-runtime.yaml", output.getvalue())

    def api(self, port, path):
        # Readiness requests must not block the module's RPC thread. Ignore
        # proxy environment variables for the node's loopback-only API.
        http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with http.open(f"http://127.0.0.1:{port}{path}", timeout=self.command_timeout(5)) as response:
            return json.load(response)

    def mining_status(self, port):
        try:
            return self.api(port, "/pow/status")
        except (OSError, ValueError):
            return None

    def start_mining(self, port):
        print("Mining waits for chain, wallet and reward readiness.", flush=True)
        while self.process.poll() is None:
            try:
                chain = self.api(port, "/cryptarchia/info")
            except (OSError, ValueError):
                chain = None
            info = chain.get("cryptarchia_info") if isinstance(chain, dict) else None
            if isinstance(info, dict) and info.get("state") == "Online":
                status = self.mining_status(port)
                auto = status.get("auto_claim", {}) if isinstance(status, dict) else {}
                targets = auto.get("targets", []) if isinstance(auto, dict) else []
                if isinstance(status, dict) and status.get("are_rewards_enabled") is False:
                    print("Network rewards are disabled; skipping automatic mining.", flush=True)
                    return
                if isinstance(targets, list) and targets and all(isinstance(t, dict) and type(t.get("balance")) is int
                                   and type(t.get("threshold")) is int for t in targets):
                    if all(t["balance"] >= t["threshold"] for t in targets):
                        print("Reward targets are funded; skipping automatic mining.", flush=True)
                        return
                    if status.get("are_rewards_enabled") is True and auto.get("is_armed") is True:
                        if status.get("is_mining") is not True:
                            # Never retry a timed-out start: the module may still
                            # be handling it. Confirmation uses the HTTP API.
                            self.rpc("call", "blockchain_module", "pow_start_mining")
                        self.wait_for("mining", lambda: self.mining_confirmed(port))
                        print("Mining running with one search thread.", flush=True)
                        return
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def mining_confirmed(self, port):
        status = self.mining_status(port)
        return isinstance(status, dict) and status.get("is_mining") is True

    def start_blockchain(self):
        deadline = time.monotonic() + self.timeout
        self.deadline = deadline
        try:
            try:
                self.rpc("call", "blockchain_module", "start", str(self.root / "blockchain-runtime.yaml"), "")
            except MethodTimeout:
                # Recovery continues after the RPC deadline; never send start twice.
                pass
            self.wait_for("Blockchain API", self.blockchain_ready, deadline=deadline)
        finally:
            self.deadline = None

    def blockchain_ready(self):
        value = self.rpc("call", "blockchain_module", "get_cryptarchia_info")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return False
        return isinstance(value, dict) and bool(value)

    def daemon_ready(self):
        proc = subprocess.run([self.ctl, "daemon", "status"], env=self.env,
                              cwd=self.root, capture_output=True, timeout=self.command_timeout())
        return proc.returncode == 0

    def stop(self):
        if self.process is None:
            return
        # Signal only our process group, including module children. Never use
        # stale PID files or a broad process-name kill to stop a node.
        for sig, timeout in ((signal.SIGTERM, 30), (signal.SIGKILL, 5)):
            deadline = time.monotonic() + timeout
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                break
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
            # A daemon can exit before its modules finish flushing state.
            while time.monotonic() < deadline:
                try:
                    os.killpg(self.process.pid, 0)
                except ProcessLookupError:
                    self.process = None
                    return
                time.sleep(0.1)
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            print("logos-node: shutdown incomplete; owned processes remain after SIGKILL", file=sys.stderr)
        self.process = None


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    default = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "logos-node"
    p.add_argument("--data-dir", type=Path, default=os.environ.get("LOGOS_NODE_DATA_DIR") or default)
    p.add_argument("--public-ip", type=lambda v: str(ipaddress.IPv4Address(v)))
    for name in PORTS:
        p.add_argument(f"--{name}-port", type=int)
    p.add_argument("--startup-timeout", type=int, default=300)
    p.add_argument("command", nargs="?", choices=("run", "ctl"), default="run")
    p.add_argument("args", nargs=argparse.REMAINDER)
    return p


def main(argv=None, control=False):
    if control:
        argv = list(sys.argv[1:] if argv is None else argv)
        prefix = []
        if argv and argv[0] == "--data-dir":
            if len(argv) < 2:
                raise NodeError("--data-dir requires a path")
            prefix, argv = argv[:2], argv[2:]
        elif argv and argv[0].startswith("--data-dir="):
            prefix, argv = argv[:1], argv[1:]
        argv = prefix + ["ctl"] + argv
    args = parser().parse_args(argv)
    if args.startup_timeout < 1:
        parser().error("startup timeout must be positive")
    overrides = {k: getattr(args, k + "_port") for k in PORTS if getattr(args, k + "_port") is not None}
    if any(not 1 <= v <= 65535 for v in overrides.values()):
        parser().error("ports must be between 1 and 65535")
    if args.command == "run" and args.args:
        parser().error("unexpected arguments after run")
    ctl = os.environ.get("LOGOS_NODE_CTL")
    modules = os.environ.get("LOGOS_NODE_MODULES")
    if not ctl or not modules:
        raise NodeError("run the packaged launcher with nix run")
    root = args.data_dir.expanduser().resolve()
    if root.is_relative_to(Path("/nix/store")):
        raise NodeError("data directory must be outside /nix/store")
    os.umask(0o077)
    node = Node(root, ctl, modules, args.startup_timeout)
    if args.command == "ctl":
        if not (root / "node.json").is_file():
            raise NodeError("no managed node exists in this data directory")
        return subprocess.run([ctl, *args.args], env=node.env, cwd=root).returncode
    with data_lock(root):
        settings = node.settings(overrides, args.public_ip)
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        signals = [signal.SIGINT, signal.SIGTERM]
        if signal.getsignal(signal.SIGHUP) != signal.SIG_IGN:
            signals.append(signal.SIGHUP)
        previous = {sig: signal.signal(sig, interrupted) for sig in signals}
        try:
            node.start(settings)
            node.start_mining(settings["ports"]["api"])
            code = node.process.wait()
            if code == 0:
                return 0
            raise NodeError(f"daemon exited unexpectedly (status {code}); inspect session/logs/")
        except KeyboardInterrupt:
            if not (root / ".initialized").is_file():
                raise NodeError("initialization interrupted; preserve and inspect the data directory before retrying") from None
            return 0
        finally:
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
            try:
                node.stop()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def entrypoint(control=False):
    try:
        sys.exit(main(control=control))
    except (NodeError, OSError, ValueError, subprocess.TimeoutExpired):
        # Tracebacks and upstream error payloads can contain secret arguments.
        error = sys.exc_info()[1]
        program = "logosctl" if control else "logos-node"
        print(f"{program}: {error}" if isinstance(error, NodeError)
              else f"{program}: local configuration or process operation failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
