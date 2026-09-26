import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
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


if __name__ == "__main__":
    unittest.main()
