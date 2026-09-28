import json
from types import SimpleNamespace

import httpx
import pytest

from video_kb.notion import (
    NotionGateway, NotionNotSent, NotionUncertain, NotionError, NotionCompatibilityError,
    _OfficialTransport, _literal, _content, _database, notion_url, MCP_URL, RECONNECT_MESSAGE,
)

DB_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DS_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PAGE_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
DB = "https://www.notion.so/" + DB_ID.replace("-", "")
DS = "collection://" + DS_ID
PAGE = "https://www.notion.so/" + PAGE_ID.replace("-", "")
MARKER = "d" * 64
KEY = "e" * 64
BINDING = {"database_url": DB, "database_id": DB_ID, "data_source_url": DS, "data_source_id": DS_ID, "title": "视频知识库 · dddddddd", "marker": MARKER}
DESCRIPTION = "由 Hermes 视频知识库建立。绑定标记：hermes-video-library:" + MARKER
SCHEMAS = {
    "notion-create-database": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "schema": {"type": "string"}}, "required": ["schema"], "additionalProperties": False},
    "notion-create-pages": {"type": "object", "properties": {"parent": {"type": "object"}, "pages": {"type": "array"}, "allow_async": {"type": "boolean"}}, "required": ["parent", "pages"], "additionalProperties": False},
    "notion-fetch": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
}
CARD = {"source_url": "https://www.douyin.com/video/1234567890123456789", "canonical_url": "https://www.douyin.com/video/1234567890123456789", "title": "测试", "platform": "douyin", "content_type": "video", "author": "", "summary": "", "points": [], "actions": [], "tags": [], "source_text": "", "evidence": "metadata", "evidence_note": "仅有元信息，未取得完整内容。"}


def result(value):
    return SimpleNamespace(isError=False, structuredContent=None, content=[SimpleNamespace(type="text", text=json.dumps(value, ensure_ascii=False))])


def database(description=DESCRIPTION):
    return {"object": "database", "id": DB_ID, "url": DB, "description": [{"text": {"content": description}}], "data_sources": [{"id": DS_ID}]}


def page(parent=DS_ID, key=KEY, body=None):
    return {"object": "page", "id": PAGE_ID, "url": PAGE, "parent": {"type": "data_source_id", "data_source_id": parent}, "properties": {"去重标记": {"rich_text": [{"text": {"content": key}}]}}, "content": _content(CARD, key) if body is None else body}


class Session:
    def __init__(self, *, drop=False, schema=None):
        self.calls = []
        self.drop = drop
        self.schema = schema or SCHEMAS
        self.saved_content = None
    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=k, inputSchema=v) for k, v in self.schema.items()])
    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.drop and name.startswith("notion-create"):
            raise TimeoutError("secret remote text")
        if name == "notion-create-database":
            return result(database())
        if name == "notion-create-pages":
            self.saved_content = args["pages"][0]["content"]
            return result({"pages": [{"url": PAGE}]})
        return result(database() if args["id"] == DB else page(body=self.saved_content))


@pytest.mark.asyncio
async def test_create_and_save_readback():
    session = Session()
    gw = NotionGateway(session)
    await gw.preflight()
    assert await gw.create_knowledge_base(MARKER) == BINDING
    assert await gw.save_card(CARD, BINDING, KEY) == {"page_url": PAGE}
    creates = [args for name, args in session.calls if name == "notion-create-pages"]
    assert len(creates) == 1 and creates[0]["parent"] == {"data_source_id": DS_ID}
    assert creates[0]["allow_async"] is False
    assert await gw.verify_saved(PAGE, BINDING, KEY, expected_card=CARD)


@pytest.mark.asyncio
async def test_unknown_mutation_is_not_retried_and_redacted():
    session = Session(drop=True)
    gw = NotionGateway(session)
    await gw.preflight()
    with pytest.raises(NotionUncertain) as exc:
        await gw.create_knowledge_base(MARKER)
    assert "secret" not in str(exc.value)
    assert [n for n, a in session.calls].count("notion-create-database") == 1


@pytest.mark.asyncio
async def test_schema_changes_fail_before_mutation():
    changed = {**SCHEMAS, "notion-create-database": {**SCHEMAS["notion-create-database"], "required": ["parent"]}}
    session = Session(schema=changed)
    with pytest.raises(NotionCompatibilityError):
        await NotionGateway(session).preflight()
    assert not session.calls


@pytest.mark.asyncio
async def test_external_schema_ref_is_never_fetched():
    changed = {**SCHEMAS, "notion-fetch": {"$ref": "https://attacker.invalid/schema"}}
    with pytest.raises(NotionCompatibilityError):
        await NotionGateway(Session(schema=changed)).preflight()


@pytest.mark.asyncio
async def test_wrong_parent_and_partial_key_never_verified():
    session = Session()
    gw = NotionGateway(session)
    await gw.preflight()
    async def wrong_parent(name, args):
        return result(database() if args["id"] == DB else page(parent=DB_ID))
    session.call_tool = wrong_parent
    assert not await gw.verify_saved(PAGE, BINDING, KEY, expected_card=CARD)
    async def partial_key(name, args):
        return result(database() if args["id"] == DB else page(key=KEY + "extra", body=_content(CARD, KEY)))
    session.call_tool = partial_key
    assert not await gw.verify_saved(PAGE, BINDING, KEY, expected_card=CARD)


@pytest.mark.asyncio
async def test_account_binding_rejected_before_save():
    session = Session()
    gw = NotionGateway(session, identity={"workspace_id": "new-account", "user_id": "new-user"})
    await gw.preflight()
    with pytest.raises(NotionNotSent):
        await gw.save_card(CARD, BINDING, KEY)
    assert not session.calls


@pytest.mark.asyncio
async def test_database_marker_mismatch_blocks_save():
    session = Session()
    gw = NotionGateway(session)
    await gw.preflight()
    async def wrong(name, args):
        return result(database("copied " + DESCRIPTION))
    session.call_tool = wrong
    with pytest.raises(NotionNotSent):
        await gw.save_card(CARD, BINDING, KEY)


def test_markdown_source_cannot_make_embeds_or_mentions():
    source = '<mention-page url="https://attacker.invalid"/>\n# command\n![img](http://127.0.0.1/)\n```\n<database>bad</database>'
    rendered = _content({"summary": source}, KEY)
    assert "<mention-page" not in rendered and "<database>" not in rendered
    assert "\n# command" not in rendered and "![img]" not in rendered
    assert "```" not in rendered


def test_markdown_database_response_and_exact_ids():
    text = 'Created database: <database url="{{' + DB + '}}"><description>' + DESCRIPTION + '</description><data-sources><data-source url="{{' + DS + '}}"></data-source></data-sources></database>'
    assert _database({}, text) == (DB, DS)
    with pytest.raises(NotionError):
        notion_url("https://www.notion.so.attacker.invalid/" + DB_ID)
    assert notion_url(DB + "?redirect=https://attacker.invalid") == DB
    assert notion_url(DB + "?source=copy_link#" + PAGE_ID) == DB
    assert notion_url(DB + "?v=" + PAGE_ID) == DB
    with pytest.raises(NotionError):
        notion_url("https://www.notion.so:not-a-port/" + DB_ID)


@pytest.mark.asyncio
async def test_markdown_verify_exact_ancestry_and_marker_line():
    session = Session()
    gw = NotionGateway(session)
    await gw.preflight()
    async def fetch(name, args):
        if args["id"] == DB:
            return result(database())
        return result({"text": 'Here is the result of "fetch" for the Page:\n<page url="{{' + PAGE + '}}"><ancestor-path><parent-data-source url="{{' + DS + '}}"/><parent-database url="{{' + DB + '}}"/></ancestor-path><content>' + _content(CARD, KEY) + '</content></page>'})
    session.call_tool = fetch
    assert await gw.verify_saved(PAGE, BINDING, KEY, expected_card=CARD)


@pytest.mark.asyncio
async def test_transport_rejects_other_origins_before_send():
    transport = _OfficialTransport()
    try:
        with pytest.raises(NotionError):
            await transport.handle_async_request(httpx.Request("POST", MCP_URL + ".attacker.invalid", headers={"Authorization": "Bearer secret"}))
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_transport_closes_401_without_reading_remote_diagnostic():
    class UnreadStream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            raise AssertionError("The remote authentication error body must not be read")
            yield b""
        async def aclose(self):
            self.closed = True
    stream = UnreadStream()
    transport = _OfficialTransport()
    await transport.transport.aclose()
    transport.transport = httpx.MockTransport(lambda request: httpx.Response(401, stream=stream))
    try:
        with pytest.raises(NotionError) as caught:
            await transport.handle_async_request(httpx.Request("POST", MCP_URL))
        assert str(caught.value) == RECONNECT_MESSAGE
        assert stream.closed
    finally:
        await transport.aclose()


@pytest.mark.parametrize("failure_at", ["initialize", "tools/list"])
def test_real_mcp_sdk_preserves_401_reconnect_message_to_cli(tmp_path, monkeypatch, capsys, failure_at):
    from unittest.mock import AsyncMock
    from video_kb import notion, cli
    calls = []
    def respond(request):
        if request.method == "GET":
            return httpx.Response(405)
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == failure_at:
            return httpx.Response(401, text="private remote authentication diagnostic")
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-notion", "version": "1"}}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        raise AssertionError("No other operation or retry is expected")
    def initialize_transport(self):
        self.transport = httpx.MockTransport(respond)
    token = AsyncMock(return_value="synthetic-access")
    monkeypatch.setattr(notion, "access_token", token)
    monkeypatch.setattr(notion, "connection_identity", lambda path: {})
    monkeypatch.setattr(_OfficialTransport, "__init__", initialize_transport)
    assert cli.main(["--state-dir", str(tmp_path / "state"), "setup", "--connect"]) == 2
    output = capsys.readouterr()
    result = json.loads(output.out.splitlines()[-1])
    assert result["status"] == "error"
    assert result["message"] == RECONNECT_MESSAGE
    assert "private remote" not in output.out + output.err
    assert calls.count(failure_at) == 1
    token.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["notion-create-database", "notion-create-pages", "readback"])
async def test_real_mcp_sdk_401_during_mutation_stays_uncertain_without_retry(tmp_path, monkeypatch, failure_at):
    from unittest.mock import AsyncMock
    from video_kb import notion
    mutations = []
    def respond(request):
        if request.method == "GET":
            return httpx.Response(405)
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            response = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "synthetic-notion", "version": "1"}}
        elif body["method"] == "tools/list":
            response = {"tools": [{"name": name, "inputSchema": schema} for name, schema in SCHEMAS.items()]}
        elif body["method"] == "tools/call":
            name = body["params"]["name"]
            args = body["params"]["arguments"]
            if name.startswith("notion-create"):
                mutations.append(name)
            if name == failure_at or (failure_at == "readback" and name == "notion-fetch" and args["id"] == PAGE):
                return httpx.Response(401, text="private remote authentication diagnostic")
            value = database() if name == "notion-fetch" else {"pages": [{"url": PAGE}]}
            response = {"content": [{"type": "text", "text": json.dumps(value)}]}
        else:
            raise AssertionError("Unexpected SDK operation")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": response})
    def initialize_transport(self):
        self.transport = httpx.MockTransport(respond)
    token = AsyncMock(return_value="synthetic-access")
    monkeypatch.setattr(notion, "access_token", token)
    monkeypatch.setattr(notion, "connection_identity", lambda path: {})
    monkeypatch.setattr(_OfficialTransport, "__init__", initialize_transport)
    with pytest.raises(NotionUncertain) as caught:
        async with notion.notion_session(tmp_path) as gateway:
            if failure_at == "notion-create-database":
                await gateway.create_knowledge_base(MARKER)
            else:
                await gateway.save_card(CARD, BINDING, KEY)
    assert RECONNECT_MESSAGE in str(caught.value)
    assert "private remote" not in str(caught.value)
    assert caught.value.candidate_url == (PAGE if failure_at == "readback" else None)
    assert mutations == ["notion-create-database" if failure_at == "notion-create-database" else "notion-create-pages"]
    token.assert_awaited_once()


@pytest.mark.asyncio
async def test_preflight_checks_save_schema_before_creating_library():
    changed = {**SCHEMAS, "notion-create-pages": {**SCHEMAS["notion-create-pages"], "required": ["unexpected"]}}
    session = Session(schema=changed)
    gateway = NotionGateway(session)
    for _ in range(2):
        with pytest.raises(NotionCompatibilityError):
            await gateway.preflight()
    assert not session.calls


@pytest.mark.asyncio
async def test_readback_failure_after_mutation_is_uncertain():
    session = Session()
    original = session.call_tool
    async def unreliable(name, args):
        if name == "notion-fetch":
            raise TimeoutError("hidden")
        return await original(name, args)
    session.call_tool = unreliable
    gw = NotionGateway(session)
    await gw.preflight()
    with pytest.raises(NotionUncertain) as raised:
        await gw.create_knowledge_base(MARKER)
    assert raised.value.candidate_url == DB
    assert [name for name, _ in session.calls] == ["notion-create-database"]


@pytest.mark.asyncio
@pytest.mark.parametrize("markdown", [False, True])
async def test_verify_saved_rechecks_library_marker_for_every_response_format(markdown):
    session = Session()
    gateway = NotionGateway(session)
    await gateway.preflight()
    calls = []
    async def fetch(name, args):
        calls.append(args["id"])
        if args["id"] == DB:
            if markdown:
                return result({"text": '<database url="' + DB + '"><description>changed marker</description><data-sources><data-source url="' + DS + '"/></data-sources></database>'})
            return result(database("changed marker"))
        return result(page())
    session.call_tool = fetch
    with pytest.raises(NotionError):
        await gateway.verify_saved(PAGE, BINDING, KEY, expected_card=CARD)
    assert calls == [DB]


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [DB_ID, None, "invalid-id"])
async def test_verify_saved_rejects_contradictory_or_missing_page_id(identifier):
    session = Session()
    gateway = NotionGateway(session)
    await gateway.preflight()
    async def fetch(name, args):
        return result(database() if args["id"] == DB else {**page(), "id": identifier})
    session.call_tool = fetch
    assert not await gateway.verify_saved(PAGE, BINDING, KEY, expected_card=CARD)


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ["structured", "json", "both"])
async def test_page_validation_error_does_not_prove_no_write(representation):
    session = Session()
    gateway = NotionGateway(session)
    await gateway.preflight()
    error = {"object": "error", "code": "validation_error", "status": 400,
             "message": "private remote diagnostic", "request_id": "fake-request"}
    rejected = result(error)
    rejected.isError = True
    if representation in {"structured", "both"}:
        rejected.structuredContent = error
    if representation == "structured":
        rejected.content = []
    original = session.call_tool
    async def reject(name, args):
        if name == "notion-create-pages":
            session.calls.append((name, args))
            return rejected
        return await original(name, args)
    session.call_tool = reject
    with pytest.raises(NotionUncertain) as caught:
        await gateway.save_card(CARD, BINDING, KEY)
    assert "private" not in str(caught.value)
    assert [name for name, _ in session.calls].count("notion-create-pages") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"code": "validation_error", "message": "private", "status": 500},
    {"code": "validation_error", "message": "private", "result": {"pages": []}},
    {"error": {"code": "validation_error", "message": "private"}},
    {"code": "internal_server_error", "message": "validation_error"},
    {"code": "validation_error"},
    {"code": "validation_error", "message": "private", "object": "async_task"},
    'validation_error: private',
    '{"code":"internal_server_error","code":"validation_error","message":"private"}',
])
async def test_ambiguous_page_errors_remain_uncertain(payload):
    session = Session()
    gateway = NotionGateway(session)
    await gateway.preflight()
    rejected = result(payload)
    if isinstance(payload, str):
        rejected.content[0].text = payload
    rejected.isError = True
    async def reject(name, args):
        return rejected
    session.call_tool = reject
    with pytest.raises(NotionUncertain) as caught:
        await gateway._call("notion-create-pages", {"parent": {}, "pages": []}, mutation=True)
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_database_validation_error_remains_uncertain():
    session = Session()
    gateway = NotionGateway(session)
    await gateway.preflight()
    async def reject(name, args):
        rejected = result({"code": "validation_error", "status": 400, "message": "private"})
        rejected.isError = True
        return rejected
    session.call_tool = reject
    with pytest.raises(NotionUncertain):
        await gateway.create_knowledge_base(MARKER)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["not_error", "conflicting_content", "multiple_blocks", "nested_structured"])
async def test_unusual_validation_error_envelopes_remain_uncertain(case):
    session = Session()
    gateway = NotionGateway(session)
    await gateway.preflight()
    error = {"code": "validation_error", "message": "private"}
    rejected = result(error)
    rejected.isError = case != "not_error"
    if case == "conflicting_content":
        rejected.structuredContent = error
        rejected.content[0].text = json.dumps({**error, "code": "internal_server_error"})
    elif case == "multiple_blocks":
        rejected.content.append(SimpleNamespace(type="text", text="partial result"))
    elif case == "nested_structured":
        rejected.structuredContent = {"error": error}
    async def reject(name, args):
        return rejected
    session.call_tool = reject
    # save_card also rejects non-error responses that merely resemble an error.
    if case == "not_error":
        original = session.call_tool
        async def fetch_or_reject(name, args):
            return result(database()) if name == "notion-fetch" else await original(name, args)
        session.call_tool = fetch_or_reject
        with pytest.raises(NotionUncertain):
            await gateway.save_card(CARD, BINDING, KEY)
    else:
        with pytest.raises(NotionUncertain):
            await gateway._call("notion-create-pages", {"parent": {}, "pages": []}, mutation=True)


def full_card():
    return {**CARD, "evidence": "asr", "summary": "摘要开头和结尾都要核对。",
            "points": ["第一个观点", "第二个观点"], "actions": ["核对原始音频"],
            "source_text": "正文开始\n" + "中段讲话。" * 7500 + "\n正文最后一句不能丢失。",
            "evidence_note": "ASR 转写，非官方字幕；尚未逐字人工核对。"}


def fetched_page(body, representation):
    if representation == "xml":
        return {"text": '<page url="' + PAGE + '"><ancestor-path><parent-data-source url="' + DS + '"/><parent-database url="' + DB + '"/></ancestor-path><content>' + body + '</content></page>'}
    value = page(body=body)
    if representation == "markdown":
        value["markdown"] = value.pop("content")
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ["content", "markdown", "xml", "structured"])
async def test_full_readback_checks_all_body_text_in_order(representation):
    expected = full_card()
    body = _content(expected, KEY)
    gateway = NotionGateway(Session())
    await gateway.preflight()
    async def fetch(name, args):
        value = database() if args["id"] == DB else fetched_page(body, representation)
        response = result(value)
        if representation == "structured":
            response.structuredContent, response.content = value, []
        return response
    gateway.session.call_tool = fetch
    assert await gateway.verify_saved(PAGE, BINDING, KEY, expected_card=expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ["content", "xml"])
@pytest.mark.parametrize("damage", ["tail", "middle", "summary", "points_order", "reliability", "marker_only"])
async def test_incomplete_or_changed_body_stays_uncertain(representation, damage):
    expected = full_card()
    body = _content(expected, KEY)
    if damage == "tail":
        body = body.replace("正文最后一句不能丢失。", "")
    elif damage == "middle":
        body = body.replace("中段讲话。" * 20, "", 1)
    elif damage == "summary":
        body = body.replace(expected["summary"], "没有真正保存摘要")
    elif damage == "points_order":
        body = body.replace("第一个观点\n\n第二个观点", "第二个观点\n\n第一个观点")
    elif damage == "reliability":
        body = body.replace(_literal(expected["evidence_note"]), "")
    else:
        body = "hermes-video-key:" + KEY
    session = Session()
    original = session.call_tool
    async def fetch(name, args):
        if name == "notion-fetch" and args["id"] == PAGE:
            return result(fetched_page(body, representation))
        return await original(name, args)
    session.call_tool = fetch
    gateway = NotionGateway(session)
    await gateway.preflight()
    with pytest.raises(NotionUncertain) as caught:
        await gateway.save_card(expected, BINDING, KEY)
    assert caught.value.candidate_url == PAGE
    assert [name for name, _ in session.calls].count("notion-create-pages") == 1
    assert not await gateway.verify_saved(PAGE, BINDING, KEY, expected_card=expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("indicator", ["metadata_only", "truncated", "unknown_block_ids", "unknown_block_count"])
async def test_readback_without_complete_body_cannot_confirm_save(indicator):
    gateway = NotionGateway(Session())
    await gateway.preflight()
    value = page()
    if indicator == "metadata_only":
        del value["content"]
    else:
        value[indicator] = {"truncated": True, "unknown_block_ids": [PAGE_ID], "unknown_block_count": 1}[indicator]
    async def fetch(name, args):
        return result(database() if args["id"] == DB else value)
    gateway.session.call_tool = fetch
    assert not await gateway.verify_saved(PAGE, BINDING, KEY, expected_card=CARD)


@pytest.mark.asyncio
async def test_no_retained_card_is_not_proof_of_a_complete_old_save():
    gateway = NotionGateway(Session())
    await gateway.preflight()
    assert not await gateway.verify_saved(PAGE, BINDING, KEY)


@pytest.mark.asyncio
async def test_documented_empty_lines_linebreaks_and_escaping_roundtrip():
    expected = {**CARD, "evidence": "full_text", "source_text": '原文的 & <符号> 和 $5^2。\n第二行。', "summary": "摘要"}
    body = _content(expected, KEY)
    body = body.replace("\n\n", "\n<empty-block/>\n").replace("\n第二行", "<br>第二行")
    body = body.replace("&lt;符号&gt;", r"\<符号\>").replace("\\.", ".").replace("\n", "\r\n")
    gateway = NotionGateway(Session())
    await gateway.preflight()
    async def fetch(name, args):
        return result(database() if args["id"] == DB else page(body=body))
    gateway.session.call_tool = fetch
    assert await gateway.verify_saved(PAGE, BINDING, KEY, expected_card=expected)


def test_math_and_citations_in_source_are_literal():
    assert _literal("$formula$ [^citation]") == r"\$formula\$ \[\^citation\]"
