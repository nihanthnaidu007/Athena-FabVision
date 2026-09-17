"""agent/registry.py: availability, defensive loading, and normalization."""

from __future__ import annotations

from agent.registry import (
    REGISTRY,
    ToolRegistry,
    load_fab_tools,
    normalize_source,
    wire_retrieval,
)

from .fakes import async_test, echo_tool, failing_tool, stub_importer, stub_module


def fresh_registry() -> ToolRegistry:
    return ToolRegistry()


@async_test
async def test_register_and_execute_async_tool():
    reg = fresh_registry()
    reg.register("echo", echo_tool)
    block = await reg.execute("echo", user=None, value="w1")
    assert block == {
        "type": "text",
        "title": "Echo",
        "summary": "echo:w1",
        "context": "echo context for the model",
    }


@async_test
async def test_sync_tool_results_are_awaited_transparently():
    reg = fresh_registry()

    def sync_tool(user, **kwargs):
        return {"type": "text", "title": "Sync", "summary": "ok"}

    reg.register("sync_tool", sync_tool)
    assert (await reg.execute("sync_tool", user=None))["summary"] == "ok"


@async_test
async def test_unknown_tool_becomes_error_block():
    reg = fresh_registry()
    block = await reg.execute("missing_tool", user=None)
    assert block["type"] == "error"
    assert "missing_tool" in block["title"]


@async_test
async def test_tool_exception_becomes_error_block_never_raises():
    reg = fresh_registry()
    reg.register("failing_tool", failing_tool)
    block = await reg.execute("failing_tool", user=None)
    assert block["type"] == "error"
    assert "wafer file not found" in block["error"]


@async_test
async def test_non_dict_result_becomes_error_block():
    reg = fresh_registry()

    async def bad_tool(user, **kwargs):
        return "just a string"

    reg.register("bad_tool", bad_tool)
    block = await reg.execute("bad_tool", user=None)
    assert block["type"] == "error"
    assert "invalid result shape" in block["error"]


@async_test
async def test_invalid_block_type_becomes_error_block():
    reg = fresh_registry()

    async def wrong_type_tool(user, **kwargs):
        return {"type": "chart", "title": "Nope"}

    reg.register("wrong_type_tool", wrong_type_tool)
    block = await reg.execute("wrong_type_tool", user=None)
    assert block["type"] == "error"
    assert "chart" in block["error"]


def test_availability_fn_gates_exposure():
    reg = fresh_registry()
    reg.register("on_tool", echo_tool, lambda: True)
    reg.register("off_tool", echo_tool, lambda: False)
    assert reg.tool_names() == ["on_tool"]
    assert reg.tool_schemas()[0]["function"]["name"] == "on_tool"
    # Schema shape is OpenAI function-tool format.
    assert reg.tool_schemas()[0]["type"] == "function"


@async_test
async def test_unavailable_tool_executes_to_error_block():
    reg = fresh_registry()
    reg.register("off_tool", echo_tool, lambda: False)
    block = await reg.execute("off_tool", user=None)
    assert block["type"] == "error"
    assert "not available" in block["error"]


def test_raising_availability_fn_marks_tool_unavailable():
    reg = fresh_registry()

    def broken_availability():
        raise RuntimeError("settings read failed")

    reg.register("broken", echo_tool, broken_availability)
    assert reg.is_available("broken") is False
    assert reg.tool_names() == []


def test_reregistering_replaces_a_tool():
    reg = fresh_registry()

    async def replacement(user, **kwargs):
        return {"type": "text", "title": "Replaced"}

    reg.register("echo", echo_tool)
    reg.register("echo", replacement)
    names = reg.tool_names()
    assert names == ["echo"]


def test_load_fab_tools_registers_contract_entries():
    reg = fresh_registry()
    tools_module = stub_module("tools", TOOLS=[["echo", echo_tool, lambda: True]])
    package = stub_module("fabtools")
    assert load_fab_tools(reg, stub_importer({"fabtools": package, "fabtools.tools": tools_module}))
    assert reg.tool_names() == ["echo"]


def test_load_fab_tools_missing_module_is_noop_not_crash():
    reg = fresh_registry()
    assert load_fab_tools(reg, stub_importer({})) is False
    assert reg.tool_names() == []


def test_load_fab_tools_malformed_entries_skipped():
    reg = fresh_registry()
    tools_module = stub_module(
        "tools",
        TOOLS=[
            "not-a-list-entry",
            ["noname", "not-callable"],
            [None, echo_tool],
            ["echo", echo_tool, "not-callable-availability"],
            ["good", echo_tool],
        ],
    )
    package = stub_module("fabtools")
    assert load_fab_tools(reg, stub_importer({"fabtools": package, "fabtools.tools": tools_module}))
    assert reg.tool_names() == ["good"]


def test_load_fab_tools_with_non_list_TOOLS_stays_disabled():
    reg = fresh_registry()
    tools_module = stub_module("tools", TOOLS={"echo": echo_tool})
    package = stub_module("fabtools")
    assert (
        load_fab_tools(reg, stub_importer({"fabtools": package, "fabtools.tools": tools_module}))
        is False
    )


def test_wire_retrieval_wires_callable():
    reg = fresh_registry()

    async def retrieve(user, query, k=5):
        return []

    module = stub_module("retrieval", retrieve=retrieve)
    package = stub_module("rag")
    assert wire_retrieval(reg, stub_importer({"rag": package, "rag.retrieval": module}))
    assert reg.retrieval is retrieve


def test_wire_retrieval_missing_module_leaves_retrieval_off():
    reg = fresh_registry()
    assert wire_retrieval(reg, stub_importer({})) is False
    assert reg.retrieval is None


def test_wire_retrieval_without_retrieve_attribute_stays_off():
    reg = fresh_registry()
    module = stub_module("retrieval")  # no retrieve attr
    package = stub_module("rag")
    assert wire_retrieval(reg, stub_importer({"rag": package, "rag.retrieval": module})) is False


def test_normalize_source_enforces_contract_shape():
    normalized = normalize_source(
        {"document_id": 3, "chunk_id": 9, "title": "Runbook", "snippet": "text", "score": 0.91}
    )
    assert normalized == {
        "document_id": 3,
        "chunk_id": 9,
        "title": "Runbook",
        "snippet": "text",
        "score": 0.91,
    }


def test_normalize_source_degrades_malformed_hits():
    normalized = normalize_source({"title": None, "score": "oops"})
    assert normalized["title"] == "Untitled document"
    assert normalized["snippet"] == ""
    assert normalized["score"] == 0.0
    # Non-dict hits survive too.
    assert normalize_source("raw text")["snippet"] == "raw text"


def test_global_registry_exists_and_starts_empty():
    assert isinstance(REGISTRY, ToolRegistry)
