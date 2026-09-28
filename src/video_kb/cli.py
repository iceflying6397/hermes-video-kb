"""Human-readable fixed operations. External text never becomes agent instructions."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import sys
import time

from . import __version__
from .cards import validate_card, notion_page_url
from .state import Store, StateError, default_state_dir, job_key, private_dir

DISCLOSURE = (
    "连接后，我会在你选择的 Notion 工作区新建“视频知识库”，以后把你发来的公开链接保存到这里。"
    "你将在 Notion 官方页面登录和授权，无需把密码或密钥发给我，也无需服务器。"
    "官方连接的权限可能覆盖你在该工作区可访问的内容；本助手的固定流程只向自己创建的知识库新增笔记，"
    "不提供删除、移动或修改其他页面的功能。这个限制不等于电脑上所有程序都被隔离。"
    "连接凭据留在本机的受限文件中；来源内容会交给你已配置的模型服务整理，并写入 Notion。"
    "连接会持续有效，直到你断开；可在这里暂停或断开，也可在 Notion 设置中撤销连接。"
    "首次连接需在运行 Hermes 的电脑上完成浏览器授权。"
)


def result(status: str, message: str, **extra) -> dict:
    return {"status": status, "message": message, **extra}


def public_job(item: dict) -> dict:
    # Titles/transcripts/model output do not go back into the operational agent context.
    if item["status"] == "saved":
        message = "已保存到知识库。"
        if item.get("evidence") in {"metadata", "none"}:
            message = "已保存链接和页面信息，尚未取得正文；这不是视频逐字稿。"
        elif item.get("needs_review", True):
            message = "已保存可读取的内容，内容不完整或尚未生成摘要，需要核对。"
        elif not item.get("content_verified", False):
            message = "这是此前已保存的笔记；旧版未核对完整正文，本次没有改写或新建。"
        return result("saved", message, page_url=notion_page_url(item["page_url"]),
                      evidence=item.get("evidence"), needs_review=item.get("needs_review", True))
    if item["status"] == "uncertain":
        message = "上次保存没有收到完整确认，可能已经保存。已停止重复写入；"
        message += ("继续处理时会先核对已保留的页面位置或同源任务。" if item.get("candidate_page_url") or item.get("duplicate_of")
                    else "请在知识库核对后，用页面链接完成确认。")
        return result("uncertain", message, job_id=item.get("key"))
    if item["status"] == "waiting_summary":
        from .providers import _FAILURE_MESSAGES
        safe_detail = item.get("summary_error", "")
        if safe_detail not in _FAILURE_MESSAGES.values():
            safe_detail = ""
        if item.get("tries", 0) >= 3:
            return result("waiting_summary", "原文仍保留在本机，尚未写入 Notion。摘要已尝试三次，请先检查模型是否可用；恢复后可以明确要求再试这个任务。" + safe_detail, job_id=item.get("key"))
        return result("waiting_summary", "原文已保留在本机，摘要暂未生成，尚未写入 Notion。稍后继续处理即可，无需重新提供正文。" + safe_detail, job_id=item.get("key"))
    return result(item["status"], item.get("last_error") or "任务已保留在本机，可稍后重试。", job_id=item.get("key"))


async def connect(store: Store, *, open_browser: bool) -> dict:
    from .notion import notion_session, NotionNotSent, NotionUncertain
    async with notion_session(store.root, interactive=True, open_browser=open_browser) as gateway:
        await gateway.preflight()
        if store.data.get("library_attempt") and not store.data.get("binding"):
            if store.data["library_attempt"].get("candidate_url"):
                return result("uncertain", "Notion 连接已确认，上次知识库的位置已保留。继续处理时会自动核对，不会重复创建。")
            return result("uncertain", "Notion 连接已确认，但上次创建知识库的结果还没有核对。请用 recover 和知识库页面链接核对，不会重复创建。")
        if store.data.get("binding"):
            if not await gateway.verify_binding(store.data["binding"]):
                return result("blocked", "当前 Notion 账户不能确认原知识库，请连接原来的账户；本机任务已保留。")
        else:
            marker = secrets.token_hex(16)
            store.data["library_attempt"] = {"marker": marker, "status": "writing", "started_at": time.time()}
            store.save()
            try:
                binding = await gateway.create_knowledge_base(marker)
                notion_page_url(binding["database_url"])
                store.data["binding"] = binding
                store.data["library_attempt"] = None
            except NotionNotSent:
                store.data["library_attempt"] = None
                store.save()
                raise
            except NotionUncertain as exc:
                store.data["library_attempt"]["status"] = "uncertain"
                if exc.candidate_url:
                    store.data["library_attempt"]["candidate_url"] = notion_page_url(exc.candidate_url)
                store.save()
                raise
            except BaseException:
                store.data["library_attempt"]["status"] = "uncertain"
                store.save()
                raise
        store.data["connected"] = True
        store.save()
        return result("connected", "已连接并核对专用知识库。可以发送链接；具体视频还需通过内容获取与转写检查。",
                      knowledge_base_url=store.data["binding"]["database_url"])


def mark_saved(store: Store, item: dict, page_url: str):
    card = item.get("card") or {}
    item.update(status="saved", page_url=notion_page_url(page_url),
                evidence=card.get("evidence", item.get("evidence", "none")),
                needs_review=card.get("evidence") not in {"full_text", "asr"} or not card.get("summary"),
                content_verified=True, saved_at=time.time())
    item.pop("card", None)
    item.pop("imported_card", None)
    item.pop("candidate_page_url", None)
    item.pop("summary_pending", None)
    item.pop("summary_error", None)
    item.pop("last_error", None)
    item["url"] = item.get("canonical_url", item["url"])
    store.save()


def reuse_saved(store: Store, item: dict, other: dict) -> dict:
    """A duplicate describes the existing remote note, never the unsaved new card."""
    item.update(status="saved", page_url=notion_page_url(other["page_url"]),
                evidence=other.get("evidence", "none"),
                needs_review=other.get("needs_review", True),
                content_verified=other.get("content_verified", False),
                saved_at=other.get("saved_at", time.time()))
    for field in ("card", "imported_card", "candidate_page_url", "summary_pending", "last_error"):
        item.pop(field, None)
    item["url"] = item.get("canonical_url", item["url"])
    store.save()
    output = public_job(item)
    output["status"] = "duplicate"
    output["message"] = "这条内容已有笔记，本次没有新建或改写。" + output["message"]
    return output


def original_job(store: Store, item: dict) -> dict:
    seen = set()
    while item.get("duplicate_of"):
        key = item["duplicate_of"]
        if key in seen or len(seen) >= 10 or key not in store.data["jobs"]:
            raise StateError("重复来源的恢复记录异常，已保留任务，未重新创建。")
        seen.add(key)
        other = store.data["jobs"][key]
        if (other.get("canonical_url") != item.get("canonical_url") or
                other.get("content_variant", "") != item.get("content_variant", "")):
            raise StateError("重复来源的恢复记录不一致，已停止核对。")
        item = other
    return item


def needs_page_link(store: Store, item: dict) -> bool:
    if item["status"] != "uncertain":
        return False
    try:
        original = original_job(store, item)
        return original["status"] != "saved" and not original.get("candidate_page_url")
    except StateError:
        return True


async def process(store: Store, key: str, item: dict) -> dict:
    from .extract import extract_url
    from .summarize import summarize_card
    from .network import normalize_source_url
    from .runtime_settings import read_settings
    from .notion import notion_session, NotionNotSent, NotionUncertain
    if item["status"] in {"saved", "uncertain"}:
        return public_job(item)
    if store.data["paused"]:
        return result("paused", "自动保存已暂停，链接已加入本机队列。")
    if not store.data.get("connected") or not store.data.get("binding"):
        return result("needs_connection", "链接已保留，请先连接 Notion。")
    if item.get("tries", 0) >= 3:
        return result("needs_review", "这条任务已尝试三次，已停止自动重试。请检查来源、模型或连接；恢复后可以明确要求再试这个任务。", job_id=key)
    item.update(status="processing", tries=item.get("tries", 0) + 1, key=key)
    store.save()
    try:
        card = item.get("card")
        if card is None:
            cache = private_dir(store.root / "cache")
            card = item.get("imported_card") or extract_url(item["url"], cache_dir=cache, config=read_settings(store.root))
            validate_card(card)
            # Persist expensive extraction before making the model request. A crash
            # must resume from the transcript, rather than downloading/transcribing again.
            item["card"] = card
            item["summary_pending"] = card["evidence"] in {"full_text", "partial", "asr"}
            item.pop("imported_card", None)
            store.save()
        if item.get("summary_pending") or not card.get("summary"):
            card = summarize_card(card)
            validate_card(card)
            item["card"] = card
            item.pop("imported_card", None)
            item["summary_pending"] = card["evidence"] in {"full_text", "partial", "asr"} and not card["summary"].strip()
            if item["summary_pending"]:
                from .providers import _FAILURE_MESSAGES
                item["summary_error"] = next((text for text in _FAILURE_MESSAGES.values()
                                               if card["evidence_note"].endswith(text)), "")
                item["status"] = "waiting_summary"
                store.save()
                return public_job(item)
        canonical = normalize_source_url(card["canonical_url"])
        item["canonical_url"] = canonical
        # Two distinct short links can resolve to the same source. A prior attempt wins.
        for other_key, other in store.data["jobs"].items():
            if (other_key == key or other.get("canonical_url") != canonical
                    or other.get("content_variant", "") != item.get("content_variant", "")):
                continue
            if other["status"] == "saved":
                return reuse_saved(store, item, other)
            if other["status"] in {"writing", "uncertain"}:
                item.update(status="uncertain", duplicate_of=other_key,
                            dedup_key=job_key(canonical + item.get("content_variant", "")))
                store.save()
                return public_job(item)
        validate_card(card)
        async with notion_session(store.root, interactive=False) as gateway:
            await gateway.preflight()
            item.update(status="writing", dedup_key=job_key(canonical + item.get("content_variant", "")))
            store.save()  # Unknown outcomes must survive even a hard process kill.
            saved = await gateway.save_card(card, store.data["binding"], item["dedup_key"])
            page_url = notion_page_url(saved["page_url"])
            item["candidate_page_url"] = page_url
            store.save()
            if not await gateway.verify_saved(page_url, store.data["binding"], item["dedup_key"], expected_card=card):
                item["status"] = "uncertain"
                store.save()
                return public_job(item)
            mark_saved(store, item, page_url)
            return public_job(item)
    except NotionNotSent:
        item["status"] = "failed"
        store.save()
        raise
    except NotionUncertain as exc:
        item["status"] = "uncertain"
        if exc.candidate_url:
            item["candidate_page_url"] = notion_page_url(exc.candidate_url)
        store.save()
        return public_job(item)
    except BaseException as exc:
        if item["status"] == "saved":
            return public_job(item)
        item["status"] = "uncertain" if item["status"] == "writing" else "failed"
        from .network import SourceError
        if isinstance(exc, SourceError):
            item["last_error"] = str(exc)
        store.save()
        raise


async def recover(store: Store, job_id: str | None, page_url: str | None) -> dict:
    from .notion import notion_session, notion_url
    if page_url:
        page_url = notion_url(page_url)
    async with notion_session(store.root, interactive=False) as gateway:
        await gateway.preflight()
        pending = store.data.get("library_attempt")
        if pending and not store.data.get("binding"):
            page_url = page_url or pending.get("candidate_url")
            if not page_url:
                return result("needs_input", "请在 Notion 找到上次新建的视频知识库，将数据库页面链接交给我核对。不会重复创建。")
            binding = await gateway.recover_knowledge_base(page_url, pending["marker"])
            if not binding:
                return result("uncertain", "暂未找到能够核对的知识库，请稍后再核对。不会再次创建。")
            notion_page_url(binding["database_url"])
            store.data.update(binding=binding, library_attempt=None, connected=True)
            store.save()
            return result("connected", "已找回并核对知识库。", knowledge_base_url=binding["database_url"])
        if not job_id or job_id not in store.data["jobs"]:
            return result("needs_input", "请提供待确认的任务编号及对应 Notion 页面链接。")
        item = store.data["jobs"][job_id]
        if item["status"] != "uncertain":
            return public_job(item)
        original = original_job(store, item)
        if original is not item and original["status"] == "saved":
            return reuse_saved(store, item, original)
        candidate = page_url or original.get("candidate_page_url")
        if not candidate:
            return result("needs_input", "请在知识库找到这条笔记，将页面链接交给我核对。")
        notion_page_url(candidate)
        expected = original.get("card")
        if not expected:
            return result("uncertain", "旧任务缺少本地原稿，暂时无法核对完整正文；已保留记录，不会重复创建。", job_id=job_id)
        if not await gateway.verify_saved(candidate, store.data["binding"], original.get("dedup_key", job_id), expected_card=expected):
            return result("uncertain", "页面位置或完整正文尚未核对通过，已保留本地原稿和待确认状态。")
        mark_saved(store, original, candidate)
        if original is not item:
            return reuse_saved(store, item, original)
        return public_job(item)


async def run(args) -> dict:
    from .runtime_settings import read_settings
    if args.command == "setup" and not args.connect:
        from .providers import check_provider
        from .asr import check_asr
        provider = check_provider()
        asr = check_asr()
        return result("ready_to_connect" if provider["available"] else "limited_setup", DISCLOSURE,
                      summary=provider, video_transcription=asr["available"], transcription=asr,
                      capability_note="原生文字优先；没有字幕时从已核验的同条视频取得媒体并在本机转写。当前可用性见 transcription，预检不等于实测通过。",
                      next_step="setup --connect --open-browser" if provider["available"] else "请先确认模型兼容性；不要把收藏链接功能当作完整视频整理。")
    if args.command == "doctor":
        from .providers import check_provider
        from .asr import check_asr
        provider = check_provider()
        asr = check_asr()
        return result("compatible" if provider["available"] else "limited", provider["message"],
                      summary_available=provider["available"], video_transcription=asr["available"], transcription=asr,
                      network=read_settings(args.state_dir or default_state_dir()),
                      message_detail="这只检查本机环境与支持的配置，不代表模型接口、Notion 连接或视频提取已经实测成功。")
    store = Store(args.state_dir or default_state_dir())
    with store.locked():
        if args.command == "configure":
            from .runtime_settings import set_public_dns
            set_public_dns(store.root, args.public_dns == "on")
            return result("configured", "已启用代理假地址的公共 DNS 核验：只有检测到 198.18.0.0/15 时，才向 Cloudflare 发送公开域名；仍核验公网地址并固定连接，不发送链接参数或正文。" if args.public_dns == "on" else "已关闭公共 DNS 核验，恢复仅使用系统解析。")
        if args.command == "status":
            counts = {}
            for job in store.data["jobs"].values():
                counts[job["status"]] = counts.get(job["status"], 0) + 1
            return result("paused" if store.data["paused"] else "connected" if store.data["connected"] else "not_connected",
                          "这是本机记录的上次状态；检查当前 Notion 是否可用请运行 check。", version=__version__, jobs=counts,
                          library_status="needs_verification" if store.data.get("library_attempt") else "ready" if store.data.get("binding") else "not_created",
                          pending=[{"job_id": key, "status": item["status"], "attempts": item.get("tries", 0)}
                                   for key, item in store.data["jobs"].items() if item["status"] != "saved"],
                          knowledge_base_url=(store.data.get("binding") or {}).get("database_url"))
        if args.command == "setup":
            return await connect(store, open_browser=args.open_browser)
        if args.command == "check":
            from .notion import notion_session
            if not store.data.get("connected") or not store.data.get("binding"):
                return result("needs_connection", "还没有可以核对的已连接知识库。")
            async with notion_session(store.root, interactive=False) as gateway:
                if not await gateway.verify_binding(store.data["binding"]):
                    return result("unavailable", "当前无法确认知识库绑定，请检查原账户连接。")
            return result("verified", "刚刚确认官方连接及原知识库可访问。没有创建或修改页面。",
                          knowledge_base_url=store.data["binding"]["database_url"])
        if args.command in {"pause", "resume"}:
            store.data["paused"] = args.command == "pause"
            store.save()
            return result(args.command, "已暂停，已有笔记保留。" if args.command == "pause" else "已恢复。后续链接会继续保存，待处理任务可运行 retry。")
        if args.command == "disconnect":
            from .oauth import disconnect
            disconnect(store.root)
            store.data["connected"] = False
            store.save()
            return result("disconnected", "本机连接已清除，Notion 笔记和待处理任务保留。若要撤销官方授权，请到 Notion 设置中移除连接。")
        if args.command == "recover":
            return await recover(store, args.job_id, args.page_url)
        if args.command == "collect":
            from .network import normalize_source_url, source_urls, SourceError
            sources = source_urls(args.url)
            if len(sources) != 1:
                raise SourceError("请每次处理一个受支持链接；分享文案可以直接发送，多条链接请由助手逐条处理。")
            source = sources[0]
            url = normalize_source_url(source)
            transcript = getattr(args, "transcript_file", None)
            imported, variant = None, ""
            if transcript:
                from .transcript_import import import_transcript
                imported, variant = import_transcript(source, transcript)
            key, item = store.enqueue(url, variant=variant)
            # Some public sharing URLs contain a platform access signature. It is
            # kept locally for fetch only, never used as the dedup identity.
            if item["status"] in {"queued", "failed", "waiting_summary"}:
                item["url"] = source
                if imported and "card" not in item:
                    item["imported_card"] = imported
                    item["content_variant"] = variant
                store.save()
            return await process(store, key, item)
        if args.command == "retry":
            if store.data["paused"]:
                return result("paused", "当前已暂停，请先恢复。")
            outcomes = []
            library = store.data.get("library_attempt") or {}
            if library and not store.data.get("binding"):
                if not library.get("candidate_url"):
                    return result("needs_input", "上次创建知识库的结果仍待确认，未重复创建。请在 Notion 找到该知识库，将页面链接交给我核对。")
                recovered = await recover(store, None, None)
                if recovered["status"] != "connected":
                    return recovered
                outcomes.append(recovered)
            requested = getattr(args, "job_id", None)
            if requested:
                item = store.data["jobs"].get(requested)
                if not item:
                    return result("needs_input", "未找到这个待处理任务。")
                if item["status"] in {"saved", "uncertain"}:
                    if item["status"] == "uncertain" and (item.get("candidate_page_url") or item.get("duplicate_of")):
                        return await recover(store, requested, None)
                    return public_job(item)
                item["tries"] = 0
                store.save()
                return await process(store, requested, item)
            for key, item in list(store.data["jobs"].items()):
                if item["status"] == "uncertain" and (item.get("candidate_page_url") or item.get("duplicate_of")):
                    try:
                        outcomes.append(await recover(store, key, None))
                    except Exception:
                        outcomes.append(public_job(item))
                    if len(outcomes) >= 5:
                        break
                    continue
                if item["status"] in {"queued", "failed", "waiting_summary"} and item.get("tries", 0) < 3:
                    try:
                        outcomes.append(await process(store, key, item))
                    except Exception:
                        outcomes.append(public_job(item))
                    if len(outcomes) >= 5:
                        break
            pending = [{"job_id": key, "status": item["status"], "attempts": item.get("tries", 0),
                        "page_link_required": needs_page_link(store, item),
                        "manual_retry_required": item["status"] != "uncertain" and item.get("tries", 0) >= 3}
                       for key, item in store.data["jobs"].items() if item["status"] != "saved"]
            message = "本次最多处理五条；待确认的保存不会重复提交。"
            if pending:
                message += f"仍有 {len(pending)} 条未完成；缺少保存位置的需提供页面链接，达到尝试上限的需明确重试单条任务。"
            else:
                message += "当前没有未完成任务。"
            return result("pending" if pending else "processed", message, items=outcomes, pending=pending)
    return result("error", "不支持的操作。")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="公开链接 → Notion 视频知识库")
    p.add_argument("--state-dir", type=Path, help="隔离的本地记录目录")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="查看连接说明")
    setup.add_argument("--connect", action="store_true", help="在说明后连接官方 Notion")
    setup.add_argument("--open-browser", action="store_true")
    for name in ("status", "check", "doctor", "pause", "resume", "disconnect"):
        sub.add_parser(name)
    retry = sub.add_parser("retry")
    retry.add_argument("--job-id", help="用户要求重新尝试的单个任务；不重试结果不确定的保存")
    collect = sub.add_parser("collect")
    collect.add_argument("url")
    collect.add_argument("--transcript-file", type=Path, help="用户明确提供的字幕或文字文件；作为独立补充笔记保存")
    configure = sub.add_parser("configure", help="只在用户了解 DNS 数据去向并允许后改变网络设置")
    configure.add_argument("--public-dns", choices=("on", "off"), required=True)
    recovery = sub.add_parser("recover")
    recovery.add_argument("--job-id")
    recovery.add_argument("--page-url")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.command == "setup" and args.connect:
        print(json.dumps(result("connection_notice", DISCLOSURE), ensure_ascii=False), flush=True)
    try:
        output = asyncio.run(run(args))
        code = 0 if output["status"] not in {"error", "blocked", "uncertain"} else 2
    except (KeyboardInterrupt, SystemExit):
        output = result("interrupted", "操作已停止。未完成记录保留，下次会先核对是否已保存。")
        code = 130
    except Exception as exc:
        # Never print repr/tracebacks: HTTP, model and OAuth exceptions can contain secrets.
        from .cards import CardError
        from .network import SourceError
        from .oauth import ConnectionError
        allowed = isinstance(exc, (StateError, CardError, SourceError, ConnectionError))
        output = result("error", str(exc) if allowed else "操作未完成，记录已保留。请查看 status；若有待确认记录，先核对，勿重复创建。")
        code = 2
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
