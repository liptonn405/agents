"""Download/probe a local Ollama model; never contacts a cloud inference API."""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:11434"


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


def request(path: str, body: dict | None = None, *, timeout: int = 600):
    value = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=value,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def json_request(path: str, body: dict | None = None, *, timeout: int = 600):
    with request(path, body, timeout=timeout) as response:
        return json.load(response)


def pull(model: str):
    previous = 0.0
    with request("/api/pull", {"model": model, "runner": "llamacpp", "stream": True}, timeout=1800) as response:
        for line in response:
            event = json.loads(line)
            if event.get("error"):
                raise RuntimeError(event["error"])
            if time.monotonic() - previous >= 10 or event.get("status") == "success":
                print(json.dumps({"model": model, **{key: event[key] for key in ("status", "total", "completed") if key in event}}), flush=True)
                previous = time.monotonic()


def probe(model: str, result: dict | None = None, *, reasoning_effort: str | None = None,
          max_tokens: int = 256) -> dict:
    result = {} if result is None else result
    result.update({"model": model, "local_endpoint": BASE + "/v1", "status": "RUNNING", "checks": {}})
    version = json_request("/api/version")
    result["ollama_version"] = version["version"]
    available = json_request("/v1/models")
    assert any(item["id"] == model for item in (available.get("data") or [])), "Model missing from /v1/models"
    result["checks"]["model_list"] = "PASS"
    detail = json_request("/api/show", {"model": model})
    result["capabilities"] = detail.get("capabilities", [])
    result["details"] = detail.get("details", {})
    result["thinking"] = detail.get("thinking")
    result["model_info"] = {key: value for key, value in detail.get("model_info", {}).items()
                            if key.endswith(("context_length", "parameter_count", "architecture"))
                            or key in ("general.finetune", "general.version")}
    assert "tools" in result["capabilities"], "Model does not advertise tools"
    common = {"model": model, "temperature": 0, "max_tokens": max_tokens}
    reasoning_effort = reasoning_effort if reasoning_effort is not None else default_reasoning(detail)
    if reasoning_effort is not None:
        common["reasoning_effort"] = reasoning_effort
    result["request_options"] = common

    started = time.monotonic()
    simple = json_request("/v1/chat/completions", {**common, "stream": False,
        "messages": [{"role": "user", "content": "Reply with exactly LOCAL_OK, without punctuation or explanation."}]})
    result["ordinary_seconds"] = round(time.monotonic() - started, 3)
    answer = simple["choices"][0]
    result["ordinary_answer"] = answer["message"].get("content")
    assert (answer["message"].get("content") or "").strip() == "LOCAL_OK", "Unexpected ordinary answer"
    assert answer["finish_reason"] == "stop", "Ordinary answer did not terminate"
    result["checks"]["ordinary"] = "PASS"

    content, finish, done = [], None, False
    started = time.monotonic()
    with request("/v1/chat/completions", {**common, "stream": True,
        "messages": [{"role": "user", "content": "Reply with exactly LOCAL_STREAM_OK, without punctuation or explanation."}]}) as response:
        for line in response:
            if not line.startswith(b"data: "):
                continue
            if line.strip() == b"data: [DONE]":
                done = True
                break
            event = json.loads(line[6:])
            for choice in event.get("choices", []):
                content.append(choice.get("delta", {}).get("content") or "")
                finish = choice.get("finish_reason") or finish
    result["stream_seconds"] = round(time.monotonic() - started, 3)
    result["stream_answer"] = "".join(content)
    assert result["stream_answer"].strip() == "LOCAL_STREAM_OK", "Unexpected streaming answer"
    assert done and finish == "stop", "Streaming response lacks stop/[DONE]"
    result["checks"]["streaming_and_completion"] = "PASS"

    messages = [
        {"role": "system", "content": "Use the requested tool. After its result, reply with exactly RECORDED."},
        {"role": "user", "content": "Call record_value once with the integer value 7."},
    ]
    tools = [{"type": "function", "function": {"name": "record_value", "description": "Record one integer value",
        "parameters": {"type": "object", "properties": {"value": {"type": "integer"}},
                       "required": ["value"], "additionalProperties": False}}}]
    started = time.monotonic()
    called = json_request("/v1/chat/completions", {**common, "stream": False,
                          "messages": messages, "tools": tools})
    choice = called["choices"][0]
    calls = choice["message"].get("tool_calls", [])
    result["tool_response"] = {"finish_reason": choice.get("finish_reason"),
        "tool_calls": calls, "content": (choice["message"].get("content") or "")[:2048]}
    assert len(calls) == 1, "Expected exactly one tool call"
    call = calls[0]
    assert call["function"]["name"] == "record_value", "Wrong tool selected"
    arguments = call["function"]["arguments"]
    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    assert arguments == {"value": 7}, "Incorrect tool arguments"
    assert choice["finish_reason"] == "tool_calls", "Tool response did not terminate as tool_calls"
    messages += [choice["message"], {"role": "tool", "tool_call_id": call["id"], "content": '{"stored":7}'}]
    final = json_request("/v1/chat/completions", {**common, "stream": False, "messages": messages, "tools": tools})
    choice = final["choices"][0]
    assert choice["finish_reason"] == "stop" and (choice["message"].get("content") or "").strip() == "RECORDED", "Tool loop did not finish correctly"
    result["tool_loop_seconds"] = round(time.monotonic() - started, 3)
    result["tool_arguments"] = arguments
    result["checks"]["tool_call_and_result"] = "PASS"
    result["loaded_models"] = json_request("/api/ps")["models"]
    result["status"] = "COMPLETED"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--pull", action="store_true")
    parser.add_argument("--pull-only", action="store_true")
    parser.add_argument("--reasoning-effort", choices=("none", "low", "medium", "high"),
                        help="Override the default selected from model thinking metadata")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if "cloud" in args.model.lower() or "/" in args.model:
        parser.error("Use an explicit local library model tag")
    if not 64 <= args.max_tokens <= 4096:
        parser.error("--max-tokens must be between 64 and 4096")
    result = {"model": args.model, "status": "BLOCKED", "local_endpoint": BASE + "/v1"}
    try:
        if args.pull or args.pull_only:
            pull(args.model)
        result = {**result, "status": "DOWNLOADED"} if args.pull_only else probe(
            args.model, result, reasoning_effort=args.reasoning_effort, max_tokens=args.max_tokens)
    except urllib.error.HTTPError as exc:
        result["status"] = "BLOCKED"
        result["error"] = f"Local Ollama HTTP {exc.code}: " + exc.read(4096).decode("utf-8", "replace")
    except (OSError, ValueError, RuntimeError, AssertionError, KeyError) as exc:
        result["status"] = "BLOCKED"
        result["error"] = f"{type(exc).__name__}: {exc}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] in ("DOWNLOADED", "COMPLETED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
