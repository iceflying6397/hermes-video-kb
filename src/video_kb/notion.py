"""Small fixed-operation Notion gateway. Model/source text never selects a tool."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import html
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from . import __version__
from .oauth import MCP_URL, ConnectionError, access_token, connection_identity


class NotionError(ConnectionError):
    pass


class NotionNotSent(NotionError):
    """Validation/read failed before the first mutation request was sent."""


class NotionUncertain(NotionError):
    """A mutation may have committed. The queue must never blindly repeat it."""
    def __init__(self, message: str, *, candidate_url: str | None = None):
        super().__init__(message)
        self.candidate_url = notion_url(candidate_url) if candidate_url else None


class NotionCompatibilityError(NotionError):
    pass


TOOLS = frozenset({"notion-create-database", "notion-create-pages", "notion-fetch"})
MAX_RESPONSE = 2 * 1024 * 1024
TITLE = "视频知识库"
KEY_PREFIX = "hermes-video-key:"
SCHEMA_DDL = 'CREATE TABLE ("名称" TITLE, "原始链接" URL, "平台" RICH_TEXT, "内容状态" RICH_TEXT, "去重标记" RICH_TEXT)'
_UUID_PATTERN = r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"


def object_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(_UUID_PATTERN, value):
        raise NotionError("知识库地址无效。")
    return str(uuid.UUID(value))


def notion_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise NotionError("Notion 返回了无效地址。")
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError:
        raise NotionError("Notion 返回了无效地址。") from None
    if p.scheme != "https" or p.hostname not in {"www.notion.so", "notion.so", "www.notion.com", "notion.com"} or p.username or p.password or port not in {None, 443}:
        raise NotionError("Notion 返回了无效地址。")
    # Copied Notion URLs often carry source/view parameters or a selected block.
    # Derive a fresh canonical URL from the verified host and terminal UUID;
    # neither query nor fragment is forwarded to Notion or the caller.
    last = p.path.strip("/").split("/")[-1]
    match = re.search(r"(" + _UUID_PATTERN + r")$", last)
    if not match:
        raise NotionError("Notion 返回了无效地址。")
    return "https://www.notion.so/" + object_id(match.group(1)).replace("-", "")


def source_id(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("collection://"):
        raise NotionError("Notion 数据库标识无效。")
    return object_id(value.removeprefix("collection://"))


def _marker(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32,64}", value):
        raise NotionError("本地任务标记无效。")
    return value


def _schema_safe(schema) -> None:
    if len(json.dumps(schema)) > 300000:
        raise NotionCompatibilityError("Notion 接口发生变化，请更新 Skill。")
    def visit(item, depth=0):
        if depth > 30:
            raise NotionCompatibilityError("Notion 接口发生变化，请更新 Skill。")
        if isinstance(item, dict):
            if any(ref in item and not str(item[ref]).startswith("#") for ref in ("$ref", "$dynamicRef", "$recursiveRef")) or "$id" in item:
                raise NotionCompatibilityError("Notion 接口包含不支持的外部引用，已停止。")
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
    visit(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise NotionCompatibilityError("Notion 接口发生变化，请更新 Skill。") from None


def _result(result) -> tuple[dict, str]:
    """Extract JSON or plain Notion markdown without executing instructions."""
    if getattr(result, "isError", False):
        raise NotionError("Notion 没有确认本次操作，请检查连接或权限。")
    structured = getattr(result, "structuredContent", None)
    texts = [block.text for block in getattr(result, "content", []) if getattr(block, "type", None) == "text"]
    if len("".join(texts).encode()) > MAX_RESPONSE:
        raise NotionError("Notion 返回内容过大，已停止。")
    value = structured if isinstance(structured, dict) else {}
    for text in texts:
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                value = {**value, **decoded}
            else:
                value.setdefault("text", text)
        except ValueError:
            value.setdefault("text", text)
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_RESPONSE:
        raise NotionError("Notion 返回内容过大，已停止。")
    text = value.get("result", value.get("text", value.get("content", "")))
    return value, text if isinstance(text, str) else ""


def _tag_url(text: str, tag: str, *, root=False) -> str:
    # Only exact tag attributes, never scan arbitrary https links out of source text.
    prefix = r"^[^<]{0,2048}" if root else ""
    pattern = prefix + r"<" + tag + r'\b[^>]*?\burl="(?:\{\{)?([^"{}]+)(?:\}\})?"[^>]*>'
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        raise NotionError("无法确认 Notion 返回的知识库结构，已停止。")
    return html.unescape(matches[0])


def _database(value: dict, text: str) -> tuple[str, str]:
    if value.get("object") == "database" and isinstance(value.get("data_sources"), list) and len(value["data_sources"]) == 1:
        db = notion_url(value.get("url", ""))
        if object_id(value.get("id")) != object_id(db.rsplit("/", 1)[-1]):
            raise NotionError("Notion 返回的数据库标识不一致。")
        return db, "collection://" + object_id(value["data_sources"][0].get("id"))
    # Official MCP renders databases as a database tag enclosing a data-sources section.
    db = notion_url(_tag_url(text, "database", root=True))
    sections = re.findall(r"<data-sources>(.*?)</data-sources>", text, re.S)
    if len(sections) != 1:
        raise NotionError("无法确认 Notion 数据源，已停止。")
    ds = "collection://" + source_id(_tag_url(sections[0], "data-source"))
    return db, ds


def _binding(binding: dict) -> tuple[str, str]:
    if not isinstance(binding, dict):
        raise NotionError("还没有绑定知识库，请先连接。")
    db, ds = notion_url(binding.get("database_url")), "collection://" + source_id(binding.get("data_source_url"))
    if object_id(binding.get("database_id")) != object_id(db.rsplit("/", 1)[-1]) or object_id(binding.get("data_source_id")) != source_id(ds):
        raise NotionError("本地知识库绑定不一致，请重新检查连接。")
    _marker(binding.get("marker"))
    return db, ds


def _literal(text: str) -> str:
    # Notion's extended markdown has executable-looking XML embeds/mentions. Escape all
    # source-derived punctuation and URLs, including newlines that introduce new blocks.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    text = html.escape(text, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~>$^\-])", r"\\\1", text)


def _content(card: dict, key: str) -> str:
    sections = [f"{KEY_PREFIX}{_marker(key)}", "# 摘要", _literal(card.get("summary") or "暂未生成摘要，见内容状态。"), "# 关键内容"]
    sections.extend(_literal(x) for x in card.get("points", []))
    sections += ["# 可行动项"] + [_literal(x) for x in card.get("actions", [])]
    sections += ["# 内容状态", _literal(card.get("evidence_note", "")), "# 来源文本", _literal(card.get("source_text", ""))]
    return "\n\n".join(sections)


def _content_lines(markdown: str) -> tuple[str, ...] | None:
    """Compare all literal text, allowing only documented representation changes.

    Notion strips plain empty lines and renders rich-text line breaks as <br>.
    Decode one level of escaping, so literal source markup remains distinguishable
    from actual Notion embeds. Do not remove words, reorder lines, or compare only
    a prefix/suffix: a missing middle paragraph must fail just like a missing tail.
    https://developers.notion.com/guides/data-apis/enhanced-markdown
    """
    if not isinstance(markdown, str) or len(markdown.encode()) > MAX_RESPONSE:
        return None
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n")
    markdown = re.sub(r"(?m)^[ \t]*<empty-block\s*/>[ \t]*$", "", markdown)
    markdown = markdown.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    # This writer emits only headings and literal paragraphs. Unexpected embeds,
    # unknown-block placeholders, and other rich structures are not equivalent.
    if re.search(r"(?<!\\)<[A-Za-z/!][^>]*>", markdown):
        return None
    markdown = re.sub(r"\\([\\`*_{}\[\]()#+.!|~<>$^\-])", r"\1", markdown)
    markdown = html.unescape(markdown)
    return tuple(line.rstrip(" \t") for line in markdown.split("\n") if line.strip())


def _verified_content(value: dict, text: str, expected_card: dict, key: str) -> bool:
    # Official MCP reports omitted subtrees explicitly. A page that exists but
    # was not read in full must stay recoverable and must not clear local text.
    if value.get("truncated") or value.get("unknown_block_ids") or value.get("unknown_block_count"):
        return False
    candidates = []
    for field in ("content", "markdown"):
        body = value.get(field)
        if isinstance(body, str):
            candidates.append(body)
    contents = re.findall(r"<content>(.*?)</content>", text, re.S)
    if contents:
        if len(contents) != 1:
            return False
        candidates.append(contents[0])
    # A structured Page object ordinarily contains properties, not block text.
    # Never interpret those metadata-only responses as proof of a complete save.
    if not candidates:
        return False
    expected = _content_lines(_content(expected_card, key))
    return expected is not None and all(_content_lines(body) == expected for body in candidates)


def _page_url(value: dict, text: str) -> str:
    pages = value.get("pages", value.get("results"))
    if isinstance(pages, list) and len(pages) == 1 and isinstance(pages[0], dict):
        return notion_url(pages[0].get("url", pages[0].get("page_url", "")))
    if value.get("object") == "page":
        return notion_url(value.get("url", ""))
    if "page_url" in value:
        return notion_url(value["page_url"])
    return notion_url(_tag_url(text, "page", root=True))


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream
    async def __aiter__(self):
        length = 0
        async for chunk in self.stream:
            length += len(chunk)
            if length > MAX_RESPONSE:
                raise NotionError("Notion 返回内容过大，已停止。")
            yield chunk
    async def aclose(self):
        await self.stream.aclose()


class _OfficialTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.transport = httpx.AsyncHTTPTransport(retries=0)
    async def handle_async_request(self, request):
        if str(request.url) != MCP_URL:
            raise NotionError("已阻止向非官方地址发送连接资料。")
        response = await self.transport.handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise NotionError("Notion 请求发生重定向，已安全停止。")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise NotionError("Notion 返回了不支持的压缩内容，已停止。")
        response.stream = _BoundedStream(response.stream)
        return response
    async def aclose(self):
        await self.transport.aclose()


class NotionGateway:
    def __init__(self, session, *, identity=None):
        self.session = session
        self.identity = identity
        self.schemas: dict[str, dict] = {}
        self._ready = False

    async def preflight(self) -> None:
        if self._ready:
            return
        self.schemas = {}
        try:
            cursor, seen = None, set()
            for _ in range(6):
                result = await self.session.list_tools(cursor=cursor) if cursor else await self.session.list_tools()
                for tool in result.tools:
                    if tool.name in TOOLS:
                        _schema_safe(tool.inputSchema)
                        if tool.name in self.schemas:
                            raise NotionCompatibilityError("Notion 返回了重复的操作定义，已停止。")
                        self.schemas[tool.name] = tool.inputSchema
                cursor = getattr(result, "nextCursor", None)
                if set(self.schemas) == TOOLS or not cursor:
                    break
                if cursor in seen:
                    raise NotionCompatibilityError("Notion 返回了重复的接口分页，已停止。")
                seen.add(cursor)
            if set(self.schemas) != TOOLS:
                raise NotionCompatibilityError("当前 Notion 连接未提供创建知识库所需功能，请检查官方连接权限或更新 Skill。")
            # Validate the exact plans before any mutation. Live schema is authoritative.
            self._validate("notion-create-database", self._create_arguments("0" * 32))
            self._validate("notion-fetch", {"id": "https://www.notion.so/" + "0" * 32})
            probe = {"parent": {"data_source_id": "00000000-0000-0000-0000-000000000000"}, "pages": [{"properties": {"名称": "检查", "原始链接": "https://www.douyin.com/video/1234567890123456789", "平台": "douyin", "内容状态": "metadata", "去重标记": "0" * 64}, "content": "检查"}]}
            if "allow_async" in self.schemas["notion-create-pages"].get("properties", {}):
                probe["allow_async"] = False
            self._validate("notion-create-pages", probe)
            self._ready = True
        except NotionError:
            raise
        except Exception:
            raise NotionError("暂时无法确认 Notion 连接，请稍后重试。") from None

    def _validate(self, name: str, args: dict) -> None:
        if name not in TOOLS or name not in self.schemas:
            raise NotionCompatibilityError("当前 Notion 操作未经预检，已停止。")
        try:
            Draft202012Validator(self.schemas[name]).validate(args)
        except Exception:
            raise NotionCompatibilityError("Notion 接口已改变，此版本不能安全继续，请先更新 Skill。") from None

    def _create_arguments(self, marker: str) -> dict:
        props = self.schemas.get("notion-create-database", {}).get("properties", {})
        title = TITLE + " · " + _marker(marker)[:8]
        description = "由 Hermes 视频知识库建立。绑定标记：hermes-video-library:" + marker
        if "schema" in props:
            return {"title": title, "description": description, "schema": SCHEMA_DDL}
        if "properties" in props:
            return {"title": [{"type": "text", "text": {"content": title}}], "description": [{"type": "text", "text": {"content": description}}], "properties": {"名称": {"type": "title", "title": {}}, "原始链接": {"type": "url", "url": {}}, "平台": {"type": "rich_text", "rich_text": {}}, "内容状态": {"type": "rich_text", "rich_text": {}}, "去重标记": {"type": "rich_text", "rich_text": {}}}}
        raise NotionCompatibilityError("Notion 的知识库创建接口不兼容，请更新 Skill。")

    async def _call(self, name: str, args: dict, *, mutation=False) -> tuple[dict, str]:
        try:
            self._validate(name, args)
        except NotionError as exc:
            if mutation:
                raise NotionNotSent(str(exc)) from None
            raise
        try:
            result = await asyncio.wait_for(self.session.call_tool(name, args), 60)
            return _result(result)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if mutation:
                # Even validation_error is not a general atomicity guarantee. The
                # official docs promise no writes for certain content parse errors,
                # but do not expose a distinct machine code for that subset.
                raise NotionUncertain("本次操作可能已经在 Notion 完成，已暂停自动重试，避免重复保存。请先核对。") from None
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, NotionError):
                raise
            raise NotionError("暂时无法读取 Notion，请稍后重试。") from None

    async def create_knowledge_base(self, marker: str) -> dict:
        try:
            marker = _marker(marker)
            arguments = self._create_arguments(marker)
        except NotionError as exc:
            raise NotionNotSent(str(exc)) from None
        value, text = await self._call("notion-create-database", arguments, mutation=True)
        db = None
        try:
            db, ds = _database(value, text)
            binding = {"database_url": db, "database_id": object_id(db.rsplit("/", 1)[-1]), "data_source_url": ds, "data_source_id": source_id(ds), "title": TITLE + " · " + marker[:8], "marker": marker, **(self.identity or {})}
            await self.verify_binding(binding)
            return binding
        except Exception:
            raise NotionUncertain("Notion 可能已创建知识库，但还未核实。请先核对，程序不会再建一个。", candidate_url=db) from None

    async def verify_binding(self, binding: dict) -> bool:
        db, ds = _binding(binding)
        if self.identity and any(binding.get(k) != v for k, v in self.identity.items()):
            raise NotionError("当前 Notion 账号或空间与原知识库不同，已停止保存。")
        value, text = await self._call("notion-fetch", {"id": db})
        actual_db, actual_ds = _database(value, text)
        if (actual_db, actual_ds) != (db, ds):
            raise NotionError("知识库绑定不匹配，已停止保存。")
        marker = "hermes-video-library:" + binding["marker"]
        # Creation description is fixed; do not accept arbitrary substrings elsewhere.
        description = value.get("description")
        if isinstance(description, list):
            description = "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in description if isinstance(x, dict))
        expected = "由 Hermes 视频知识库建立。绑定标记：" + marker
        if isinstance(description, str):
            valid = description == expected
        else:
            # Supported metadata wrappers only, before the data-source schema or page
            # content. Never use a loose substring match against an entire document.
            metadata_text = re.split(r"<(?:data-sources|content)>", text, maxsplit=1)[0]
            descriptions = re.findall(r"<(?:description|database-description)>\s*(.*?)\s*</(?:description|database-description)>", metadata_text, re.S)
            descriptions += re.findall(r"^(?:The description of this Database is: |Description: )(.+)$", metadata_text, re.M)
            valid = descriptions == [expected]
        if not valid:
            raise NotionError("无法确认这是本程序创建的知识库，已停止保存。")
        return True

    async def recover_knowledge_base(self, database_url: str, marker: str) -> dict:
        """Explicit user-supplied URL only; no broad workspace search or new mutation."""
        db = notion_url(database_url)
        value, text = await self._call("notion-fetch", {"id": db})
        actual_db, ds = _database(value, text)
        if actual_db != db:
            raise NotionError("返回的知识库与所选地址不同。")
        binding = {"database_url": db, "database_id": object_id(db.rsplit("/", 1)[-1]), "data_source_url": ds, "data_source_id": source_id(ds), "title": TITLE + " · " + _marker(marker)[:8], "marker": marker, **(self.identity or {})}
        await self.verify_binding(binding)
        return binding

    async def save_card(self, card: dict, binding: dict, key: str) -> dict:
        from .cards import validate_card
        try:
            card = validate_card(card)
            _, ds = _binding(binding)
            key = _marker(key)
            await self.verify_binding(binding)
        except Exception:
            raise NotionNotSent("尚未发送保存请求：内容或知识库连接未通过核对，请检查后重试。") from None
        args = {"parent": {"data_source_id": source_id(ds)}, "pages": [{"properties": {"名称": _literal(card["title"]), "原始链接": card["canonical_url"], "平台": card["platform"], "内容状态": card["evidence"], "去重标记": key}, "content": _content(card, key)}]}
        props = self.schemas["notion-create-pages"].get("properties", {})
        if "allow_async" in props:
            args["allow_async"] = False
        value, text = await self._call("notion-create-pages", args, mutation=True)
        url = None
        try:
            url = _page_url(value, text)
            if not await self.verify_saved(url, binding, key, expected_card=card):
                raise NotionError("保存后核对失败。")
            return {"page_url": url}
        except Exception:
            raise NotionUncertain("Notion 可能已保存内容，但还未核实。请先核对，程序不会重复保存。", candidate_url=url) from None

    async def verify_saved(self, page_url: str, binding: dict, key: str, *, expected_card: dict | None = None) -> bool:
        if expected_card is None:
            return False
        from .cards import validate_card
        try:
            validate_card(expected_card)
        except Exception:
            return False
        db, ds = _binding(binding)
        if self.identity and any(binding.get(k) != v for k, v in self.identity.items()):
            raise NotionError("当前 Notion 账号或空间与原知识库不同，已停止核对。")
        key = _marker(key)
        url = notion_url(page_url)
        # Recovery must check the same library identity as an ordinary save, even
        # when the page still carries a copied task key or its data source moved.
        await self.verify_binding(binding)
        value, text = await self._call("notion-fetch", {"id": url})
        if "id" in value or value.get("object") == "page":
            try:
                if object_id(value.get("id")) != object_id(url.rsplit("/", 1)[-1]):
                    return False
            except NotionError:
                return False
        if value.get("object") == "page":
            if notion_url(value.get("url", "")) != url:
                return False
            parent = value.get("parent", {})
            if parent.get("type") != "data_source_id" or object_id(parent.get("data_source_id")) != source_id(ds):
                return False
            properties = value.get("properties", {})
            key_property = properties.get("去重标记")
            if isinstance(key_property, dict):
                key_property = "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in key_property.get("rich_text", []) if isinstance(x, dict))
            return key_property == key and _verified_content(value, text, expected_card, key)
        if notion_url(_tag_url(text, "page", root=True)) != url:
            return False
        paths = re.findall(r"<ancestor-path>(.*?)</ancestor-path>", text, re.S)
        contents = re.findall(r"<content>(.*?)</content>", text, re.S)
        if len(paths) != 1 or len(contents) != 1:
            return False
        try:
            parent_ds = "collection://" + source_id(_tag_url(paths[0], "parent-data-source"))
            parent_db = notion_url(_tag_url(paths[0], "parent-database"))
        except NotionError:
            return False
        lines = contents[0].strip().splitlines()
        return (parent_ds == ds and parent_db == db and bool(lines) and lines[0] == KEY_PREFIX + key
                and _verified_content(value, text, expected_card, key))


@asynccontextmanager
async def notion_session(state_dir: Path, *, interactive=False, open_browser=False, on_authorize=None):
    token = await access_token(state_dir, interactive=interactive, open_browser=open_browser, on_authorize=on_authorize)
    completed = False
    try:
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token, "User-Agent": "hermes-video-kb/2.0", "Accept-Encoding": "identity"}, timeout=httpx.Timeout(60, connect=20), follow_redirects=False, trust_env=False, transport=_OfficialTransport()) as client:
            async with streamable_http_client(MCP_URL, http_client=client, terminate_on_close=False) as (reader, writer, _):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=65), client_info=Implementation(name="hermes-video-kb", version=__version__)) as session:
                    await session.initialize()
                    gateway = NotionGateway(session, identity=connection_identity(state_dir))
                    await gateway.preflight()
                    yield gateway
                    completed = True
    except NotionError:
        if completed:
            return
        raise
    except BaseException as exc:
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        if completed:
            return
        # AnyIO task groups can wrap safe application errors. Never expose raw nested errors.
        def find_safe(error):
            if isinstance(error, NotionError):
                return error
            for item in getattr(error, "exceptions", []):
                if found := find_safe(item):
                    return found
            return None
        if found := find_safe(exc):
            raise found from None
        raise NotionError("Notion 连接中断，请稍后检查保存状态。") from None
