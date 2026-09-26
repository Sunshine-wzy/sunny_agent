import asyncio
import importlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import nonebot
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.exception import NetworkError


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.bootstrap.cleanup)
        root = Path(cls.bootstrap.name)
        try:
            nonebot.get_driver()
        except ValueError:
            nonebot.init(
                driver="~none", _env_file=root / "no.env", log_level="CRITICAL"
            )
        nonebot.require("nonebot_plugin_localstore")
        nonebot.require("nonebot_plugin_apscheduler")
        import nonebot_plugin_localstore as store

        package = types.ModuleType("context_test_plugin")
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules[package.__name__] = package
        with patch.object(
            store, "get_plugin_data_file", return_value=root / "active.json"
        ):
            cls.context = importlib.import_module("context_test_plugin.context")
            cls.controller = importlib.import_module(
                "context_test_plugin.conversation_chat"
            )
            cls.messaging = importlib.import_module("context_test_plugin.messaging")
            cls.graph = importlib.import_module("context_test_plugin.graph")
            cls.builder = importlib.import_module("context_test_plugin.context.builder")
            cls.session_module = importlib.import_module(
                "context_test_plugin.context.session"
            )

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "context.sqlite3"
        self.manager = self.context.ConversationManager(self.db_path)
        self.addCleanup(self.manager.store.close)
        self.addAsyncCleanup(self.manager.cancel_title_tasks)
        self.patch = patch.object(self.context, "_manager", self.manager)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.scope = self.context.Scope("42", "group", "123")
        self.bot = Mock(self_id="42")
        self.next_id = 1000
        self.sent = []

        async def send(*args, **kwargs):
            self.next_id += 1
            self.sent.append((self.next_id, kwargs))
            return {"message_id": self.next_id}

        self.bot.send_group_msg = AsyncMock(side_effect=send)
        self.bot.send_private_msg = AsyncMock(side_effect=send)
        self.bot.call_api = AsyncMock(side_effect=send)
        self.bot.get_msg = AsyncMock(return_value={})
        self.runs = []

        async def run(event, bot, input_items, turn):
            history = await turn.session.get_items()
            self.runs.append((turn.conversation, history, input_items))
            await turn.session.add_items(
                input_items + [{"role": "assistant", "content": "模型回复"}]
            )
            return "模型回复"

        self.fake_run = run
        for method in ("run_group_chat", "run_private_chat"):
            mocked = patch.object(self.controller.chat, method, side_effect=run)
            mocked.start()
            self.addCleanup(mocked.stop)

    def event(
        self, message_id, text, *, reply=None, user=100, group=123, private=False
    ):
        message = text if isinstance(text, Message) else Message(text)
        data = {
            "time": 1,
            "self_id": 42,
            "post_type": "message",
            "message_type": "private" if private else "group",
            "sub_type": "friend" if private else "normal",
            "message_id": message_id,
            "user_id": user,
            "message": message,
            "raw_message": str(message),
            "font": 0,
            "sender": {"user_id": user, "nickname": f"user-{user}"},
            "to_me": True,
        }
        if not private:
            data["group_id"] = group
        event = (PrivateMessageEvent if private else GroupMessageEvent).model_validate(
            data
        )
        if reply is not None:
            event.reply = types.SimpleNamespace(
                message_id=reply,
                message=Message("untrusted transport snapshot"),
                sender={},
            )
        return event

    async def say(self, message_id, text, **kwargs):
        await self.controller.handle_message(
            self.event(message_id, text, **kwargs), self.bot
        )

    async def wait_for_titles(self):
        await asyncio.wait_for(asyncio.gather(*self.manager._title_tasks.values()), 2)

    async def test_title_generated_once_and_shown_in_commands_after_restart(self):
        generator = AsyncMock(return_value="  标题：“SQLite 会话持久化”  ")
        self.manager.title_generator = generator
        await self.say(1, "/new")
        original = await self.manager.get_active_session(self.scope)
        await self.say(2, "/session current")
        generator.assert_not_awaited()
        await self.say(3, "如何用 SQLite 持久化会话？")
        await self.wait_for_titles()
        current = await self.manager.get_active_session(self.scope)
        self.assertEqual(current.title, "SQLite 会话持久化")
        self.assertEqual(current, original)
        self.assertEqual(hash(current), hash(original))
        naming_input = generator.await_args.args[0]
        self.assertIn("如何用 SQLite 持久化会话", naming_input)
        self.assertIn("模型回复", naming_input)
        self.assertNotIn("/new", naming_input)
        self.assertNotIn("user-100", naming_input)
        self.assertNotIn("qq=", naming_input)
        entries = await self.manager.call("entries", current)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["content"]), 2)
        await self.say(4, "继续讨论")
        await self.wait_for_titles()
        await self.say(5, "/session list")
        self.assertIn(
            f"→ #{current.short_id} SQLite 会话持久化", str(self.sent[-1][1]["message"])
        )
        await self.say(6, "/session current")
        self.assertIn(current.title, str(self.sent[-1][1]["message"]))
        restored = self.context.ConversationManager(
            self.db_path, title_generator=generator
        )
        self.addCleanup(restored.store.close)
        self.addAsyncCleanup(restored.cancel_title_tasks)
        self.assertEqual(
            (await restored.get_active_session(self.scope)).title, current.title
        )
        restored.schedule_title(original)
        await asyncio.gather(*restored._title_tasks.values())
        generator.assert_awaited_once()

    async def test_slow_naming_does_not_block_chat_or_change_active_session(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def name(text):
            started.set()
            await release.wait()
            return "旧会话主题"

        self.manager.title_generator = AsyncMock(side_effect=name)
        await asyncio.wait_for(self.say(1, "原来的话题"), 2)
        await asyncio.wait_for(started.wait(), 2)
        old = await self.manager.get_active_session(self.scope)
        self.manager.schedule_title(old)
        await asyncio.wait_for(self.say(2, "继续原来的话题"), 2)
        await asyncio.wait_for(self.say(3, "/new"), 2)
        active = await self.manager.get_active_session(self.scope)
        release.set()
        await self.wait_for_titles()
        self.manager.title_generator.assert_awaited_once()
        self.assertEqual(await self.manager.get_active_session(self.scope), active)
        self.assertEqual(
            (await self.manager.find_session(self.scope, old.short_id)).title,
            "旧会话主题",
        )
        binding = await self.manager.lookup_message(self.scope, 1)
        self.assertEqual(binding.conversation.title, "旧会话主题")
        await self.say(4, "", reply=1)
        self.assertEqual(await self.manager.get_active_session(self.scope), old)

    async def test_naming_failures_preserve_reply_and_retry_after_next_success(self):
        for index, failure in enumerate(
            [RuntimeError("offline"), "", "[CQ:at,qq=all]"]
        ):
            with self.subTest(failure=failure):
                generator = AsyncMock(side_effect=[failure, "恢复后的会话标题"])
                self.manager.title_generator = generator
                await self.say(index * 10 + 1, "/new")
                with self.assertLogs(
                    "context_test_plugin.context.manager", level="WARNING"
                ):
                    await self.say(index * 10 + 2, "讨论上下文管理")
                    await self.wait_for_titles()
                self.assertIn("模型回复", str(self.sent[-1][1]["message"]))
                self.assertEqual(
                    (await self.manager.get_active_session(self.scope)).title, "新会话"
                )
                await self.say(index * 10 + 3, "继续")
                await self.wait_for_titles()
                self.assertEqual(
                    (await self.manager.get_active_session(self.scope)).title,
                    "恢复后的会话标题",
                )
                self.assertEqual(generator.await_count, 2)

    async def test_naming_timeout_cancels_request_and_can_retry(self):
        cancelled = asyncio.Event()

        async def slow(text):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.manager.title_generator = slow
        self.manager.title_timeout_seconds = 0.02
        with self.assertLogs("context_test_plugin.context.manager", level="WARNING"):
            await self.say(1, "讨论异步超时")
            await self.wait_for_titles()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(
            (await self.manager.get_active_session(self.scope)).title, "新会话"
        )
        self.manager.title_generator = AsyncMock(return_value="异步超时处理")
        await self.say(2, "继续")
        await self.wait_for_titles()
        self.assertEqual(
            (await self.manager.get_active_session(self.scope)).title, "异步超时处理"
        )

    async def test_empty_failed_and_explicitly_named_conversations_are_not_renamed(
        self,
    ):
        generator = AsyncMock(return_value="不应使用")
        self.manager.title_generator = generator
        empty = await self.manager.get_active_session(self.scope)
        self.manager.schedule_title(empty)
        await self.wait_for_titles()
        with patch.object(
            self.controller.chat, "run_group_chat", side_effect=RuntimeError("failed")
        ):
            await self.say(1, "失败的请求")
        self.manager.schedule_title(empty)
        await self.wait_for_titles()
        for index, title in enumerate(["AI 早报", "新会话"]):
            await self.manager.create_session(self.scope, title=title)
            await self.say(index + 2, "补充讨论")
            await self.wait_for_titles()
            self.assertEqual(
                (await self.manager.get_active_session(self.scope)).title, title
            )
        generator.assert_not_awaited()

    async def test_title_input_is_bounded_and_excludes_tools_reasoning_and_media(self):
        naming = importlib.import_module("context_test_plugin.context.naming")
        entries = [
            {
                "kind": "turn",
                "content": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "问题" * 4000},
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,secret",
                            },
                            {"type": "input_file", "file_data": "secret file"},
                        ],
                    },
                    {"type": "function_call", "name": "x", "arguments": "secret call"},
                    {"type": "function_call_output", "output": "secret result"},
                    {"type": "reasoning", "summary": "secret reasoning"},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "回复摘要"},
                        ],
                    },
                ],
            },
            {
                "kind": "external",
                "content": [{"type": "input_text", "text": "项目资料"}],
            },
        ]
        value = naming.build_title_input(entries * 10)
        self.assertLessEqual(len(value), naming.TITLE_INPUT_MAX_CHARS)
        self.assertNotIn("secret", value)
        self.assertIn("回复摘要", value)
        self.assertIn("项目资料", value)
        self.assertEqual(
            naming.normalize_title("“标题\n换行” [CQ:at,qq=all]\u202e"), "标题 换行"
        )
        self.assertEqual(
            len(naming.normalize_title("长" * 100)), naming.TITLE_MAX_CHARS
        )

    async def test_renamed_refs_keep_entry_associations_and_scope_isolation(self):
        from dataclasses import replace

        original = await self.manager.get_active_session(self.scope)
        entry = await self.manager.append_context(
            original,
            content=self.context.ContextContent.text("项目资料"),
            source=self.context.SourceInfo("test", "title"),
            idempotency_key="title",
        )
        self.manager.title_generator = AsyncMock(return_value="项目讨论")
        await self.say(1, "讨论项目")
        await self.wait_for_titles()
        fresh = await self.manager.get_active_session(self.scope)
        self.assertIn("项目资料", self.manager.title_generator.await_args.args[0])
        await self.manager.bind_message(
            fresh,
            message_id="manual",
            visible_content=self.context.ContextContent.text("入口"),
            entries=[entry],
        )
        delivery = await self.messaging.send_in_session(
            self.bot, fresh, "入口", idempotency_key="entry", entries=[entry]
        )
        self.assertTrue(delivery)
        forged = replace(fresh, scope=self.context.Scope("42", "group", "999"))
        with self.assertRaises(self.context.ContextError):
            await self.manager.call("title_entries", forged)
        with self.assertRaises(self.context.ContextError):
            await self.manager.call("set_generated_title", forged, "wrong")

    async def test_v1_migration_preserves_titles_history_and_creation_idempotency(self):
        path = self.db_path.with_name("v1.sqlite3")
        legacy = self.context.ConversationManager(path)
        original = await legacy.create_session(self.scope, idempotency_key="original")
        fixed = await legacy.create_session(self.scope, title="早报", activate=False)
        entry = await legacy.append_context(
            original,
            content=self.context.ContextContent.text("历史资料"),
            source=self.context.SourceInfo("test", "legacy"),
            idempotency_key="legacy",
        )
        legacy.store.close()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE conversations DROP COLUMN title_status")
            db.execute("PRAGMA user_version=1")
        restored = self.context.ConversationManager(path)
        self.addCleanup(restored.store.close)
        self.assertEqual(await restored.get_active_session(self.scope), original)
        self.assertIn(
            "历史资料", await restored.read_context(original, entry.entry_id, 0, 1000)
        )
        await restored.call("set_generated_title", original, "迁移后的标题")
        await restored.call("set_generated_title", fixed, "不可覆盖")
        self.assertEqual(
            (await restored.find_session(self.scope, fixed.short_id)).title, "早报"
        )
        retried = await restored.create_session(self.scope, idempotency_key="original")
        self.assertEqual(retried.title, "迁移后的标题")
        self.assertEqual(
            restored.store.db.execute("PRAGMA user_version").fetchone()[0], 2
        )

    async def test_title_runner_uses_configured_model_without_tools_or_session(self):
        config = types.SimpleNamespace(
            sunny_agent_context_title_model="configured-title-model"
        )
        with (
            patch.object(self.graph, "get_plugin_config", return_value=config),
            patch.object(
                self.graph.Runner,
                "run",
                new_callable=AsyncMock,
                return_value=types.SimpleNamespace(final_output="生成标题"),
            ) as run,
        ):
            self.assertEqual(
                await self.graph.generate_conversation_title("对话内容"), "生成标题"
            )
        args, kwargs = run.await_args
        self.assertEqual(args[1], "对话内容")
        self.assertEqual(args[0].tools, [])
        self.assertNotIn("session", kwargs)
        self.assertEqual(kwargs["max_turns"], 1)
        self.assertEqual(kwargs["run_config"].model, "configured-title-model")
        self.assertIs(kwargs["run_config"].model_provider, self.graph.model_provider)

    async def test_title_factory_wires_default_model_and_respects_disabled_setting(
        self,
    ):
        import nonebot_plugin_localstore as store

        config_type = importlib.import_module("context_test_plugin.config").Config
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                config = config_type(sunny_agent_context_auto_title_enabled=enabled)
                with (
                    patch.object(self.context, "_manager", None),
                    patch.object(
                        store,
                        "get_data_file",
                        return_value=self.db_path.with_name(
                            f"factory-{enabled}.sqlite3"
                        ),
                    ),
                    patch.object(nonebot, "get_plugin_config", return_value=config),
                    patch.object(self.graph, "get_plugin_config", return_value=config),
                    patch.object(
                        self.graph.Runner,
                        "run",
                        new_callable=AsyncMock,
                        return_value=types.SimpleNamespace(final_output="私聊主题"),
                    ) as run,
                ):
                    manager = self.context.get_manager()
                    self.addCleanup(manager.store.close)
                    self.addAsyncCleanup(manager.cancel_title_tasks)
                    await self.say(1, "私聊的问题", private=True)
                    await asyncio.gather(*manager._title_tasks.values())
                    private_scope = self.context.Scope("42", "private", "100")
                    current = await manager.get_active_session(private_scope)
                    self.assertEqual(current.title, "私聊主题" if enabled else "新会话")
                    if enabled:
                        run.assert_awaited_once()
                        self.assertEqual(
                            run.await_args.kwargs["run_config"].model,
                            self.graph.MODEL_NAME,
                        )
                    else:
                        run.assert_not_awaited()

    async def test_shutdown_cancels_naming_and_leaves_it_retryable(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(text):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.manager.title_generator = slow
        await self.say(1, "讨论任务关闭")
        await asyncio.wait_for(started.wait(), 2)
        await self.context.stop_title_tasks()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.manager._title_tasks, {})
        self.assertEqual(
            (await self.manager.get_active_session(self.scope)).title, "新会话"
        )
        self.manager.title_generator = AsyncMock(return_value="任务关闭处理")
        await self.say(2, "继续讨论")
        await self.wait_for_titles()
        self.assertEqual(
            (await self.manager.get_active_session(self.scope)).title, "任务关闭处理"
        )

    async def test_new_resume_user_and_bot_messages_and_shared_group_pointer(self):
        await self.say(1, "A-topic")
        a = await self.manager.get_active_session(self.scope)
        a_reply = self.sent[-1][0]
        await self.say(2, "/clear")
        b = await self.manager.get_active_session(self.scope)
        self.assertNotEqual(a, b)
        await self.say(3, "B-topic")
        await self.say(4, "continue A", reply=1)
        await self.say(5, "no reply needed", user=200)
        self.assertEqual([r[0] for r in self.runs], [a, b, a, a])
        await self.say(6, "/new")
        await self.say(7, "continue via bot reply", reply=a_reply)
        self.assertEqual(await self.manager.get_active_session(self.scope), a)
        self.assertIn("A-topic", json.dumps(self.runs[-1][1]))
        self.assertNotIn("B-topic", json.dumps(self.runs[-1][1]))

    async def test_quote_imports_only_selected_message_and_retains_owner(self):
        await self.say(1, "budget=500")
        a = await self.manager.get_active_session(self.scope)
        await self.say(2, "private-to-session-A")
        await self.say(3, "/new")
        b = await self.manager.get_active_session(self.scope)
        await self.say(4, "B-plan")
        await self.say(5, "/quote adjust B using this budget", reply=1)
        self.assertEqual(await self.manager.get_active_session(self.scope), b)
        self.assertEqual(
            (await self.manager.lookup_message(self.scope, 1)).conversation, a
        )
        self.assertEqual(
            (await self.manager.lookup_message(self.scope, 5)).conversation, b
        )
        prompt = json.dumps(self.runs[-1][1:], ensure_ascii=False)
        self.assertIn("budget=500", prompt)
        self.assertIn("B-plan", prompt)
        self.assertNotIn("private-to-session-A", prompt)
        self.assertNotIn("/quote", prompt)
        await self.say(6, "continue B")
        self.assertEqual(self.runs[-1][0], b)
        quote = await self.manager.read_context(b, "quote:1", 0, 1000)
        self.assertIn("budget=500", quote)
        self.assertNotIn("private-to-session-A", quote)

    async def test_restart_restores_pointer_history_and_bindings(self):
        await self.say(1, "persisted")
        a = await self.manager.get_active_session(self.scope)
        restored = self.context.ConversationManager(self.db_path)
        self.addCleanup(restored.store.close)
        self.assertEqual(await restored.get_active_session(self.scope), a)
        self.assertEqual(
            (await restored.lookup_message(self.scope, self.sent[-1][0])).conversation,
            a,
        )
        self.assertIn("persisted", json.dumps(await restored.call("entries", a)))

    async def test_private_group_and_bot_scopes_are_isolated(self):
        await self.say(1, "group secret")
        for scope in [
            self.context.Scope("42", "group", "456"),
            self.context.Scope("43", "group", "123"),
            self.context.Scope("42", "private", "100"),
        ]:
            self.assertIsNone(await self.manager.lookup_message(scope, 1))
        await self.say(2, "private question", private=True, reply=1)
        self.assertEqual(self.runs[-1][0].scope.chat_type, "private")
        self.assertNotIn("group secret", json.dumps(self.runs[-1][1:]))

    async def test_session_commands_and_poke_do_not_call_model(self):
        await self.say(1, "A")
        a = await self.manager.get_active_session(self.scope)
        await self.say(2, "/new")
        b = await self.manager.get_active_session(self.scope)
        await self.say(3, "/session list")
        self.assertIn(a.short_id, str(self.sent[-1][1]["message"]))
        await self.say(4, f"/session use {a.short_id}")
        self.assertEqual(await self.manager.get_active_session(self.scope), a)
        await self.say(5, "/session use invalid")
        self.assertEqual(await self.manager.get_active_session(self.scope), a)
        await self.say(6, "/session use", reply=2)
        self.assertEqual(await self.manager.get_active_session(self.scope), b)
        await self.say(7, "", reply=1)
        self.assertEqual(await self.manager.get_active_session(self.scope), a)
        await self.controller.new_session_from_poke(
            self.scope, self.bot, "poke:1:100:42"
        )
        c = await self.manager.get_active_session(self.scope)
        await self.controller.new_session_from_poke(
            self.scope, self.bot, "poke:1:100:42"
        )
        self.assertEqual(await self.manager.get_active_session(self.scope), c)
        self.assertNotEqual(a, c)
        self.assertEqual(len(self.runs), 1)

    async def test_duplicate_event_does_not_rerun_or_resend(self):
        await self.say(1, "once")
        await self.say(1, "once")
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(len(self.sent), 1)

    async def test_same_scope_is_ordered_while_another_group_can_run(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(event, bot, input_items, turn):
            if event.message_id == 1:
                entered.set()
                await release.wait()
            return await self.fake_run(event, bot, input_items, turn)

        with patch.object(self.controller.chat, "run_group_chat", side_effect=slow):
            first = asyncio.create_task(self.say(1, "old"))
            await entered.wait()
            a = await self.manager.call("active", self.scope)
            new = asyncio.create_task(self.say(2, "/new"))
            await asyncio.wait_for(self.say(3, "other group", group=456), 2)
            self.assertFalse(new.done())
            release.set()
            await asyncio.wait_for(asyncio.gather(first, new), 2)
        self.assertNotEqual(await self.manager.get_active_session(self.scope), a)
        self.assertEqual(
            (await self.manager.lookup_message(self.scope, 1)).conversation, a
        )

    async def test_model_failure_discards_partial_tools_but_keeps_input(self):
        async def broken(event, bot, input_items, turn):
            await turn.session.add_items(
                input_items + [{"type": "function_call", "call_id": "dangling"}]
            )
            raise RuntimeError("provider failed")

        with patch.object(self.controller.chat, "run_group_chat", side_effect=broken):
            await self.say(1, "keep this request")
        await self.say(2, "retry please")
        history = json.dumps(self.runs[-1][1], ensure_ascii=False)
        self.assertIn("keep this request", history)
        self.assertNotIn("dangling", history)
        self.assertIn("失败", history)

    async def test_unknown_quote_does_not_switch_or_trust_transport_content(self):
        await self.say(1, "current")
        active = await self.manager.get_active_session(self.scope)
        await self.say(2, "/quote explain", reply=999)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(await self.manager.get_active_session(self.scope), active)
        await self.say(3, "question", reply=999)
        self.assertNotIn("untrusted transport snapshot", json.dumps(self.runs[-1][1:]))

    async def test_external_api_does_not_activate_and_is_idempotent(self):
        a = await self.manager.get_active_session(self.scope)
        b = await self.manager.create_session(
            self.scope,
            activate=False,
            title="report",
            source_key="r",
            idempotency_key="r",
        )
        kwargs = dict(
            content=self.context.ContextContent.text("FULL REPORT"),
            source=self.context.SourceInfo("rss", "r"),
            idempotency_key="r",
        )
        first = await self.manager.append_context(b, **kwargs)
        second = await self.manager.append_context(b, **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(await self.manager.get_active_session(self.scope), a)
        with self.assertRaises(self.context.ContextError):
            await self.manager.append_context(
                b,
                **{**kwargs, "content": self.context.ContextContent.text("different")},
            )
        self.assertIn(
            "FULL REPORT", await self.manager.read_context(b, first.entry_id, 0, 1000)
        )
        self.assertNotIn(
            "FULL REPORT", await self.manager.read_context(a, first.entry_id, 0, 1000)
        )

    async def test_unknown_delivery_is_not_automatically_resent(self):
        self.bot.send_group_msg.side_effect = NetworkError("lost receipt")
        a = await self.manager.get_active_session(self.scope)
        result = await self.messaging.send_in_session(
            self.bot, a, "payload", idempotency_key="send:1"
        )
        again = await self.messaging.send_in_session(
            self.bot, a, "payload", idempotency_key="send:1"
        )
        self.assertEqual(result.status, "unknown")
        self.assertEqual(again.status, "unknown")
        self.bot.send_group_msg.assert_awaited_once()
        self.assertEqual(await self.manager.call("pending_deliveries", "42"), [])

    async def test_new_input_and_output_are_saved_only_once(self):
        await self.say(1, "one")
        await self.say(2, "two")
        a = await self.manager.get_active_session(self.scope)
        entries = await self.manager.call("entries", a)
        self.assertEqual([len(e["content"]) for e in entries], [2, 2])
        self.assertEqual(len(self.runs[-1][1]), 2)

    async def test_quote_preserves_image_without_using_real_network(self):
        message = (
            Message(MessageSegment.image("https://example.com/image.png")) + "describe"
        )
        image = {"type": "input_image", "image_url": "data:image/png;base64,AA=="}
        with patch.object(
            self.controller.chat,
            "_build_image_block",
            new_callable=AsyncMock,
            return_value=image,
        ):
            await self.say(1, message)
            await self.say(2, "/new")
            await self.say(3, "/quote compare", reply=1)
        self.assertIn("input_image", json.dumps(self.runs[-1][2]))

    async def test_registered_user_reply_triggers_without_at(self):
        await self.say(1, "registered")
        event_module = importlib.import_module("context_test_plugin.event")
        event = self.event(2, "reply", reply=1)
        event.to_me = False
        self.assertTrue(await event_module._is_to_me_or_active_group(event, self.bot))
        event.reply.message_id = 999
        self.assertFalse(await event_module._is_to_me_or_active_group(event, self.bot))

    async def test_window_keeps_complete_tool_turns_and_rejects_oversized_input(self):
        items = [
            {"role": "user", "content": "question"},
            {
                "type": "function_call",
                "call_id": "c",
                "name": "tool",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "c", "output": "result"},
            {"role": "assistant", "content": "answer"},
        ]
        entries = [{"id": "e", "kind": "turn", "content": items, "source": {}}]
        history = self.builder.build_history(
            entries, [], max_input_tokens=10000, recent_turns=1, entry_max_chars=100
        )
        self.assertEqual(history, items)
        short = self.builder.build_history(
            entries, [], max_input_tokens=100, recent_turns=1, entry_max_chars=100
        )
        self.assertEqual(short, [])
        with self.assertRaises(self.context.ContextError):
            self.builder.build_history(
                entries,
                [{"role": "user", "content": "x" * 1001}],
                max_input_tokens=100,
                recent_turns=1,
                entry_max_chars=100,
            )

    async def test_real_agents_runner_session_contract_with_fake_model(self):
        from agents import Agent, RunConfig, Runner
        from agents.items import ModelResponse
        from agents.models.interface import Model
        from agents.usage import Usage
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        calls = []

        class FakeModel(Model):
            async def get_response(self, *args, **kwargs):
                calls.append(kwargs.get("input", args[1] if len(args) > 1 else None))
                return ModelResponse(
                    output=[
                        ResponseOutputMessage(
                            id=f"m{len(calls)}",
                            role="assistant",
                            status="completed",
                            type="message",
                            content=[
                                ResponseOutputText(
                                    type="output_text",
                                    text="fake reply",
                                    annotations=[],
                                )
                            ],
                        )
                    ],
                    usage=Usage(),
                    response_id=f"r{len(calls)}",
                )

            def stream_response(self, *args, **kwargs):
                raise NotImplementedError

        agent = Agent(name="test", model=FakeModel())
        first = self.session_module.ConversationSession("a", [])
        result = await Runner.run(
            agent, "hello", session=first, run_config=RunConfig(tracing_disabled=True)
        )
        self.assertEqual(result.final_output, "fake reply")
        self.assertEqual(len(first.new_items), 2)
        second = self.session_module.ConversationSession("a", first.new_items)
        await Runner.run(
            agent,
            [{"role": "user", "content": [{"type": "input_text", "text": "followup"}]}],
            session=second,
            run_config=RunConfig(tracing_disabled=True),
        )
        self.assertEqual(len(second.new_items), 2)
        self.assertEqual(len(calls[1]), 3)
        self.assertEqual((await second.get_items(limit=0)), [])
        popped = await second.pop_item()
        self.assertEqual(popped["role"], "assistant")
        await second.clear_session()
        self.assertEqual(await second.get_items(), [])
        self.assertEqual(len(first.new_items), 2)

    async def test_timeout_releases_scope_and_preserves_the_failed_input(self):
        cancelled = asyncio.Event()

        async def never_finish(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        config = types.SimpleNamespace(sunny_agent_context_turn_timeout_seconds=0.2)
        with (
            patch.object(self.controller, "get_plugin_config", return_value=config),
            patch.object(
                self.controller.chat,
                "run_group_chat",
                side_effect=never_finish,
            ),
        ):
            await asyncio.wait_for(self.say(1, "timed out request"), 2)
        self.assertTrue(cancelled.is_set())
        await asyncio.wait_for(self.say(2, "next request"), 2)
        self.assertIn("timed out request", json.dumps(self.runs[-1][1]))

    async def test_tool_image_survives_model_failure_and_remains_a_reply_anchor(self):
        from agents import RunContextWrapper

        tool = importlib.import_module("context_test_plugin.tool")

        async def fail_after_image(event, bot, input_items, turn):
            wrapper = RunContextWrapper(
                tool.ChatContext(bot=bot, event=event, turn=turn)
            )
            result = await tool._send_generated_image(
                wrapper, {"url": "https://example.com/generated.png"}
            )
            self.assertTrue(result["sent"])
            raise RuntimeError("failed after sending image")

        with patch.object(
            self.controller.chat, "run_group_chat", side_effect=fail_after_image
        ):
            await asyncio.wait_for(self.say(1, "draw"), 2)
        binding = await self.manager.lookup_message(self.scope, 1001)
        self.assertIsNotNone(binding)
        self.assertEqual(binding.content.blocks[0]["type"], "input_image")
        entries = await self.manager.call("entries", binding.conversation)
        self.assertEqual(len(entries), 2)
        self.assertNotIn("skip_projection", entries[1])

    async def test_recovery_sends_only_pending_and_registers_receipt(self):
        conversation = await self.manager.get_active_session(self.scope)
        row = await self.manager.call(
            "prepare_delivery",
            conversation,
            "pending:1",
            self.messaging.outgoing_payload("pending"),
            self.context.ContextContent.text("pending").blocks,
            [],
            None,
        )
        unknown = await self.manager.call(
            "prepare_delivery",
            conversation,
            "unknown:1",
            self.messaging.outgoing_payload("uncertain"),
            self.context.ContextContent.text("uncertain").blocks,
            [],
            None,
        )
        await self.manager.call("delivery_status", unknown["id"], "unknown")
        await self.messaging.recover_pending(self.bot)
        await self.messaging.recover_pending(self.bot)
        self.bot.send_group_msg.assert_awaited_once()
        self.assertEqual(
            (await self.manager.call("delivery", row["id"]))["status"], "confirmed"
        )
        self.assertIsNotNone(await self.manager.lookup_message(self.scope, 1001))

    async def test_private_tool_queues_a_separate_persistent_destination(self):
        from agents.tool_context import ToolContext

        tool = importlib.import_module("context_test_plugin.tool")
        arguments = json.dumps({"user_id": 200, "message": "only this note"})
        wrapper = ToolContext(
            tool.ChatContext(bot=self.bot, event=self.event(1, "send a note")),
            tool_name="send_private_message",
            tool_call_id="private-1",
            tool_arguments=arguments,
        )
        async with self.manager.operation(self.scope):
            result = await tool.send_private_message.on_invoke_tool(wrapper, arguments)
            self.assertIn("queued", result)
        if tool._delivery_tasks:
            await asyncio.wait_for(asyncio.gather(*tool._delivery_tasks), 2)
        private_scope = self.context.Scope("42", "private", "200")
        binding = await self.manager.lookup_message(private_scope, 1001)
        self.assertEqual(binding.content.plain_text(), "only this note")
        self.assertIsNone(await self.manager.call("active", private_scope))
        self.assertIsNone(await self.manager.lookup_message(self.scope, 1001))

    async def invoke_history_tool(self, name, *, event=None, turn=None, **arguments):
        from agents.tool_context import ToolContext

        tool = importlib.import_module("context_test_plugin.tool")
        encoded = json.dumps(arguments)
        wrapper = ToolContext(
            tool.ChatContext(
                bot=self.bot, event=event or self.event(999, "查阅会话"), turn=turn
            ),
            tool_name=name,
            tool_call_id="history-test",
            tool_arguments=encoded,
        )
        return json.loads(await getattr(tool, name).on_invoke_tool(wrapper, encoded))

    async def test_history_tools_do_not_create_empty_chat_or_expose_scope_parameters(
        self,
    ):
        listing = await self.invoke_history_tool("list_sessions")
        self.assertEqual(listing["sessions"], [])
        self.assertFalse(listing["has_more"])
        self.assertEqual(listing["total"], 0)
        missing = await self.invoke_history_tool("read_session", session_id="unknown")
        self.assertIn("error", missing)
        self.assertIsNone(await self.manager.call("active", self.scope))
        self.assertEqual(await self.manager.list_sessions(self.scope), [])
        for agent in (self.graph.group_agent, self.graph.private_agent):
            by_name = {t.name: t for t in agent.tools}
            self.assertIn("list_sessions", by_name)
            self.assertIn("read_session", by_name)
            self.assertEqual(
                set(by_name["list_sessions"].params_json_schema["properties"]),
                {"offset", "limit"},
            )
            self.assertEqual(
                set(by_name["read_session"].params_json_schema["properties"]),
                {"session_id", "offset", "limit"},
            )
        self.bot.send_group_msg.assert_not_awaited()

    async def test_session_tool_listing_is_paginated_bounded_and_read_only(self):
        sessions = [
            await self.manager.create_session(self.scope, title=f"话题 {index}")
            for index in range(55)
        ]
        await self.manager.create_session(
            self.context.Scope("42", "group", "456"), title="其他群"
        )
        changes = self.manager.store.db.total_changes
        first = await self.invoke_history_tool("list_sessions", offset=-1, limit=999)
        self.assertEqual(first["total"], 55)
        self.assertEqual(len(first["sessions"]), 50)
        self.assertEqual(first["sessions"][0]["session_id"], sessions[-1].short_id)
        self.assertEqual(
            [item["is_current"] for item in first["sessions"]], [True] + [False] * 49
        )
        self.assertTrue(first["sessions"][0]["created_at"])
        second = await self.invoke_history_tool(
            "list_sessions", offset=first["next_offset"]
        )
        self.assertEqual(
            [item["session_id"] for item in second["sessions"]],
            [s.short_id for s in reversed(sessions[:5])],
        )
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_offset"])
        past_end = await self.invoke_history_tool("list_sessions", offset=100)
        self.assertEqual(past_end["sessions"], [])
        self.assertEqual(self.manager.store.db.total_changes, changes)

    async def test_read_session_tool_reads_messages_sources_and_does_not_switch(self):
        await self.say(1, "先前关于 SQLite 的讨论")
        old = await self.manager.get_active_session(self.scope)
        await self.manager.append_context(
            old,
            content=self.context.ContextContent.text("SQLite 资料正文"),
            source=self.context.SourceInfo("article", "sqlite", title="数据库说明"),
            idempotency_key="source",
        )
        await self.manager.append_context(
            old,
            content=self.context.ContextContent.text("Sunny 的补充说明"),
            kind="assistant_publication",
            source=self.context.SourceInfo("note", "note"),
            idempotency_key="note",
        )
        await self.say(2, "/new")
        current = await self.manager.get_active_session(self.scope)
        changes = self.manager.store.db.total_changes
        result = await self.invoke_history_tool(
            "read_session", session_id=f" #{old.short_id.lower()} ", limit=8000
        )
        for text in (
            "先前关于 SQLite",
            "模型回复",
            "SQLite 资料正文",
            "数据库说明",
            "Sunny 的补充说明",
            "尚未确认送达",
        ):
            self.assertIn(text, result["content"])
        by_id = await self.invoke_history_tool(
            "read_session", session_id=old.conversation_id
        )
        self.assertEqual(by_id["content"], result["content"])
        self.assertEqual(await self.manager.get_active_session(self.scope), current)
        self.assertEqual(self.manager.store.db.total_changes, changes)
        empty = await self.invoke_history_tool(
            "read_session", session_id=current.short_id
        )
        self.assertEqual(empty["content"], "")
        self.assertEqual(empty["total_chars"], 0)

    async def test_session_reader_scope_is_derived_from_event_and_cannot_leak_other_chats(
        self,
    ):
        for scope in (
            self.context.Scope("42", "group", "456"),
            self.context.Scope("43", "group", "123"),
            self.context.Scope("42", "private", "100"),
            self.context.Scope("42", "private", "200"),
        ):
            hidden = await self.manager.create_session(scope, title="隔离标题")
            await self.manager.append_context(
                hidden,
                content=self.context.ContextContent.text("隔离正文"),
                source=self.context.SourceInfo("test", "isolated"),
                idempotency_key="isolated",
            )
            for identifier in (hidden.short_id, hidden.conversation_id):
                result = await self.invoke_history_tool(
                    "read_session", session_id=identifier
                )
                self.assertEqual(set(result), {"error"})
            if scope.chat_type == "private" and scope.peer_id == "100":
                own_private = hidden
        result = await self.invoke_history_tool(
            "read_session",
            event=self.event(2, "读取", private=True),
            session_id=own_private.short_id,
        )
        self.assertIn("隔离正文", result["content"])
        listing = await self.invoke_history_tool("list_sessions")
        self.assertEqual(listing["sessions"], [])
        self.assertIsNone(await self.manager.call("active", self.scope))

    async def test_session_reader_pages_full_text_without_raw_tools_reasoning_or_media(
        self,
    ):
        async def rich_run(event, bot, input_items, turn):
            await turn.session.add_items(
                input_items
                + [
                    {"type": "reasoning", "summary": "SECRET_REASONING"},
                    {
                        "type": "function_call",
                        "name": "read_session",
                        "call_id": "c",
                        "arguments": "SECRET_ARGUMENTS",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "c",
                        "output": "SECRET_TOOL_RESULT",
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "最终回复"}],
                    },
                ]
            )
            return "最终回复"

        with patch.object(self.controller.chat, "run_group_chat", side_effect=rich_run):
            await self.say(1, "可见问题")
        session = await self.manager.get_active_session(self.scope)
        await self.manager.append_context(
            session,
            content=self.context.ContextContent(
                [
                    {"type": "input_text", "text": "长资料" * 3500 + "资料结束"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,SECRET_IMAGE",
                    },
                    {"type": "input_file", "file_data": "SECRET_FILE"},
                ]
            ),
            source=self.context.SourceInfo("test", "long"),
            idempotency_key="long",
        )
        offset, parts = 0, []
        while True:
            page = await self.invoke_history_tool(
                "read_session", session_id=session.short_id, offset=offset, limit=1000
            )
            self.assertLessEqual(len(page["content"]), 1000)
            parts.append(page["content"])
            if not page["has_more"]:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        joined = "".join(parts)
        self.assertEqual(len(joined), page["total_chars"])
        self.assertIn("可见问题", joined)
        self.assertIn("最终回复", joined)
        self.assertIn("资料结束", joined)
        self.assertIn("[图片]", joined)
        self.assertIn("[附件]", joined)
        self.assertNotIn("SECRET_", joined)
        clamped = await self.invoke_history_tool(
            "read_session", session_id=session.short_id, offset=-50, limit=99999
        )
        self.assertEqual(clamped["content"], joined[:8000])
        beyond = await self.invoke_history_tool(
            "read_session", session_id=session.short_id, offset=page["total_chars"] + 1
        )
        self.assertEqual(beyond["content"], "")
        self.assertFalse(beyond["has_more"])

    async def test_session_reader_preserves_failure_delivery_status_and_excludes_active_turn(
        self,
    ):
        with patch.object(
            self.controller.chat, "run_group_chat", side_effect=RuntimeError("failed")
        ):
            await self.say(1, "失败请求")
        self.bot.send_group_msg.side_effect = NetworkError("lost receipt")
        await self.say(2, "发送状态未知")
        session = await self.manager.get_active_session(self.scope)
        turn_id = await self.manager.begin_turn(
            session,
            "running",
            self.context.ContextContent.text("当前尚未完成"),
            "用户",
            "100",
        )
        turn = await self.manager.prepare_model_turn(session, turn_id, "当前尚未完成")
        result = await self.invoke_history_tool(
            "read_session", session_id=session.short_id, turn=turn
        )
        self.assertIn("失败请求", result["content"])
        self.assertIn("失败或中断", result["content"])
        self.assertIn("未确认送达：unknown", result["content"])
        self.assertNotIn("当前尚未完成", result["content"])


if __name__ == "__main__":
    unittest.main()
