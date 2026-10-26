"""Recovery tests use private temporary stores and trusted fake engines only."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from coder_support import CoderCase, FakeEngine
from coder_agent import CoderAgent, Policy, Result, Task
from coder_agent.engine import EngineOutcome
from coder_agent.policy import Rejected
from coder_agent.reports import operation_key
import coder_agent.reports as reports_module


WORKER = r'''
import json,sys,time
from pathlib import Path
from coder_agent import CoderAgent,Config
from coder_agent.engine import EngineOutcome
config=Config.model_validate_json(Path(sys.argv[1]).read_bytes())
payload=Path(sys.argv[2]).read_bytes()
ready=Path(sys.argv[3])
class PartialEngine:
    def execute(self,task,workspace,run_dir):
        assert (workspace.root/'src/app.py').read_text() == 'VALUE = 1\n'
        (workspace.root/'src/app.py').write_text('PARTIAL = True\n')
        ready.write_text(json.dumps({'run_id':run_dir.name}))
        while True:
            time.sleep(.1)
CoderAgent(config,engine=PartialEngine()).run(payload)
'''


class RecoveryTests(CoderCase):
    def receipt(self):
        task = Task.model_validate(self.payload)
        return self.root / 'runs/receipts' / f'{operation_key(task)}-{task.attempt}.json'

    def assert_blocked(self, result):
        self.assertEqual(result.status, 'BLOCKED', result.error)
        self.assertNotIn('files', result.artifacts)
        self.assertNotIn('patch', result.artifacts)
        self.assertEqual(result.task_id, self.payload['task_id'])
        self.assertEqual(result.attempt, self.payload['attempt'])
        self.assertEqual((self.repo / 'src/app.py').read_text(), 'VALUE = 1\n')

    def test_receipt_io_error_is_structured(self):
        with patch.object(self.agent.reports, 'previous', side_effect=OSError('private-sentinel')):
            result = self.agent.run(self.payload)
        self.assert_blocked(result)
        self.assertNotIn('private-sentinel', result.model_dump_json())
        self.assertEqual(self.fake.calls, 0)

    def test_run_creation_failure_is_structured(self):
        with patch.object(self.agent.reports, 'begin', side_effect=OSError('disk full')):
            result = self.agent.run(self.payload)
        self.assert_blocked(result)
        self.assertEqual(result.artifacts, {})
        self.assertEqual(self.fake.calls, 0)

    def test_workspace_creation_failure_is_structured(self):
        with patch('coder_agent.agent.Workspace.prepare', side_effect=OSError('disk full')):
            result = self.agent.run(self.payload)
        self.assert_blocked(result)
        self.assertEqual(self.fake.calls, 0)
        self.assertTrue(self.receipt().exists())
        self.assertEqual(self.agent.run(self.payload), result)

    def test_persist_failure_after_engine_never_reexecutes(self):
        with patch.object(self.agent.reports, 'finish', side_effect=OSError('disk full')):
            result = self.agent.run(self.payload)
        self.assert_blocked(result)
        self.assertEqual(result.artifacts, {})
        self.assertTrue(self.receipt().exists())
        replay = self.agent.run(self.payload)
        self.assert_blocked(replay)
        self.assertIn(result.run_id, replay.error)
        self.assertEqual(self.fake.calls, 1)

    def test_claim_failure_after_publication_preserves_receipt(self):
        original = reports_module.sync_directory
        def failing_sync(path):
            if path == self.receipt().parent:
                raise OSError('simulated receipt fsync failure')
            return original(path)
        with patch('coder_agent.reports.sync_directory', side_effect=failing_sync):
            result = self.agent.run(self.payload)
        self.assert_blocked(result)
        receipt = self.receipt().read_bytes()
        replay = self.agent.run(self.payload)
        self.assert_blocked(replay)
        self.assertEqual(self.receipt().read_bytes(), receipt)
        self.assertEqual(self.fake.calls, 0)

    def test_claim_never_overwrites_existing_receipt(self):
        task = Task.model_validate(self.payload)
        digest = hashlib.sha256(json.dumps(task.model_dump(), sort_keys=True).encode()).hexdigest()
        self.agent.reports.claim(task, digest, 'a' * 32)
        before = self.receipt().read_bytes()
        with self.assertRaises(Rejected):
            self.agent.reports.claim(task, digest, 'b' * 32)
        self.assertEqual(self.receipt().read_bytes(), before)

    def test_corrupt_receipt_is_blocked_and_preserved(self):
        self.receipt().write_text('{"run_id":"../../outside"}')
        before = self.receipt().read_bytes()
        result = self.agent.run(self.payload)
        self.assert_blocked(result)
        self.assertEqual(self.fake.calls, 0)
        self.assertEqual(self.receipt().read_bytes(), before)

    def test_malformed_receipt_json_is_blocked(self):
        self.receipt().write_text('{truncated')
        self.assert_blocked(self.agent.run(self.payload))
        self.assertEqual(self.fake.calls, 0)

    def test_corrupt_finished_report_is_not_reexecuted(self):
        first = self.agent.run(self.payload)
        (self.workspace(first).parent / 'report.json').write_text('{bad')
        result = self.agent.run(self.payload)
        self.assert_blocked(result)
        self.assertEqual(self.fake.calls, 1)
        self.assertEqual(self.agent.status(first.run_id)['state'], 'BLOCKED')

    def test_corrupt_artifact_identifier_is_not_returned(self):
        first = self.agent.run(self.payload)
        report_path = self.workspace(first).parent / 'report.json'
        report = json.loads(report_path.read_text())
        report['artifacts']['files'] = '../../outside'
        report_path.write_text(json.dumps(report))
        self.assert_blocked(self.agent.run(self.payload))
        self.assertEqual(self.fake.calls, 1)

    def test_cached_success_cannot_restore_revoked_permissions(self):
        first = self.agent.run(self.payload)
        self.assertEqual(first.status, 'COMPLETED')
        policy = Policy(read_files=[], write_files=self.config.policy.write_files)
        fake = FakeEngine()
        agent = CoderAgent(self.config.model_copy(update={'policy': policy}), engine=fake)
        result = agent.run(self.payload)
        self.assertEqual(result.status, 'SECURITY_VIOLATION')
        self.assertNotIn('files', result.artifacts)
        self.assertEqual(fake.calls, 0)

    def test_receipt_is_bound_to_trusted_repository(self):
        first = self.agent.run(self.payload)
        self.assertEqual(first.status, 'COMPLETED')
        second_repo = self.root / 'another-repo'
        shutil.copytree(self.repo, second_repo)
        fake = FakeEngine()
        agent = CoderAgent(self.config.model_copy(update={'repository': str(second_repo)}), engine=fake)
        result = agent.run(self.payload)
        self.assert_blocked(result)
        self.assertIn(first.run_id, result.error)
        self.assertEqual(fake.calls, 0)

    def test_completed_artifacts_are_sealed_and_checked_on_replay(self):
        mutations = ('source', 'patch', 'mode', 'manifest', 'extra')
        for attempt, mutation in enumerate(mutations, start=1):
            with self.subTest(mutation=mutation):
                self.payload['attempt'] = attempt
                first = self.agent.run(self.payload)
                self.assertEqual(first.status, 'COMPLETED', first.error)
                root = self.workspace(first)
                if mutation == 'source':
                    (root / 'src/app.py').write_text('CORRUPTED\n')
                elif mutation == 'patch':
                    (root.parent / 'changes.patch').write_text('corrupt patch')
                elif mutation == 'mode':
                    (root / 'src/app.py').chmod(0o755)
                elif mutation == 'manifest':
                    (root.parent / 'outputs.json').unlink()
                else:
                    (root / 'extra.py').write_text('unexpected')
                replay = self.agent.run(self.payload)
                self.assert_blocked(replay)
                self.assertIn(first.run_id, replay.error)
                self.assertEqual(self.fake.calls, attempt)

    def test_success_syncs_output_before_final_report(self):
        events = []
        original_sync = os.fsync
        original_publish = reports_module.atomic_json
        def sync(fd):
            events.append(('sync', fd))
            return original_sync(fd)
        def publish(path, value, **kwargs):
            events.append(('publish', path.name))
            return original_publish(path, value, **kwargs)
        with patch('coder_agent.reports.os.fsync', side_effect=sync), \
                patch('coder_agent.reports.atomic_json', side_effect=publish):
            result = self.agent.run(self.payload)
        self.assertEqual(result.status, 'COMPLETED', result.error)
        outputs = events.index(('publish', 'outputs.json'))
        report = events.index(('publish', 'report.json'))
        self.assertLess(outputs, report)
        self.assertTrue(any(kind == 'sync' for kind, _ in events[:outputs]))

    def test_corrupt_state_is_structured(self):
        run_id = 'c' * 32
        directory = self.agent.reports.begin(run_id, operation_key(Task.model_validate(self.payload)))
        (directory / 'state.json').write_text('{"operation_key":"../../outside"}')
        state = self.agent.status(run_id)
        self.assertEqual(state['state'], 'BLOCKED')
        self.assertFalse((self.root / 'outside').exists())

    def test_status_rechecks_report_after_lock(self):
        run_id = 'd' * 32
        self.agent.reports.begin(run_id, operation_key(Task.model_validate(self.payload)))
        report = Result(run_id=run_id, status='FAILED', task_id='543', workspace_id='workspace-543', attempt=1)
        original = self.agent.reports._report
        calls = 0
        def raced_read(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.agent.reports.finish(report)
                raise FileNotFoundError('report was absent just before publication')
            return original(*args)
        with patch.object(self.agent.reports, '_report', side_effect=raced_read):
            self.assertEqual(self.agent.status(run_id)['state'], 'FINISHED')
        self.assertEqual(calls, 2)

    def start_partial_process(self, payload):
        config = self.root / 'worker-config.json'
        task_path = self.root / 'worker-task.json'
        ready = self.root / 'worker-ready.json'
        config.write_text(self.config.model_dump_json())
        task_path.write_text(json.dumps(payload))
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
               'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        process = subprocess.Popen([sys.executable, '-c', WORKER, str(config), str(task_path), str(ready)],
                                   cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def stop():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.02)
        if not ready.exists():
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            self.fail(f'Fake worker failed before ready: {stdout!r} {stderr!r}')
        return process, json.loads(ready.read_text())['run_id']

    def test_process_lock_and_sigkill_replay_are_safe(self):
        process, run_id = self.start_partial_process(self.payload)
        self.assertEqual(self.agent.status(run_id)['state'], 'RUNNING')
        receipt = self.receipt().read_bytes()
        self.assert_blocked(self.agent.run(self.payload))
        self.assertEqual(self.fake.calls, 0)
        os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        self.assertEqual(self.agent.status(run_id)['state'], 'INTERRUPTED')
        replay = self.agent.run(self.payload)
        self.assert_blocked(replay)
        self.assertIn(run_id, replay.error)
        self.assertEqual(self.receipt().read_bytes(), receipt)
        self.assertEqual(self.fake.calls, 0)
        self.payload['attempt'] = 2
        def fresh(task, workspace, run_dir):
            self.assertEqual((workspace.root / 'src/app.py').read_text(), 'VALUE = 1\n')
            (workspace.root / 'src/app.py').write_text('VALUE = 2\n')
            return EngineOutcome('COMPLETED', 'Fresh snapshot edited')
        self.fake.action = fresh
        self.assertEqual(self.agent.run(self.payload).status, 'COMPLETED')

    def test_old_run_is_not_running_during_new_attempt_process(self):
        old_run = 'e' * 32
        task = Task.model_validate(self.payload)
        self.agent.reports.begin(old_run, operation_key(task))
        self.payload['attempt'] = 2
        process, active_run = self.start_partial_process(self.payload)
        self.assertEqual(self.agent.status(active_run)['state'], 'RUNNING')
        self.assertEqual(self.agent.status(old_run)['state'], 'INTERRUPTED')
        process.kill()
        process.communicate(timeout=5)
