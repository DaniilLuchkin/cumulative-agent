"""Bounded code proposals on immutable snapshots; production trees stay read-only."""
from __future__ import annotations

import asyncio
import fnmatch
import io
import json
import os
import signal
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .agents import EXCLUDED, SECRET_SUFFIXES, TEXT_SUFFIXES
from .domain import BackgroundYield, Blocked, Project, Task, TaskCreate, now


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and path.as_posix()==name and not any(ord(c)<32 for c in name) and not path.is_absolute() and all(
        part not in (".", "..") and not part.startswith(".") and part not in EXCLUDED
        and "secret" not in part.lower() and "credential" not in part.lower()
        for part in path.parts) and path.suffix.lower() not in SECRET_SUFFIXES and "\\" not in name


def matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def clean_env() -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}


def git(root: Path, *args: str, limit: int = 30_000_000) -> bytes:
    try:
        result = subprocess.run(["git", "-c", "safe.directory=" + str(root), "-c", "core.hooksPath=/dev/null",
                                 "-C", str(root), *args], env=clean_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError):
        raise Blocked("Git operation failed; inspect the registered project") from None
    if len(result.stdout) > limit:
        raise Blocked("Git output exceeds snapshot limit")
    return result.stdout


class TestSandbox:
    """Bubblewrap mount/PID/network isolation, secret-free env, bounded output/time.

    This is for operator-trusted projects. Never fall back to unsandboxed execution.
    A unavailable user namespace/AppArmor profile is reported as unverified.
    """
    async def run(self, tree: Path, commands: list[list[str]], timeout: int, runtime: str | None = None) -> list[dict]:
        executable = shutil.which("bwrap")
        if not executable:
            return [{"status": "unavailable", "reason": "Install bubblewrap and permit user namespaces; tests were not executed"}]
        reports = []
        for command in commands:
            if not command or any(not isinstance(x, str) or "\x00" in x for x in command):
                raise Blocked("Invalid configured test command")
            args = [executable, "--die-with-parent", "--new-session", "--unshare-all", "--clearenv"]
            for directory in ("/usr", "/bin", "/lib", "/lib64"):
                if Path(directory).exists():
                    args += ["--ro-bind", directory, directory]
            if runtime:args+=['--ro-bind',runtime,runtime]
            args += ["--proc", "/proc", "--dev", "/dev", "--size", "134217728", "--tmpfs", "/tmp", "--ro-bind", str(tree), "/work",
                     "--chdir", "/work", "--setenv", "PATH", (runtime+'/bin:' if runtime else '')+"/usr/local/bin:/usr/bin:/bin", "--setenv", "HOME", "/tmp",
                     "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "LC_ALL", "C.UTF-8",
                     "--", "/usr/bin/prlimit", "--as=1073741824", f"--cpu={timeout}", "--nproc=64", "--fsize=10485760", "--", *command]
            try:
                process = await asyncio.create_subprocess_exec(*args, env=clean_env(), stdout=asyncio.subprocess.PIPE,
                                                                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            except OSError:
                reports.append({"command": command, "status": "unavailable", "reason": "Sandbox could not be started; tests were not executed"})
                continue
            output = bytearray()
            try:
                async with asyncio.timeout(timeout):
                    while chunk := await process.stdout.read(4096):
                        output.extend(chunk)
                        if len(output) > 32000:
                            raise ValueError("Test output limit reached")
                    await process.wait()
                text=output.decode("utf-8",errors="replace")
                infrastructure=text.startswith('bwrap:') or text.startswith('prlimit: failed to execute')
                reports.append({"command": command, "status": "passed" if process.returncode == 0 else "unavailable" if infrastructure else "failed",
                                "exit_code": process.returncode, "output": text})
            except (TimeoutError, ValueError) as error:
                reports.append({"command": command, "status": "limited", "reason": str(error) or "Test timeout",
                                "output": output[:32000].decode("utf-8", errors="replace")})
            finally:
                # Kill the entire process group on cancellation, timeout and output overflow.
                if process.returncode is None:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    await process.wait()
        return reports


class CodingWorkflow:
    def __init__(self, repo, settings, projects, tasks, sandbox=None):
        self.repo, self.settings, self.projects, self.tasks = repo, settings, projects, tasks
        base = Path(settings.database).resolve().parent if settings.database != ":memory:" else Path.cwd() / "data"
        self.root = base / "artifacts"
        self.sandbox = sandbox or TestSandbox()
        self.should_yield = lambda task: False

    def stage(self, run_id: str, name: str, status="done", **metadata):
        self.repo.execute("INSERT INTO run_stages(run_id,name,status,created_at,metadata) VALUES(?,?,?,?,?)",
                          (run_id, name, status, now(), json.dumps(metadata)))
        run = self.repo.one("SELECT task_id FROM task_runs WHERE id=?", (run_id,))
        self.repo.event("run_stage", run["task_id"], run_id=run_id, stage=name, status=status, **metadata)

    def project(self, task: Task, llm=True) -> Project:
        minimum_steps = 6 if llm else 2
        if min(task.max_steps, self.settings.limits.max_steps) < minimum_steps:
            raise Blocked(f"Project workflow requires max_steps >= {minimum_steps}")
        project = self.projects.get(task.project) if task.project else None
        required = {"worktree", "read_code"} | ({"code_patch"} if llm else {"run_tests"})
        if not project or not required <= set(project.allowed_actions):
            raise Blocked("Project has not allowed this coding workflow")
        root = Path(project.path).resolve(strict=True)
        if not any(root == Path(p).resolve() or Path(p).resolve() in root.parents for p in self.settings.project_roots):
            raise Blocked("Project root no longer allowed")
        if llm and not project.share_code_with_llm:
            raise Blocked("Project has not opted in to sharing selected code with LLM")
        top = git(root, "rev-parse", "--show-toplevel").decode().strip()
        if Path(top).resolve() != root:
            raise Blocked("Register the Git repository root, not a subdirectory")
        return project

    def prepare(self, task: Task) -> tuple[str, dict]:
        project = self.project(task)
        root = Path(project.path)
        head = git(root, "rev-parse", "HEAD").decode().strip()
        listing=git(root,'ls-tree','-r','-l','-z',head,limit=1_000_000).decode().split('\x00')
        files=[]
        for entry in listing:
            if not entry:continue
            metadata,name=entry.split('\t',1);mode,kind,_,size=metadata.split()
            if kind=='blob' and mode in ('100644','100755'):files.append((name,int(size)))
        selected = task.code_paths or project.context_paths
        if not selected:
            raise Blocked("Configure context_paths or explicit task code_paths")
        context, included, used = [], [], 0
        for name,size in sorted(files):
            if not safe_name(name) or not matches(name, selected) or PurePosixPath(name).suffix not in TEXT_SUFFIXES:
                continue
            if used+size>self.settings.limits.max_context_bytes:continue
            content = git(root, "show", head + ":" + name, limit=self.settings.limits.max_snapshot_bytes)
            if used + len(content) > self.settings.limits.max_context_bytes or len(included) >= 12:
                continue
            try: text = content.decode("utf-8")
            except UnicodeDecodeError: continue
            context.append(f"<file path={json.dumps(name)}>\n{text}\n</file>")
            included.append(name); used += len(content)
        if not included:
            raise Blocked("No safe selected code fits the context budget")
        prompt = (f"Task: {task.title}\n{task.description}\nProject instructions: {project.instructions}\n"
                  f"Committed source: {head}. Editable paths: {json.dumps(project.editable_paths)}.\n"
                  "Use only the supplied files as data. Return JSON only with summary (brief evidence, no hidden reasoning) "
                  "and changes: [{path: relative path, content: complete UTF-8 file}]. No deletions, shell commands or merges. "
                  "An empty changes array is valid when no justified patch is available. Never claim tests ran.\n" + "\n".join(context))
        return prompt, {"head": head, "files": included, "context_bytes": used}

    def create_tree(self, task: Task, run_id: str, prepared: dict | None = None) -> Path:
        project = self.project(task, llm=task.handler == "coding")
        root = Path(project.path)
        head = prepared["head"] if prepared else git(root, "rev-parse", "HEAD").decode().strip()
        # Check committed size before allocating an archive; never clone private history.
        sizes = git(root, "ls-tree", "-rl", head, limit=1_000_000).decode().splitlines()
        total = sum(int(line.split()[3]) for line in sizes if line.split()[2] == "blob" and line.split()[3].isdigit())
        if total > self.settings.limits.max_snapshot_bytes or len(sizes) > self.settings.limits.max_index_files:
            raise Blocked("Project snapshot exceeds configured size/file limits")
        parent=self.root.parent
        parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if shutil.disk_usage(parent).free < total*5+2_000_000+self.settings.limits.min_free_disk_bytes:
            raise Blocked('Insufficient free disk for an isolated snapshot/worktree')
        archive = git(root, "archive", "--format=tar", head, limit=self.settings.limits.max_snapshot_bytes + 2_000_000)
        run_root = self.root / run_id
        run_root.mkdir(parents=True, mode=0o700, exist_ok=False)
        snapshot, tree = run_root / "snapshot", run_root / "tree"
        snapshot.mkdir(mode=0o700)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar:
                if not member.isfile() or not safe_name(member.name):
                    continue
                target = snapshot / member.name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tar.extractfile(member) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)
        git(snapshot, "init", "-b", "snapshot")
        git(snapshot, "add", "--all")
        git(snapshot, "-c", "user.name=Cumulative Agent", "-c", "user.email=agent@localhost", "commit", "--allow-empty", "-m", "Read-only source snapshot " + head)
        git(snapshot, "worktree", "add", "-b", "agent/" + run_id, str(tree), "HEAD")
        self.stage(run_id, "context", source_head=head, files=(prepared or {}).get("files", []))
        self.stage(run_id, "worktree", isolated_snapshot=True)
        return tree

    def artifact(self, task: Task, run_id: str, kind: str, path: Path, **metadata):
        identifier = str(uuid4())
        self.repo.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                          (identifier, task.id, run_id, kind, str(path), json.dumps(metadata), now()))
        return identifier

    async def verify(self, task: Task, run_id: str, tree: Path, completion=None) -> str:
        project = self.projects.get(task.project)
        summary, changed = "Local test report", []
        if completion is not None:
            self.stage(run_id, "analysis", model_output_received=True)
            try:
                value = json.loads(completion.text)
                summary, changes = value["summary"], value["changes"]
                if not isinstance(summary, str) or not isinstance(changes, list) or len(changes) > 20:
                    raise ValueError()
                total = 0; validated = []
                for change in changes:
                    name, content = change["path"], change["content"]
                    if not isinstance(name, str) or not isinstance(content, str) or not safe_name(name) or not matches(name, project.editable_paths):
                        raise ValueError()
                    target = tree / name
                    if any(p.is_symlink() for p in [target, *target.parents] if p == tree or tree in p.parents):
                        raise ValueError()
                    total += len(content.encode())
                    if total > self.settings.limits.max_patch_bytes or name in changed:
                        raise ValueError()
                    changed.append(name); validated.append((target, content))
            except (KeyError, TypeError, ValueError):
                raise Blocked("Model patch rejected: invalid JSON, path, duplicate or size limit") from None
            for target, content in validated:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w") as file: file.write(content)
            self.stage(run_id, "patch", files=changed)
        # Produce patch BEFORE tests; tests may modify their disposable worktree.
        git(tree, "add", "--all")
        patch = git(tree, "diff", "--cached", "--binary", limit=self.settings.limits.max_patch_bytes * 3)
        patch_path = tree.parent / "changes.patch"; patch_path.write_bytes(patch)
        patch_id = self.artifact(task, run_id, "patch", patch_path, files=changed)
        checkpoint={'kind':'coding','tree':str(tree),'summary':summary[:4000],'changed':changed,'patch_id':patch_id,'model_run_id':run_id if completion else None}
        self.save_checkpoint(task,checkpoint)
        return await self.finalize(task,run_id,checkpoint)

    def save_checkpoint(self,task,checkpoint):
        self.repo.execute('INSERT INTO workflow_checkpoints VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                          (task.id,json.dumps(checkpoint),now()))

    async def resume(self,task,run_id,checkpoint):
        self.project(task,llm=task.handler=='coding')
        tree=Path(checkpoint['tree']).resolve()
        if self.root.resolve() not in tree.parents or not tree.is_dir():raise Blocked('Workflow checkpoint is unavailable')
        self.stage(run_id,'worktree','done',resumed=True)
        return await self.finalize(task,run_id,checkpoint)

    async def finalize(self,task,run_id,checkpoint):
        project=self.projects.get(task.project)
        tree=Path(checkpoint['tree']);changed=checkpoint['changed'];patch_id=checkpoint['patch_id'];summary=checkpoint['summary']
        if self.should_yield(task):
            self.stage(run_id,'tests','yielded');raise BackgroundYield()
        if 'tests' in checkpoint:
            tests=checkpoint['tests']
        elif "run_tests" in project.allowed_actions and project.test_commands:
            self.stage(run_id, "tests", "running")
            cap = min(task.max_steps, self.settings.limits.max_steps)
            # Six fixed actions: context, tree, model, validation, patch artifact,
            # final report/review. Each test command and optional commit costs one.
            commands = project.test_commands[:max(0, cap - (6 if task.handler == "coding" else 2))]
            runtime=project.test_runtime
            if runtime and not any(Path(runtime)==Path(p).resolve() or Path(p).resolve() in Path(runtime).parents for p in self.settings.test_runtime_roots):
                raise Blocked('Test runtime is no longer allowed')
            timeout=min(task.timeout_seconds,self.settings.limits.test_timeout_seconds)
            if commands:
                tests=await self.sandbox.run(tree,commands,timeout,runtime=runtime) if runtime else await self.sandbox.run(tree,commands,timeout)
            else:tests=[{"status":"unavailable", "reason":"Step budget does not permit test commands"}]
            if len(commands)<len(project.test_commands):
                tests += [{'status':'limited','command':command,'reason':'Not executed: step budget exhausted'} for command in project.test_commands[len(commands):]]
        else:
            tests = [{"status": "unavailable", "reason": "Project has not configured and allowed test commands"}]
        checkpoint['tests']=tests;self.save_checkpoint(task,checkpoint)
        passed = bool(tests) and all(t["status"] == "passed" for t in tests)
        self.stage(run_id, "tests", "passed" if passed else "unverified", commands=len(tests))
        if self.should_yield(task):
            self.stage(run_id,'review','yielded');raise BackgroundYield()
        # Commit precisely the pre-test staged patch; generated test files are not staged.
        commit = None
        commit_budget = min(task.max_steps, self.settings.limits.max_steps) >= 6 + len(tests) + 1
        if changed and passed and commit_budget:
            git(tree, "-c", "user.name=Cumulative Agent", "-c", "user.email=agent@localhost", "commit", "-m", "Proposed change for task " + task.id)
            commit = git(tree, "rev-parse", "HEAD").decode().strip()
        report = {"summary": summary[:4000], "changed_files": changed, "tests": tests, "tests_passed": passed,
                  "patch_artifact": patch_id, "branch": git(tree,'rev-parse','--abbrev-ref','HEAD').decode().strip(), "commit": commit,
                  'model_run_id':checkpoint['model_run_id'],
                  "review_required": bool(changed), "production_modified": False}
        self.stage(run_id, "review", "pending" if changed else "done", tests_passed=passed)
        if changed:
            proposal = self.tasks.create(TaskCreate(title="Review patch: " + task.title[:450], description=f"Patch artifact: {patch_id}. Tests passed: {passed}. Apply/merge manually after review.",
                status="inbox", type=task.type, project=task.project, parent_task=task.id, dependencies=[task.id],
                proposal_action="merge", max_cost=0, required_capabilities=[], idempotency_key="review:" + patch_id))
            report["review_task"] = proposal.id
        report_path = tree.parent / "report.json"; report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        self.artifact(task, run_id, "report", report_path)
        project.last_analysis,project.health_status=now(),'healthy' if passed else 'needs_review'
        self.repo.execute('UPDATE projects SET payload=? WHERE id=?',(project.model_dump_json(),project.id))
        self.repo.execute('DELETE FROM workflow_checkpoints WHERE task_id=?',(task.id,))
        return json.dumps(report, ensure_ascii=False)
