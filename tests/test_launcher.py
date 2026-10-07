import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'src/logos_node.py'
spec = importlib.util.spec_from_file_location('logos_node', SOURCE)
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.n = node.Node(self.root, 'mock-logosctl', '/mock/modules', timeout=1)

    def test_storage_configuration_is_isolated_without_changing_home(self):
        self.assertEqual(self.n.env["LOGOS_STORAGE_HOME"], str(self.root / "storage-config"))
        self.assertEqual(self.n.env.get("HOME"), os.environ.get("HOME"))

    def test_control_selects_managed_session_and_preserves_arguments(self):
        node.write_json(self.root / 'node.json', {'schema': 1})
        env = {'LOGOS_NODE_CTL': '/pinned/logosctl', 'LOGOS_NODE_MODULES': '/pinned/modules',
               'LOGOS_NODE_DATA_DIR': str(self.root)}
        command = ['call', 'example', 'method', '--data-dir', 'opaque-value']
        response = subprocess.CompletedProcess([], 7)
        with patch.dict(os.environ, env), patch.object(node.subprocess, 'run', return_value=response) as run:
            self.assertEqual(node.main(command, control=True), 7)
        self.assertEqual(run.call_args.args[0], ['/pinned/logosctl', *command])
        self.assertEqual(run.call_args.kwargs['cwd'], self.root)
        self.assertEqual(run.call_args.kwargs['env']['LOGOSCTL_CONFIG_DIR'], str(self.root / 'session'))
        self.assertEqual(run.call_args.kwargs['env']['LOGOS_STORAGE_HOME'], str(self.root / 'storage-config'))

    def test_control_explicit_directory_overrides_environment(self):
        node.write_json(self.root / 'node.json', {'schema': 1})
        env = {'LOGOS_NODE_CTL': 'pinned-ctl', 'LOGOS_NODE_MODULES': '/modules',
               'LOGOS_NODE_DATA_DIR': str(self.root / 'unused')}
        for prefix in (['--data-dir', str(self.root)], ['--data-dir=' + str(self.root)]):
            with self.subTest(prefix=prefix), patch.dict(os.environ, env), \
                    patch.object(node.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(node.main([*prefix, 'daemon', 'status'], control=True), 0)
                self.assertEqual(run.call_args.kwargs['cwd'], self.root)

    def test_control_refuses_unmanaged_directory_without_starting(self):
        env = {'LOGOS_NODE_CTL': 'pinned-ctl', 'LOGOS_NODE_MODULES': '/modules',
               'LOGOS_NODE_DATA_DIR': str(self.root / 'absent')}
        with patch.dict(os.environ, env), patch.object(node.subprocess, 'run') as run:
            with self.assertRaisesRegex(node.NodeError, 'no managed node'):
                node.main(['daemon', 'status'], control=True)
        run.assert_not_called()
        self.assertFalse((self.root / 'absent').exists())

    def test_control_missing_directory_argument_is_rejected(self):
        with self.assertRaisesRegex(node.NodeError, 'requires a path'):
            node.main(['--data-dir'], control=True)

    def test_clean_daemon_exit_succeeds_and_failures_still_cleanup(self):
        env = {'LOGOS_NODE_CTL': 'pinned-ctl', 'LOGOS_NODE_MODULES': '/modules'}
        for code in (0, 7, -signal.SIGKILL):
            with self.subTest(code=code), patch.dict(os.environ, env), \
                    patch.object(node, 'Node') as factory:
                owner = factory.return_value
                owner.settings.return_value = {'ports': node.PORTS}
                owner.process.wait.return_value = code
                if code == 0:
                    self.assertEqual(node.main(['--data-dir', str(self.root)]), 0)
                else:
                    with self.assertRaisesRegex(node.NodeError, f'status {code}'):
                        node.main(['--data-dir', str(self.root)])
                owner.start_mining.assert_called_once_with(node.PORTS['api'])
                owner.stop.assert_called_once()
        with node.data_lock(self.root):
            pass

    def test_mining_override_preserves_saved_yaml_and_tags(self):
        original = "wallet: {key: !Ed25519 '012345', label: 'on'}\npow: {mining: {max_threads: 8, max_tickets_per_block: 4}}\n"
        (self.root / 'blockchain.yaml').write_text(original)
        self.n.prepare_blockchain()
        self.assertEqual((self.root / 'blockchain.yaml').read_text(), original)
        runtime = self.root / 'blockchain-runtime.yaml'
        config = node.YAML().load(runtime.read_text())
        self.assertEqual(config['pow']['mining']['max_threads'], 1)
        self.assertEqual(config['pow']['mining']['max_tickets_per_block'], 4)
        self.assertEqual(str(config['wallet']['key'].tag), '!Ed25519')
        self.assertEqual(config['wallet']['label'], 'on')
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)
        self.n.prepare_blockchain()
        self.assertEqual((self.root / 'blockchain.yaml').read_text(), original)

    def test_invalid_mining_config_is_not_replaced_or_echoed(self):
        for original in ('pow: [secret]\n', 'pow: {mining: null}\n', 'private: [broken\n'):
            with self.subTest(original=original):
                (self.root / 'blockchain.yaml').write_text(original)
                with self.assertRaisesRegex(node.NodeError, '^invalid Blockchain configuration'):
                    self.n.prepare_blockchain()
                self.assertFalse((self.root / 'blockchain-runtime.yaml').exists())
                self.assertEqual((self.root / 'blockchain.yaml').read_text(), original)

    def pow_status(self, balance=0, mining=False, armed=True, rewards=True):
        return dict(is_mining=mining, are_rewards_enabled=rewards,
                    auto_claim=dict(is_armed=armed, targets=[dict(balance=balance, threshold=10)]))

    def test_mining_waits_for_online_wallet_then_starts_once(self):
        self.n.process = Mock()
        self.n.process.poll.return_value = None
        self.n.process.wait.side_effect = subprocess.TimeoutExpired('daemon', 10)
        responses = [dict(cryptarchia_info=dict(state='Bootstrapping')),
                     dict(cryptarchia_info=dict(state='Online')), self.pow_status(balance=None),
                     dict(cryptarchia_info=dict(state='Online')), self.pow_status(),
                     self.pow_status(mining=True)]
        with patch.object(self.n, 'api', side_effect=responses), patch.object(self.n, 'rpc') as rpc, \
                patch('sys.stdout', new_callable=io.StringIO):
            self.n.start_mining(8080)
        rpc.assert_called_once_with('call', 'blockchain_module', 'pow_start_mining')
        self.assertEqual(self.n.process.wait.call_count, 2)

    def test_mining_does_not_start_for_funded_or_disabled_rewards(self):
        self.n.process = Mock()
        self.n.process.poll.return_value = None
        for status in (self.pow_status(balance=10, armed=False), self.pow_status(rewards=False)):
            with self.subTest(status=status), patch.object(self.n, 'api', side_effect=[
                    dict(cryptarchia_info=dict(state='Online')), status]), \
                    patch.object(self.n, 'rpc') as rpc, patch('sys.stdout', new_callable=io.StringIO):
                self.n.start_mining(8080)
            rpc.assert_not_called()

    def test_already_mining_is_confirmed_without_another_start(self):
        self.n.process = Mock()
        self.n.process.poll.return_value = None
        with patch.object(self.n, 'api', side_effect=[dict(cryptarchia_info=dict(state='Online')),
                self.pow_status(mining=True), self.pow_status(mining=True)]), \
                patch.object(self.n, 'rpc') as rpc, patch('sys.stdout', new_callable=io.StringIO):
            self.n.start_mining(8080)
        rpc.assert_not_called()

    def test_mining_start_timeout_is_never_retried(self):
        self.n.process = Mock()
        self.n.process.poll.return_value = None
        with patch.object(self.n, 'api', side_effect=[dict(cryptarchia_info=dict(state='Online')),
                self.pow_status()]), patch.object(self.n, 'rpc', side_effect=node.MethodTimeout('timed out')) as rpc, \
                patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaises(node.MethodTimeout):
                self.n.start_mining(8080)
        rpc.assert_called_once()

    def test_unavailable_mining_api_does_not_prevent_daemon_exit(self):
        self.n.process = Mock()
        self.n.process.poll.side_effect = [None, 1]
        with patch.object(self.n, 'api', side_effect=TimeoutError), patch.object(self.n, 'rpc') as rpc, \
                patch('sys.stdout', new_callable=io.StringIO):
            self.n.start_mining(8080)
        rpc.assert_not_called()

    def test_invalid_saved_settings_are_rejected(self):
        node.write_json(self.root / "node.json", {"schema": 1, "ports": {}})
        with self.assertRaisesRegex(node.NodeError, "invalid saved"):
            self.n.settings({}, None)

    def test_nested_failure_and_false_are_not_success(self):
        for result in (False, {'success': False, 'value': 'ignored'}):
            with self.assertRaises(node.NodeError):
                node.result_value({'status': 'ok', 'result': result})
        self.assertEqual(node.result_value({'status': 'ok', 'result': {'success': True, 'value': 'true'}}), 'true')

    def test_lock_prevents_second_launcher(self):
        with node.data_lock(self.root):
            with self.assertRaises(node.NodeError):
                with node.data_lock(self.root):
                    self.fail('second launcher acquired lock')

    def test_unmanaged_data_is_preserved(self):
        secret = self.root / 'wallet'
        secret.write_text('existing-identity')
        with self.assertRaises(node.NodeError):
            self.n.settings({}, None)
        self.assertEqual(secret.read_text(), 'existing-identity')

    def test_settings_preserved_and_changed_options_rejected(self):
        expected = self.n.settings({'storage': 18091}, None)
        before = (self.root / 'node.json').read_bytes()
        self.assertEqual(self.n.settings({}, None), expected)
        self.assertEqual((self.root / 'node.json').read_bytes(), before)
        with self.assertRaises(node.NodeError):
            self.n.settings({'storage': 8091}, None)

    def initialize(self):
        settings = self.n.settings({}, None)
        def rpc(*args):
            if args[2] == 'generate_user_config':
                (self.root / 'blockchain.yaml').write_text('wallet: generated-once\n')
                (self.root / 'keystore.yaml').write_text('key: generated-once\n')
                return None
            if args[2] == 'loadConfigOrDefault':
                return json.dumps({'mix-enabled': True, 'data-dir': 'default'})
            self.fail('unexpected RPC')
        with patch.object(self.n, 'rpc', side_effect=rpc):
            self.n.initialize(settings)
        return settings

    def test_restart_preserves_configs_and_delivery_identity(self):
        settings = self.initialize()
        paths = [self.root / f for f in ('blockchain.yaml', 'storage.json', 'delivery.json', 'keystore.yaml')]
        before = [p.read_bytes() for p in paths]
        with patch.object(self.n, 'rpc', side_effect=AssertionError('regenerated keys')):
            self.n.initialize(settings)
        self.assertEqual(before, [p.read_bytes() for p in paths])
        for p in paths:
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        delivery = node.read_json(paths[2])
        self.assertEqual(len(delivery['kernelConf']['nodekey']), 64)
        self.assertEqual(node.read_json(paths[1])['data-dir'], str(self.root / 'storage'))

    def test_partial_initialization_does_not_retry_key_generation(self):
        settings = self.n.settings({}, None)
        with patch.object(self.n, 'rpc', side_effect=node.NodeError('failed')):
            with self.assertRaises(node.NodeError):
                self.n.initialize(settings)
        with patch.object(self.n, 'rpc', side_effect=AssertionError('regenerated keys')):
            with self.assertRaisesRegex(node.NodeError, 'interrupted'):
                self.n.initialize(settings)

    def test_missing_config_after_initialization_is_not_regenerated(self):
        settings = self.initialize()
        (self.root / 'delivery.json').unlink()
        with patch.object(self.n, 'rpc', side_effect=AssertionError('regenerated keys')):
            with self.assertRaisesRegex(node.NodeError, 'missing'):
                self.n.initialize(settings)

    def test_missing_keystore_prevents_restart(self):
        settings = self.initialize()
        (self.root / "keystore.yaml").unlink()
        with self.assertRaisesRegex(node.NodeError, "missing"):
            self.n.initialize(settings)

    def test_interruption_at_completion_preserves_state(self):
        replace = os.replace
        for promoted in (False, True):
            with self.subTest(promoted=promoted), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                self.n = node.Node(self.root, 'mock-logosctl', '/mock/modules')
                def interrupt(source, target):
                    if Path(target).name != '.initialized':
                        return replace(source, target)
                    if promoted:
                        replace(source, target)
                    raise KeyboardInterrupt
                with patch.object(node.os, 'replace', side_effect=interrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.initialize()
                files = ['blockchain.yaml', 'keystore.yaml', 'storage.json', 'delivery.json']
                before = {name: (self.root / name).read_bytes() for name in files}
                self.assertEqual((self.root / '.initialized').exists(), promoted)
                self.assertEqual((self.root / '.initializing').exists(), not promoted)
                with patch.object(self.n, 'rpc', side_effect=AssertionError('regenerated keys')):
                    if promoted:
                        self.n.initialize(self.n.settings({}, None))
                    else:
                        with self.assertRaisesRegex(node.NodeError, 'interrupted'):
                            self.n.initialize(self.n.settings({}, None))
                self.assertEqual(before, {name: (self.root / name).read_bytes() for name in files})

    def test_port_timeout_retains_conflict_without_daemon_log_hint(self):
        with patch.object(node.time, 'monotonic', side_effect=[0, 0, 0, 2]), \
                patch.object(node.time, 'sleep'):
            with self.assertRaisesRegex(node.NodeError, 'storage port 8091 is unavailable') as error:
                self.n.wait_for('network ports', Mock(side_effect=node.NodeError('storage port 8091 is unavailable')))
        self.assertNotIn('session/logs/', str(error.exception))
        self.assertIsNone(self.n.deadline)

    def test_preflight_timeout_is_specific_and_does_not_start_daemon(self):
        settings = self.n.settings({}, None)
        with patch.object(node.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private argument', 25)):
            with self.assertRaisesRegex(node.NodeError, 'logosctl daemon status timed out') as error:
                self.n.start(settings)
        self.assertNotIn('private argument', str(error.exception))
        self.assertNotIn('session/logs/', str(error.exception))
        self.assertIsNone(self.n.process)

    def test_shutdown_warns_if_owned_processes_survive(self):
        self.n.process = Mock(pid=123)
        self.n.process.wait.side_effect = subprocess.TimeoutExpired('mock', 1)
        with patch.object(node.os, 'killpg'), patch.object(node.sys, 'stderr', new_callable=io.StringIO) as err:
            self.n.stop()
        self.assertIn('shutdown incomplete', err.getvalue())
        self.assertIsNone(self.n.process)

    def test_shutdown_of_exited_group_does_not_warn(self):
        self.n.process = Mock(pid=123)
        with patch.object(node.os, 'killpg', side_effect=ProcessLookupError), \
                patch.object(node.sys, 'stderr', new_callable=io.StringIO) as err:
            self.n.stop()
        self.assertEqual(err.getvalue(), '')

    def test_occupied_port_is_detected_before_start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            ports = dict(node.PORTS, api=sock.getsockname()[1])
            with self.assertRaises(node.NodeError):
                node.check_ports(ports)

    def test_listening_tcp_port_with_reuse_enabled_is_rejected(self):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', 0))
            sock.listen()
            ports = dict.fromkeys(node.PORTS, 0)
            ports['storage'] = sock.getsockname()[1]
            with self.assertRaisesRegex(node.NodeError, 'storage port'):
                node.check_ports(ports)

    def test_closed_tcp_connection_does_not_prevent_restart(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            server.listen()
            port = server.getsockname()[1]
            with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
                connection, _ = server.accept()
                connection.close()
                self.assertEqual(client.recv(1), b'')
        ports = dict.fromkeys(node.PORTS, 0)
        ports['storage'] = port
        node.check_ports(ports)

    def test_timeout_and_dead_daemon_fail_readiness(self):
        self.n.timeout = 0
        with self.assertRaisesRegex(node.NodeError, 'did not become ready'):
            self.n.wait_for('Storage', lambda: False)
        self.n.timeout = 1
        with patch.object(self.n, 'process') as proc:
            proc.poll.return_value = 7
            with self.assertRaisesRegex(node.NodeError, 'exited'):
                self.n.wait_for('Storage', lambda: True)

    def test_unknown_session_status_does_not_start_or_reconfigure(self):
        settings = self.n.settings({}, None)
        response = subprocess.CompletedProcess([], 1, b'{"status":"error"}', b"")
        with patch.object(node.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(node.NodeError, "cannot establish"):
                self.n.start(settings)
        self.assertIsNone(self.n.process)
        self.assertFalse((self.root / "daemon.json").exists())

    def test_transient_readiness_timeout_can_recover(self):
        with patch("time.sleep"), patch.object(self.n, "daemon_ready", side_effect=[
            subprocess.TimeoutExpired("fake", 1), True
        ]):
            self.n.wait_for("daemon", self.n.daemon_ready)
        self.assertIsNone(self.n.deadline)

    def test_rpc_errors_do_not_expose_output_or_config_argument(self):
        response = subprocess.CompletedProcess([], 1, 'private material', 'private material')
        with patch.object(node.subprocess, 'run', return_value=response):
            with self.assertRaises(node.NodeError) as err:
                self.n.rpc('call', 'delivery_module', 'createNode', 'private material')
        self.assertNotIn('private material', str(err.exception))

    def test_only_structured_matching_method_timeouts_are_classified(self):
        timeout = {'status': 'error', 'code': 'METHOD_FAILED',
                   'error': {'code': 'timeout', 'origin': 'blockchain_module',
                             'message': 'private material'}}
        cases = [
            (timeout, node.MethodTimeout),
            (dict(timeout, error={'code': 'failed', 'origin': 'blockchain_module'}), node.NodeError),
            (dict(timeout, error={'code': 'timeout', 'origin': 'storage_module'}), node.NodeError),
            (dict(timeout, error=None, message='timeout: private material'), node.NodeError),
            (dict(timeout, status='ok'), node.NodeError),
            (dict(timeout, code='OTHER_ERROR'), node.NodeError),
        ]
        for doc, error_type in cases:
            with self.subTest(doc=doc):
                response = subprocess.CompletedProcess([], 4, json.dumps(doc), 'private material')
                with patch.object(node.subprocess, 'run', return_value=response):
                    with self.assertRaises(node.NodeError) as err:
                        self.n.rpc('call', 'blockchain_module', 'start', 'private material')
                self.assertIs(type(err.exception), error_type)
                self.assertNotIn('private material', str(err.exception))
        with patch.object(node.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ctl', 25)):
            with self.assertRaises(node.NodeError) as err:
                self.n.rpc('call', 'blockchain_module', 'start')
        self.assertIs(type(err.exception), node.NodeError)

    def test_blockchain_start_and_readiness_share_deadline(self):
        self.n.timeout = 30
        with patch.object(node.time, 'monotonic', return_value=100), \
                patch.object(self.n, 'rpc', side_effect=node.MethodTimeout('timed out')) as rpc, \
                patch.object(self.n, 'wait_for') as wait:
            self.n.start_blockchain()
        rpc.assert_called_once()
        wait.assert_called_once_with('Blockchain API', self.n.blockchain_ready, deadline=130)
        self.assertIsNone(self.n.deadline)

    def test_blockchain_real_start_failure_does_not_poll_readiness(self):
        with patch.object(self.n, 'rpc', side_effect=node.NodeError('rejected')) as rpc, \
                patch.object(self.n, 'wait_for') as wait:
            with self.assertRaisesRegex(node.NodeError, 'rejected'):
                self.n.start_blockchain()
        rpc.assert_called_once()
        wait.assert_not_called()
        self.assertIsNone(self.n.deadline)

    def test_expired_start_deadline_does_not_poll_or_extend_wait(self):
        with patch.object(node.time, 'monotonic', return_value=130), \
                patch.object(self.n, 'blockchain_ready') as ready:
            with self.assertRaisesRegex(node.NodeError, 'did not become ready'):
                self.n.wait_for('Blockchain API', self.n.blockchain_ready, deadline=130)
        ready.assert_not_called()
        self.assertIsNone(self.n.deadline)


class ProcessTests(unittest.TestCase):
    """Exercise subprocess ownership with a fake CLI; no network node starts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'state'
        self.ctl = self.base / 'ctl'
        self.ctl.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, signal, sys, time
root = pathlib.Path.cwd()
a = sys.argv[1:]
alive = root / 'alive'
downloader = root / 'downloader-loaded'
storage_config = root / 'storage-in-use'
if a == ['daemon', 'start']:
    downloader.touch()
    storage_config.write_text('{"data-dir":"default"}')
    alive.write_text(str(os.getpid()))
    def stop(*unused):
        alive.unlink(missing_ok=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    print('mock daemon stdout', flush=True)
    print('mock daemon stderr', file=sys.stderr, flush=True)
    while True: time.sleep(0.02)
if a == ['daemon', 'status']:
    print(json.dumps({'daemon': {'status': 'running' if alive.exists() else 'not_running'}}))
    sys.exit(0 if alive.exists() else 1)
if a == ['daemon', 'stop']:
    os.kill(int(alive.read_text()), signal.SIGTERM)
    sys.exit(0)
result = True
if a == ['module', 'unload', 'package_downloader']:
    downloader.unlink(missing_ok=True)
if a == ['module', 'unload', 'storage_module']:
    storage_config.unlink(missing_ok=True)
if a == ['module', 'load', 'package_downloader']:
    downloader.touch()
if a[:2] == ['module', 'load'] and downloader.exists() and not storage_config.exists():
    storage_config.write_text('{"data-dir":"default"}')
if a[:3] == ['call', 'storage_module', 'init']:
    if storage_config.exists():
        result = False
    else:
        storage_config.write_text(pathlib.Path(a[3][1:]).read_text())
if a[:3] == ['call', 'blockchain_module', 'generate_user_config']:
    args = json.loads(pathlib.Path(a[3][1:]).read_text())
    pathlib.Path(args['output']).write_text('wallet: unique-identity\\n')
    pathlib.Path(args['kms_file']).write_text('key: unique-identity\\n')
    if os.getenv('PAUSE_INITIALIZATION'):
        (root / 'initialization-paused').touch()
        while True: time.sleep(0.02)
    result = {'success': True, 'value': None}
if a[:3] == ['call', 'blockchain_module', 'start']:
    with (root / 'start-count').open('a') as f: f.write('start\\n')
    if os.getenv('BLOCKCHAIN_START'):
        (root / 'chain-ready-at').write_text(str(time.monotonic() + 0.3))
        print(json.dumps({'status': 'error', 'code': 'METHOD_FAILED',
                          'error': {'code': os.environ['BLOCKCHAIN_START'],
                                    'origin': 'blockchain_module', 'message': 'private material'}}))
        sys.exit(4)
if a[:3] == ['call', 'blockchain_module', 'get_cryptarchia_info']:
    ready = root / 'chain-ready-at'
    if os.getenv('CHAIN_NEVER_READY') or (ready.exists() and time.monotonic() < float(ready.read_text())):
        print(json.dumps({'status': 'error'}))
        sys.exit(4)
    result = {'success': True, 'value': json.dumps({'mode': 'Bootstrapping'})}
if a[:3] == ['call', 'storage_module', 'loadConfigOrDefault']:
    result = {'success': True, 'value': json.dumps({'mix-enabled': True})}
if a[:3] == ['call', 'storage_module', 'start'] and os.getenv('FAIL_STORAGE'):
    result = False
if a[:3] == ['call', 'delivery_module', 'getNodeInfo']:
    (root / 'delivery-ready').touch()
    result = {'success': True, 'value': 'true'}
print(json.dumps({'status': 'ok', 'result': result}))
''')
        self.ctl.chmod(0o700)

    def launch(self, fail=False, extra_env=None, ignore_hup=False):
        env = dict(os.environ, LOGOS_NODE_CTL=str(self.ctl), LOGOS_NODE_MODULES='/mock/modules')
        env.update(extra_env or {})
        if fail:
            env['FAIL_STORAGE'] = '1'
        # Port preflight is exercised separately; allocate unused ports here.
        sockets = []
        ports = []
        for _ in node.PORTS:
            s = socket.socket()
            s.bind(('127.0.0.1', 0))
            sockets.append(s)
            ports.append(s.getsockname()[1])
        for s in sockets:
            s.close()
        args = [sys.executable, str(SOURCE), '--data-dir', str(self.root), '--startup-timeout', '5']
        for name, port in zip(node.PORTS, ports):
            args.extend([f'--{name}-port', str(port)])
        if ignore_hup:
            args = [sys.executable, '-c',
                    'import os, signal, sys; signal.signal(signal.SIGHUP, signal.SIG_IGN); '
                    'os.execv(sys.executable, [sys.executable, *sys.argv[1:]])', *args[1:]]
        proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_process, proc)
        return proc

    def cleanup_process(self, proc):
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

    def test_startup_failure_stops_owned_daemon(self):
        proc = self.launch(fail=True)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertFalse((self.root / 'alive').exists())
        self.assertIn('returned an error', err)

    def test_sigterm_stops_owned_daemon(self):
        proc = self.launch()
        deadline = time.monotonic() + 10
        while not (self.root / 'delivery-ready').exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                self.fail('mock startup did not initialize')
            time.sleep(0.02)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertFalse((self.root / 'alive').exists())
        self.assertEqual(node.read_json(self.root / 'storage-in-use'),
                         node.read_json(self.root / 'storage.json'))
        self.assertTrue((self.root / 'downloader-loaded').exists())

    def wait_for_file(self, proc, name):
        deadline = time.monotonic() + 10
        while not (self.root / name).exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                self.fail('mock startup did not reach ' + name)
            time.sleep(0.02)

    def test_sighup_stops_owned_daemon(self):
        proc = self.launch()
        self.wait_for_file(proc, 'delivery-ready')
        proc.send_signal(signal.SIGHUP)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertFalse((self.root / 'alive').exists())

    def test_control_stop_exits_successfully_while_waiting_for_mining(self):
        proc = self.launch()
        self.wait_for_file(proc, 'delivery-ready')
        env = dict(os.environ, LOGOS_NODE_CTL=str(self.ctl), LOGOS_NODE_MODULES='/mock/modules')
        stopped = subprocess.run([sys.executable, str(SOURCE.with_name('logosctl.py')),
                                  '--data-dir', str(self.root), 'daemon', 'stop'],
                                 env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertNotIn('daemon exited unexpectedly', err)
        self.assertIn('mock daemon stdout', out)
        self.assertIn('mock daemon stderr', err)
        self.assertFalse((self.root / 'daemon.log').exists())
        self.assertFalse((self.root / 'alive').exists())
        with node.data_lock(self.root):
            pass

    def test_daemon_sigkill_remains_an_error(self):
        proc = self.launch()
        self.wait_for_file(proc, 'delivery-ready')
        os.kill(int((self.root / 'alive').read_text()), signal.SIGKILL)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertIn('daemon exited unexpectedly (status -9)', err)
        self.assertIn('inspect session/logs/', err)

    def test_inherited_ignored_sighup_is_preserved(self):
        proc = self.launch(ignore_hup=True)
        self.wait_for_file(proc, 'delivery-ready')
        proc.send_signal(signal.SIGHUP)
        time.sleep(0.1)
        self.assertIsNone(proc.poll())
        proc.terminate()
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertFalse((self.root / 'alive').exists())

    def test_interrupted_initialization_reports_failure_and_preserves_keys(self):
        proc = self.launch(extra_env={'PAUSE_INITIALIZATION': '1'})
        self.wait_for_file(proc, 'initialization-paused')
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertIn('initialization interrupted', err)
        self.assertIn('preserve', err)
        self.assertTrue((self.root / '.initializing').exists())
        self.assertEqual((self.root / 'keystore.yaml').read_text(), 'key: unique-identity\n')
        self.assertFalse((self.root / 'alive').exists())

    def test_timed_out_start_recovers_without_repeating_start(self):
        proc = self.launch(extra_env={'BLOCKCHAIN_START': 'timeout'})
        deadline = time.monotonic() + 12
        while not (self.root / 'delivery-ready').exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                self.fail('timed-out start did not recover')
            time.sleep(0.02)
        proc.terminate()
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual((self.root / 'start-count').read_text(), 'start\n')
        self.assertFalse((self.root / 'alive').exists())
        self.assertNotIn('private material', out + err)

    def test_unready_chain_after_start_timeout_cleans_up(self):
        proc = self.launch(extra_env={'BLOCKCHAIN_START': 'timeout', 'CHAIN_NEVER_READY': '1'})
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertIn('Blockchain API did not become ready', err)
        self.assertEqual((self.root / 'start-count').read_text(), 'start\n')
        self.assertFalse((self.root / 'alive').exists())
        self.assertFalse((self.root / 'delivery-ready').exists())

    def test_rejected_blockchain_start_cleans_up(self):
        proc = self.launch(extra_env={'BLOCKCHAIN_START': 'rejected'})
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertNotIn('private material', out + err)
        self.assertEqual((self.root / 'start-count').read_text(), 'start\n')
        self.assertFalse((self.root / 'alive').exists())
        self.assertFalse((self.root / 'delivery-ready').exists())

    def test_modules_can_flush_after_daemon_exits(self):
        daemon = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                  process_group=0)
        self.addCleanup(self.cleanup_process, daemon)
        module = subprocess.Popen([sys.executable, '-c', '''
import pathlib, signal, sys, time
root = pathlib.Path(sys.argv[1])
def stop(*unused):
    time.sleep(0.3)
    (root / 'flushed').touch()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
(root / 'ready').touch()
while True: time.sleep(0.01)
''', str(self.base)], process_group=daemon.pid)
        self.addCleanup(self.cleanup_process, module)
        deadline = time.monotonic() + 5
        while not (self.base / 'ready').exists():
            if module.poll() is not None or time.monotonic() > deadline:
                self.fail('mock module did not start')
            time.sleep(0.01)
        reaper = threading.Thread(target=module.wait, daemon=True)
        reaper.start()
        owner = node.Node(self.base, 'unused', 'unused')
        owner.process = daemon
        owner.stop()
        reaper.join(timeout=1)
        self.assertEqual(module.returncode, 0)
        self.assertTrue((self.base / 'flushed').exists())


if __name__ == '__main__':
    unittest.main()
