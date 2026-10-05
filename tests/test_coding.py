import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from cumulative_agent.coding import TestSandbox, git
from cumulative_agent.domain import Completion, Project, TaskCreate
from cumulative_agent.service import Service
from test_system import settings


class CodeModel:
    def __init__(self, changes): self.changes = changes; self.calls = 0
    async def complete(self, model, prompt, output_limit, timeout):
        self.calls += 1
        assert 'return a - b' in prompt
        return Completion(text=json.dumps({"summary":"Fix addition", "changes":self.changes}), input_tokens=100, output_tokens=100, cost=.00003)


class PassingSandbox:
    async def run(self, tree, commands, timeout):
        assert 'return a + b' in (tree/'calc.py').read_text()
        return [{"status":"passed","command":commands[0],"exit_code":0,"output":"1 test passed"}]


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_sandbox_never_runs_a_command(self):
        with patch('cumulative_agent.coding.shutil.which', return_value=None), patch('cumulative_agent.coding.asyncio.create_subprocess_exec', new_callable=AsyncMock) as execute:
            reports = await TestSandbox().run(Path('/tmp'), [['python3', '-m', 'unittest']], 1)
            self.assertEqual(reports[0]['status'], 'unavailable')
            execute.assert_not_called()

    async def test_sandbox_start_failure_has_no_unsandboxed_fallback(self):
        with patch('cumulative_agent.coding.shutil.which', return_value='/usr/bin/bwrap'), patch('cumulative_agent.coding.asyncio.create_subprocess_exec', new_callable=AsyncMock, side_effect=PermissionError) as execute:
            reports = await TestSandbox().run(Path('/tmp'), [['python3', '-m', 'unittest']], 1)
            self.assertEqual(reports[0]['status'], 'unavailable')
            self.assertEqual(execute.call_count, 1)
            self.assertIn('--unshare-all', execute.call_args.args)
            self.assertIn('--clearenv', execute.call_args.args)
            self.assertIn('--ro-bind', execute.call_args.args)


class CodingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root/'production'; self.project.mkdir()
        (self.project/'calc.py').write_text('def add(a, b):\n    return a - b\n')
        (self.project/'.env').write_text('NEVER_SHARE=synthetic-key')
        git(self.project,'init','-b','main'); git(self.project,'add','--all')
        git(self.project,'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','base')
        self.head = git(self.project,'rev-parse','HEAD')
        self.model = CodeModel([{"path":"calc.py","content":"def add(a, b):\n    return a + b\n"}])
        cfg = settings(str(self.root/'agent.sqlite3'),project_roots=[str(self.root)])
        self.service = Service(cfg,providers={"mock":self.model})
        self.service.projects.register(Project(id='demo',path=str(self.project),allowed_actions=['read_code','worktree','code_patch','run_tests'],
            share_code_with_llm=True,context_paths=['calc.py'],editable_paths=['calc.py'],test_commands=[['python3','-m','unittest']]))
        self.service.coding.sandbox = PassingSandbox()

    async def asyncTearDown(self):
        await self.service.stop(); self.temp.cleanup()

    async def execute(self):
        task=self.service.tasks.create(TaskCreate(title='Fix addition',project='demo',handler='coding',max_steps=8,required_capabilities=['coding']))
        selected=self.service.scheduler.next()
        if selected is None:return task,None
        run=self.service.worker.claim(selected); await self.service.worker.execute(selected,run)
        return self.service.repo.task(task.id),self.service.repo.one('SELECT * FROM task_runs WHERE id=?',(run,))

    async def test_real_worktree_patch_tests_artifacts_and_main_unchanged(self):
        task,run=await self.execute()
        self.assertEqual(task.status,'done',run['error'])
        report=json.loads(run['result']); self.assertTrue(report['tests_passed']); self.assertTrue(report['review_required'])
        self.assertEqual(git(self.project,'rev-parse','HEAD'),self.head)
        self.assertIn('a - b',(self.project/'calc.py').read_text())
        artifacts=self.service.repo.all('SELECT * FROM artifacts WHERE task_id=?',(task.id,))
        patch=next(a for a in artifacts if a['kind']=='patch')
        self.assertIn('+    return a + b',Path(patch['path']).read_text())
        self.assertNotIn('synthetic-key',Path(patch['path']).read_text())
        self.assertEqual(self.service.repo.task(report['review_task']).blocked_reason,'approval_required')
        self.assertEqual(self.model.calls,1)

    async def test_path_traversal_and_secret_patch_are_rejected_before_writes(self):
        for path in ('../production/calc.py','.env','/tmp/escape','calc.py/../../escape'):
            self.model.changes=[{'path':path,'content':'danger'}]
            task,run=await self.execute()
            self.assertEqual(run['status'],'failed')
            self.assertIn('patch rejected',run['error'])
        self.assertIn('a - b',(self.project/'calc.py').read_text())

    async def test_no_code_opt_in_blocks_before_remote_call(self):
        project=self.service.projects.get('demo');project.share_code_with_llm=False;self.service.projects.register(project)
        task,run=await self.execute()
        self.assertIsNone(run);self.assertEqual(self.service.repo.task(task.id).status,'blocked');self.assertEqual(self.model.calls,0)

    async def test_unavailable_tests_are_not_claimed_as_passed(self):
        class Unavailable:
            async def run(self,*args):return [{'status':'unavailable','reason':'No sandbox'}]
        self.service.coding.sandbox=Unavailable()
        task,run=await self.execute();report=json.loads(run['result'])
        self.assertFalse(report['tests_passed']);self.assertIsNone(report['commit']);self.assertTrue(report['review_required'])

    async def test_background_yields_then_resumes_without_second_model_call(self):
        original=self.model.complete
        async def complete(*args):
            result=await original(*args)
            self.service.tasks.create(TaskCreate(title='Urgent user',handler='system_report',required_capabilities=[]))
            return result
        self.model.complete=complete
        task=self.service.tasks.create(TaskCreate(title='Fix addition',project='demo',handler='coding',type='maintenance',max_steps=8,required_capabilities=['coding']))
        selected=self.service.scheduler.next();run=self.service.worker.claim(selected);await self.service.worker.execute(selected,run)
        self.assertEqual(self.service.repo.one('SELECT status FROM task_runs WHERE id=?',(run,))['status'],'yielded')
        self.assertEqual(self.service.repo.task(task.id).attempts,0)
        user=self.service.scheduler.next();self.assertEqual(user.task.type,'user')
        user_run=self.service.worker.claim(user);await self.service.worker.execute(user,user_run)
        resumed=self.service.scheduler.next();self.assertIsNone(resumed.decision);self.assertEqual(resumed.task.id,task.id)
        resumed_run=self.service.worker.claim(resumed);await self.service.worker.execute(resumed,resumed_run)
        self.assertEqual(self.service.repo.task(task.id).status,'done');self.assertEqual(self.model.calls,1)
        self.assertAlmostEqual(self.service.repo.one('SELECT quality_score FROM usage WHERE run_id=?',(run,))['quality_score'],.8)
        self.assertIsNone(self.service.repo.one('SELECT * FROM workflow_checkpoints WHERE task_id=?',(task.id,)))

    async def test_step_budget_caps_commands_and_never_claims_skipped_tests_passed(self):
        class MultiSandbox:
            async def run(self, tree, commands, timeout):
                return [{'status':'passed','command':command,'exit_code':0} for command in commands]
        self.service.coding.sandbox = MultiSandbox()
        for count in (2, 3):
            project = self.service.projects.get('demo')
            project.test_commands = [['python3', '-m', 'unittest']] * count
            self.service.projects.register(project)
            task, run = await self.execute()
            report = json.loads(run['result'])
            self.assertIsNone(report['commit'])
            self.assertEqual(report['tests_passed'], count == 2)
            self.assertEqual(sum(test['status'] == 'passed' for test in report['tests']), 2)
            if count == 3:
                self.assertEqual(report['tests'][-1]['status'], 'limited')

    async def test_local_project_tests_require_budget_before_snapshot_creation(self):
        task = self.service.tasks.create(TaskCreate(title='Run tests', project='demo', handler='project_tests', required_capabilities=[], max_steps=1))
        self.assertIsNone(self.service.scheduler.next())
        self.assertEqual(self.service.repo.task(task.id).status, 'blocked')
        self.assertIn('max_steps', self.service.repo.task(task.id).blocked_reason)
        self.assertFalse(self.service.coding.root.exists())
