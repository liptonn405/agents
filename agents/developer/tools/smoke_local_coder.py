"""Finite live-model smoke of the existing CoderAgent against local Ollama.

Creates only a toy Git repository. The main Miminet repository is never used.
No commits, cloud models, host credentials or system services are involved.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import threading
import time
import uuid
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from coder_agent import CoderAgent, Config, Policy
from coder_agent.build_runtime import build_runtime
from coder_agent.engine import run_process
from coder_agent.gateway import Gateway, Handler
from coder_agent.sandbox import BubblewrapSandbox

ORIGINAL = 'VALUE = 1\n\ndef untouched():\n    return "keep this exactly"\n'
EXPECTED = ORIGINAL.replace("VALUE = 1", "VALUE = 2", 1)
PYTHON_ORIGINAL = 'def clamp(value, lower, upper):\n    return value\n\ndef untouched():\n    return "keep this exactly"\n'
PYTHON_CHECKS = {"status": "PASS", "clamp_cases": 8, "inverted_ranges": 2, "untouched_exact": True}
PYTHON_EVALUATOR = r'''
import ast
import importlib.util
import json
from pathlib import Path

path = Path("/workspace/app.py")
source = path.read_bytes().decode("utf-8")
functions = [node for node in ast.parse(source).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "untouched"]
assert len(functions) == 1
node = functions[0]
assert not node.decorator_list
assert "".join(source.splitlines(keepends=True)[node.lineno - 1:node.end_lineno]) == 'def untouched():\n    return "keep this exactly"\n'
spec = importlib.util.spec_from_file_location("generated_app", path)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
for value, lower, upper, expected in (
        (-3, 0, 10, 0), (0, 0, 10, 0), (4, 0, 10, 4), (10, 0, 10, 10),
        (12, 0, 10, 10), (4, 4, 4, 4), (2.5, 0.0, 2.0, 2.0), (-5, -4, -1, -4)):
    assert app.clamp(value, lower, upper) == expected
for lower, upper in ((3, 2), (-1, -4)):
    try:
        app.clamp(0, lower, upper)
    except ValueError:
        pass
    else:
        raise AssertionError("An inverted range must raise ValueError")
assert app.untouched() == "keep this exactly"
print(json.dumps({"status": "PASS", "clamp_cases": 8, "inverted_ranges": 2, "untouched_exact": True}))
'''


def evaluate_python(config: Config, workspace: Path) -> dict:
    """Operator assertions run in a fresh read-only, closed-network sandbox."""
    isolated = config.model_copy(update={"model_socket": None, "timeout_seconds": 15,
                                          "cpu_seconds": 5, "process_limit": 16})
    log = workspace.parent / "python-evaluation.log"
    command = BubblewrapSandbox(isolated).command(
        SimpleNamespace(root=workspace), workspace.parent / "control", [],
        [isolated.python_binary, "-I", "-B", "-c", PYTHON_EVALUATOR])
    result = run_process(command, log, timeout=15, max_bytes=16_384)
    if result.returncode == 0 and not result.timed_out and not result.overflow:
        try:
            checks = json.loads(result.output)
        except ValueError:
            checks = None
        if checks == PYTHON_CHECKS:
            return checks
    return {"status": "FAILED", "returncode": result.returncode,
            "timed_out": result.timed_out, "overflow": result.overflow, "log": str(log)}


def fingerprint(root: Path):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def default_reasoning(detail: dict) -> str | None:
    thinking = detail.get("thinking")
    values = thinking.get("values", []) if isinstance(thinking, dict) else []
    if not isinstance(values, list):
        return None
    if any(value is False for value in values):
        return "none"
    if any(value is True for value in values) or "low" in values:
        return "low"
    return next((effort for effort in ("medium", "high") if effort in values), None)


def smoke(root: Path, binary: Path, model: str, reasoning_effort: str | None = None,
          scenario: str = "value") -> dict:
    if scenario not in ("value", "python"):
        raise ValueError("Unknown smoke scenario")
    root = root.resolve()
    request = urllib.request.Request("http://127.0.0.1:11434/api/show",
        data=json.dumps({"model": model}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        detail = json.load(response)
    runtime = root / "runtime"
    if not runtime.exists():
        build_runtime(runtime, binary.resolve())
    tag = uuid.uuid4().hex[:8]
    run = root / "smoke" / tag
    source = run / "source"
    source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "app.py").write_text(ORIGINAL if scenario == "value" else PYTHON_ORIGINAL)
    before = fingerprint(source)
    channel = root / "channels" / tag
    channel.mkdir(parents=True, mode=0o700)
    request_summaries = []
    class ObservedHandler(Handler):
        def do_POST(self):
            # Observe only bounded requests, then replay the bytes through all
            # production endpoint, model and budget checks unchanged.
            if self.path != "/v1/chat/completions" or self.headers.get("Transfer-Encoding"):
                return super().do_POST()
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return super().do_POST()
            if not 0 < length <= 2_097_152:
                return super().do_POST()
            raw = self.rfile.read(length)
            self.rfile = io.BytesIO(raw)
            try:
                body = json.loads(raw)
            except ValueError:
                return super().do_POST()
            if isinstance(body, dict):
                offered = body.get("tools")
                offered = offered if isinstance(offered, list) else []
                names = [item["function"].get("name") for item in offered
                         if isinstance(item, dict) and isinstance(item.get("function"), dict)]
                request_summaries.append({"model": body.get("model"), "tools": names,
                    "reasoning_effort": body.get("reasoning_effort"), "max_tokens": body.get("max_tokens"),
                    "temperature": body.get("temperature"), "top_p": body.get("top_p")})
            return super().do_POST()
    config = Config(repository=str(source), runs_dir=str(run / "runs"), runtime_root=str(runtime),
        model_socket=str(channel / "model.sock"), model=model,
        model_context_tokens=8192, model_output_tokens=2048,
        model_reasoning_effort=reasoning_effort if reasoning_effort is not None else default_reasoning(detail),
        model_temperature=0.0,
        timeout_seconds=1800, model_request_timeout_seconds=600,
        max_steps=8, cpu_seconds=600, policy=Policy(read_files=["app.py"], write_files=["app.py"]))
    (run / "config.json").write_text(config.model_dump_json(indent=2))
    task = {"schema_version": "1.0", "task_id": "local-live-smoke", "workspace_id": "smoke-" + tag,
        "attempt": 1, "instructions": [{"file": "app.py", "operation": "modify", "target": "VALUE",
            "requirements": ["Replace only VALUE = 1 with VALUE = 2. Preserve all other bytes, including untouched().",
                             "Read app.py before editing. Do not run tests or shell commands."]}],
        "read_files": ["app.py"], "allowed_write_files": ["app.py"],
        "acceptance_criteria": ["Only app.py changes; only VALUE becomes 2; untouched() stays byte-for-byte identical."]}
    if scenario == "python":
        task["task_id"] += "-python"
        task["instructions"][0].update(target="clamp", requirements=[
            "Implement clamp(value, lower, upper): return lower below the inclusive range, upper above it, and value inside it.",
            "Raise ValueError whenever lower > upper. Support integer and floating-point values.",
            "Preserve the entire unrelated untouched() function byte-for-byte.",
            "Read app.py before editing. Do not run tests or shell commands."])
        task["acceptance_criteria"] = [
            "Only app.py changes; clamp respects inclusive bounds and rejects inverted ranges; untouched() remains identical."]
    (run / "task.json").write_text(json.dumps(task, indent=2))
    started = time.monotonic()
    agent = CoderAgent(config)
    gateway = Gateway(str(channel / "model.sock"), "http://127.0.0.1:11434/v1", model,
                      max_requests=32, max_tokens=2048, upstream_timeout=600)
    gateway.RequestHandlerClass = ObservedHandler
    worker = threading.Thread(target=gateway.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    try:
        worker.start()
        result = agent.run(task)
        summary = {"model": model, "scenario": scenario, "status": result.status, "elapsed_seconds": round(time.monotonic() - started, 3),
                   "config": str(run / "config.json"), "task": str(run / "task.json"), "result": result.model_dump()}
        summary["request_summaries"] = request_summaries
        after = fingerprint(source)
        summary["source_unchanged"] = before == after
        workspace = run / "runs/runs" / result.run_id / "workspace"
        if result.status == "COMPLETED":
            if scenario == "value":
                summary["exact_edit"] = (workspace / "app.py").read_bytes() == EXPECTED.encode("utf-8")
                valid_edit = summary["exact_edit"]
            else:
                summary["behavior_checks"] = evaluate_python(config, workspace)
                valid_edit = summary["behavior_checks"]["status"] == "PASS"
            summary["changed_files_correct"] = result.changed_files == ["app.py"]
            replay = agent.run(task)
            summary["replay_reuses_verified_result"] = replay == result
            negative = json.loads(json.dumps(task))
            negative["task_id"] = "local-negative-smoke"
            negative["instructions"][0]["file"] = "undeclared.py"
            denied = agent.run(negative)
            summary["out_of_scope_plan_status"] = denied.status
            summary["out_of_scope_file_absent"] = not (source / "undeclared.py").exists() and not (workspace / "undeclared.py").exists()
            if (not summary["source_unchanged"] or not valid_edit or not summary["changed_files_correct"]
                    or not summary["replay_reuses_verified_result"] or denied.status != "SECURITY_VIOLATION"
                    or not summary["out_of_scope_file_absent"]):
                summary["status"] = "FAILED"
        (run / "smoke.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        return summary
    finally:
        if worker.is_alive():
            gateway.shutdown()
            worker.join(timeout=5)
        gateway.server_close()
        (channel / "model.sock").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--scenario", choices=("value", "python"), default="value")
    parser.add_argument("--reasoning-effort", choices=("none", "low", "medium", "high"),
                        help="Override the default selected from model thinking metadata")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if "cloud" in args.model.lower() or "/" in args.model:
        parser.error("An explicit local Ollama library tag is required")
    result = smoke(args.root, args.opencode, args.model, args.reasoning_effort, args.scenario)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
