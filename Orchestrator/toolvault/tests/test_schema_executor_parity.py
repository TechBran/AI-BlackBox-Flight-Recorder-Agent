"""Each tool's schema describes what its executor actually does (audit
O18-toolvault-modules-12).

The schema is everything a model knows about a tool. When it drifts from the
executor the model is misled: a required param the executor ignores, a default
it does not apply, a param it reads but the model can never send, an operator
marked required that the server resolves itself, an example that passes the
wrong type. These checks read every real module under ToolVault/tools:

  * every ``params.get("<key>")`` / ``params["<key>"]`` an executor reads is a
    declared property (a handful of documented legacy aliases aside);
  * a literal default in the executor equals the schema's declared default;
  * an operator property (``x-source: operators``) is never required -- the
    server resolves it, and on the MCP path ignores the caller's value;
  * a parseable ``example`` passes each argument as its declared type.
"""
import ast
import json
from pathlib import Path

import pytest

from Orchestrator.toolvault import registry

# Keys an executor reads that are deliberately NOT advertised to the model.
UNDECLARED_OK = {
    # Legacy alias of `limit` some older callers still send.
    "search_snapshots": {"k"},
}

# Literal executor defaults that intentionally differ from the schema's.
DEFAULT_MISMATCH_OK: dict = {}


def _modules():
    for folder in sorted(registry.TOOLS_DIR.iterdir()):
        schema = folder / "schema.json"
        if schema.exists():
            yield folder, json.loads(schema.read_text(encoding="utf-8"))


def _executor_reads(source: str) -> dict:
    """{key: [literal default or None, ...]} for every read of ``params``."""
    reads: dict = {}
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "pop")
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "params"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            default = None
            if len(node.args) > 1:
                try:
                    default = ("literal", ast.literal_eval(node.args[1]))
                except ValueError:
                    default = None
            reads.setdefault(node.args[0].value, []).append(default)
        elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name) and node.value.id == "params"
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            reads.setdefault(node.slice.value, []).append(None)
    return reads


_WITH_EXECUTOR = [(f.name, s) for f, s in _modules() if (f / "executor.py").exists()]


@pytest.mark.parametrize("name,schema", _WITH_EXECUTOR, ids=[n for n, _ in _WITH_EXECUTOR])
def test_executor_reads_only_declared_params(name, schema):
    props = schema["parameters"].get("properties") or {}
    source = (registry.TOOLS_DIR / name / "executor.py").read_text(encoding="utf-8")
    undeclared = set(_executor_reads(source)) - set(props) - UNDECLARED_OK.get(name, set())
    assert not undeclared, f"{name} reads params the schema never declares: {sorted(undeclared)}"


@pytest.mark.parametrize("name,schema", _WITH_EXECUTOR, ids=[n for n, _ in _WITH_EXECUTOR])
def test_executor_defaults_match_the_schema(name, schema):
    props = schema["parameters"].get("properties") or {}
    source = (registry.TOOLS_DIR / name / "executor.py").read_text(encoding="utf-8")
    wrong = []
    for key, defaults in _executor_reads(source).items():
        declared = props.get(key)
        if not isinstance(declared, dict) or "default" not in declared:
            continue
        for d in defaults:
            if d is not None and d[1] != declared["default"] \
                    and (name, key) not in DEFAULT_MISMATCH_OK:
                wrong.append((key, declared["default"], d[1]))
    assert not wrong, f"{name}: (param, schema default, executor default) {wrong}"


def test_no_schema_requires_the_operator():
    offenders = []
    for folder, schema in _modules():
        params = schema["parameters"]
        props = params.get("properties") or {}
        for key in params.get("required") or []:
            if isinstance(props.get(key), dict) and props[key].get("x-source") == "operators":
                offenders.append(schema["name"])
    assert offenders == [], f"operator marked required (it resolves server-side): {offenders}"


_PY_TYPES = {"array": ast.List, "object": ast.Dict}


def test_examples_pass_each_argument_as_its_declared_type():
    wrong = []
    for folder, schema in _modules():
        example = schema.get("example")
        if not isinstance(example, str):
            continue
        try:
            tree = ast.parse(example.replace("true", "True").replace("false", "False"), mode="eval")
        except SyntaxError:
            continue                      # prose examples are not checked
        if not isinstance(tree.body, ast.Call):
            continue
        props = schema["parameters"].get("properties") or {}
        for kw in tree.body.keywords:
            declared = props.get(kw.arg)
            if declared is None:
                wrong.append((schema["name"], kw.arg, "undeclared"))
                continue
            want = _PY_TYPES.get(declared.get("type"))
            if want is not None and not isinstance(kw.value, want):
                wrong.append((schema["name"], kw.arg, f"not a {declared['type']}"))
    assert wrong == [], wrong


# The specific drifts the audit found, pinned so they cannot come back.

def test_get_music_status_does_not_promise_a_task_lookup():
    schema = registry.get_tool("get_music_status")
    assert "task_id" not in (schema["parameters"].get("required") or [])
    assert "get_task_status" in schema["description"]


def test_get_media_is_not_described_as_asynchronous():
    schema = registry.get_tool("get_media")
    assert "task_id (string) for async" not in schema.get("returns", "")
    assert "Async operation" not in schema.get("notes", "")


def test_device_control_tools_declare_the_params_they_read():
    assert "device" in registry.get_tool("control_phone")["parameters"]["properties"]
    props = registry.get_tool("control_device")["parameters"]["properties"]
    assert {"model", "task_id"} <= set(props)


def test_search_snapshots_does_not_promise_cross_operator_search():
    desc = registry.get_tool("search_snapshots")["parameters"]["properties"]["operator"]["description"]
    assert "ALL operators" not in desc
