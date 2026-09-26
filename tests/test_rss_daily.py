import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import nonebot
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError

if TYPE_CHECKING:
    from rss_daily import FeedItem


class AiDailyCommentaryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Load real modules without registering commands, starting jobs, or reading
        # the running bot's state. All model calls and OneBot sends are mocked.
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        nonebot.init(
            driver="~none",
            _env_file=(root / "no.env",),
            log_level="CRITICAL",
        )
        nonebot.require("nonebot_plugin_localstore")
        nonebot.require("nonebot_plugin_apscheduler")
        import nonebot_plugin_localstore as store
        from nonebot_plugin_apscheduler import scheduler

        package = types.ModuleType("rss_test_plugin")
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules[package.__name__] = package
        with (
            patch.object(store, "get_data_file", return_value=root / "rss.json"),
            patch.object(
                store, "get_plugin_data_file", return_value=root / "active.json"
            ),
            patch.object(scheduler, "add_job"),
        ):
            cls.rss = importlib.import_module("rss_test_plugin.rss_daily")
            cls.chat = importlib.import_module("rss_test_plugin.chat")
            cls.graph = importlib.import_module("rss_test_plugin.graph")
            cls.context = importlib.import_module("rss_test_plugin.context")

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = self.context.ConversationManager(
            Path(directory.name) / "context.sqlite3"
        )
        self.addCleanup(manager.store.close)
        patch.object(self.context, "_manager", manager).start()
        self.state = self.rss.AiDailyRssState({}, {123}, set())
        self.bot = Mock(self_id="42")
        self.receipt_id = 1000

        async def receipt(*args, **kwargs):
            self.receipt_id += 1
            return {"message_id": self.receipt_id}

        self.bot.send_group_msg = AsyncMock(side_effect=receipt)
        self.bot.call_api = AsyncMock(side_effect=receipt)
        self.commentary = (
            "模型开放挺好，不过部署成本要是太高，还是很难用起来。\n\n"
            "这个编程工具倒是想试试，看看改老项目时能不能少踩点坑。\n\n"
            "评测分数看着不错，就是不知道换一批题还能不能稳住。"
        )
        self.generate = AsyncMock(return_value=self.commentary)
        self.addCleanup(patch.stopall)
        patch.object(self.rss, "acomment_ai_daily", self.generate).start()
        patch.object(self.rss, "connected_onebot_bots", return_value=[self.bot]).start()
        patch.object(self.rss, "wait_between_messages", new_callable=AsyncMock).start()
        patch.object(
            self.rss,
            "plugin_config",
            self.rss.Config(sunny_agent_ai_daily_max_items=2),
        ).start()

    def item(self, item_id: str) -> "FeedItem":
        return self.rss.FeedItem(
            item_id=item_id,
            title=f"早报 {item_id}",
            link=f"https://example.com/{item_id}",
            published="",
            content=(
                f"# 今日摘要\n摘要 {item_id}\n"
                f"# 产品动态\n模型开放 {item_id}\n编程工具 {item_id}\n"
                f"# 研究\n评测 {item_id}\n"
                + self.rss.make_image_placeholder("https://example.com/image.png")
            ),
        )

    async def test_commentary_is_one_message_after_all_reports_and_forwards(
        self,
    ) -> None:
        async def generate_after_sends(text: str) -> str:
            self.assertIn("评测 new", text)
            self.assertEqual(len(self.bot.mock_calls), 4)
            return self.commentary

        self.generate.side_effect = generate_after_sends
        result = await self.rss.send_items_to_group(
            123,
            [self.item("new"), self.item("old")],
            self.state,
            preferred_bot=self.bot,
        )

        self.assertEqual(result, (2, True))
        self.assertEqual(self.state.sent_item_ids["123"], ["old", "new"])
        self.assertEqual(
            [entry[0] for entry in self.bot.mock_calls],
            [
                "send_group_msg",
                "call_api",
                "send_group_msg",
                "call_api",
                "send_group_msg",
            ],
        )
        last_message = self.bot.send_group_msg.call_args.kwargs["message"]
        self.assertEqual(
            last_message.extract_plain_text(),
            self.commentary,
        )
        self.generate.assert_awaited_once()
        prompt = self.generate.call_args.args[0]
        self.assertLess(prompt.index("早报 old"), prompt.index("早报 new"))
        self.assertIn("模型开放 old", prompt)
        self.assertIn("评测 new", prompt)
        self.assertNotIn("sunny-rss-image:", prompt)

    async def test_only_new_selected_reports_are_used_for_commentary(self) -> None:
        self.state.sent_item_ids["123"] = ["old"]
        result = await self.rss.send_items_to_group(
            123,
            [self.item("new"), self.item("old"), self.item("excluded")],
            self.state,
        )
        self.assertEqual(result, (1, True))
        prompt = self.generate.call_args.args[0]
        self.assertIn("早报 new", prompt)
        self.assertNotIn("早报 old", prompt)
        self.assertNotIn("早报 excluded", prompt)

    async def test_no_commentary_when_nothing_is_sent(self) -> None:
        self.state.sent_item_ids["123"] = ["old"]
        for items in ([], [self.item("old")]):
            with self.subTest(items=items):
                result = await self.rss.send_items_to_group(123, items, self.state)
                self.assertEqual(result, (0, False))
        self.generate.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_manual_resend_also_sends_commentary(self) -> None:
        self.state.sent_item_ids["123"] = ["old"]
        result = await self.rss.send_items_to_group(
            123,
            [self.item("old")],
            self.state,
            only_unsent=False,
            preferred_bot=self.bot,
        )
        self.assertEqual(result, (1, False))
        self.generate.assert_awaited_once()
        self.assertEqual(self.bot.send_group_msg.await_count, 2)

    async def test_incomplete_report_batch_still_sends_commentary(self) -> None:
        with patch.object(
            self.rss,
            "send_item_to_group",
            new_callable=AsyncMock,
            side_effect=[(True, True), (False, True)],
        ):
            result = await self.rss.send_items_to_group(
                123,
                [self.item("new"), self.item("old")],
                self.state,
            )
        self.assertEqual(result, (1, True))
        self.assertEqual(self.state.sent_item_ids["123"], ["old"])
        self.generate.assert_awaited_once()
        prompt = self.generate.call_args.args[0]
        self.assertIn("早报 old", prompt)
        self.assertIn("评测 new", prompt)

    async def test_failed_forward_still_sends_commentary_using_full_report(
        self,
    ) -> None:
        self.bot.call_api.side_effect = ActionFailed(
            status="failed",
            retcode=1200,
            message="发送转发消息失败",
        )
        result = await self.rss.send_items_to_group(
            123,
            [self.item("new"), self.item("old")],
            self.state,
            preferred_bot=self.bot,
        )
        # Failed detail delivery remains retryable and does not count as complete.
        self.assertEqual(result, (0, False))
        self.assertEqual(self.state.sent_item_ids["123"], [])
        self.generate.assert_awaited_once()
        prompt = self.generate.call_args.args[0]
        self.assertIn("模型开放 old", prompt)
        self.assertIn("评测 old", prompt)
        self.assertNotIn("早报 new", prompt)
        self.assertEqual(
            [entry[0] for entry in self.bot.mock_calls],
            ["send_group_msg", "call_api", "send_group_msg"],
        )
        self.assertEqual(
            self.bot.send_group_msg.call_args.kwargs["message"].extract_plain_text(),
            self.commentary,
        )

    async def test_failed_overview_does_not_generate_commentary(self) -> None:
        self.bot.send_group_msg.side_effect = ActionFailed(
            status="failed",
            retcode=1200,
            message="发送消息失败",
        )
        result = await self.rss.send_items_to_group(
            123,
            [self.item("new")],
            self.state,
        )
        self.assertEqual(result, (0, False))
        self.generate.assert_not_awaited()
        self.bot.call_api.assert_not_awaited()

    async def test_later_failed_overview_does_not_suppress_earlier_commentary(
        self,
    ) -> None:
        self.bot.send_group_msg.side_effect = [
            {},
            ActionFailed(status="failed", retcode=1200, message="发送消息失败"),
            {},
        ]
        result = await self.rss.send_items_to_group(
            123,
            [self.item("new"), self.item("old")],
            self.state,
        )
        self.assertEqual(result, (1, True))
        self.assertEqual(self.state.sent_item_ids["123"], ["old"])
        self.generate.assert_awaited_once()
        prompt = self.generate.call_args.args[0]
        self.assertIn("评测 old", prompt)
        self.assertNotIn("早报 new", prompt)
        self.assertEqual(
            self.bot.send_group_msg.call_args.kwargs["message"].extract_plain_text(),
            self.commentary,
        )

    async def test_empty_output_or_model_error_keeps_report_success(self) -> None:
        for output in ("", RuntimeError("model unavailable")):
            with self.subTest(output=output):
                self.generate.side_effect = [output]
                self.bot.reset_mock()
                state = self.rss.AiDailyRssState({}, set(), set())
                result = await self.rss.send_items_to_group(
                    123, [self.item("new")], state, only_unsent=False
                )
                self.assertEqual(result, (1, True))
                self.assertEqual(state.sent_item_ids["123"], ["new"])
                self.bot.send_group_msg.assert_awaited_once()

    async def test_model_timeout_cancels_generation_and_keeps_report_success(
        self,
    ) -> None:
        cancelled = asyncio.Event()

        async def slow_model(text: str) -> str:  # noqa: ARG001
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return ""

        self.generate.side_effect = slow_model
        with patch.object(self.rss, "COMMENTARY_TIMEOUT_SECONDS", 0.01):
            result = await self.rss.send_items_to_group(
                123, [self.item("new")], self.state
            )
        self.assertEqual(result, (1, True))
        self.assertTrue(cancelled.is_set())
        self.bot.send_group_msg.assert_awaited_once()

    async def test_commentary_send_failure_keeps_report_success(self) -> None:
        for outcome in (False, RuntimeError("send failed")):
            with (
                self.subTest(outcome=outcome),
                patch.object(
                    self.rss,
                    "send_group_text",
                    new_callable=AsyncMock,
                    side_effect=[True, outcome],
                ),
            ):
                state = self.rss.AiDailyRssState({}, set(), set())
                result = await self.rss.send_items_to_group(
                    123, [self.item("new")], state
                )
                self.assertEqual(result, (1, True))
                self.assertEqual(state.sent_item_ids["123"], ["new"])

    async def test_scheduled_push_continues_to_other_groups_and_saves_state(
        self,
    ) -> None:
        self.state.enabled_group_ids.add(456)
        self.generate.side_effect = [RuntimeError("model unavailable"), self.commentary]
        with (
            patch.object(self.rss, "load_state", return_value=self.state),
            patch.object(
                self.rss,
                "fetch_ai_daily_items",
                new_callable=AsyncMock,
                return_value=[self.item("new")],
            ),
            patch.object(self.rss, "save_state") as save,
        ):
            await self.rss.push_ai_daily_rss()
        save.assert_called_once_with(self.state)
        self.assertEqual(self.state.sent_item_ids, {"123": ["new"], "456": ["new"]})
        self.assertEqual(self.bot.send_group_msg.call_args.kwargs["group_id"], 456)
        self.assertEqual(self.generate.await_count, 2)

    async def test_commentator_reuses_model_without_chat_session_or_tools(self) -> None:
        with patch.object(
            self.chat.Runner,
            "run",
            new_callable=AsyncMock,
            return_value=types.SimpleNamespace(final_output="  我的看法  "),
        ) as runner:
            self.assertEqual(await self.chat.acomment_ai_daily("完整早报"), "我的看法")
        self.assertIs(runner.call_args.args[0], self.graph.ai_daily_commentator_agent)
        self.assertEqual(runner.call_args.args[1], "完整早报")
        self.assertIs(
            runner.call_args.kwargs["run_config"].model_provider,
            self.graph.model_provider,
        )
        self.assertNotIn("session", runner.call_args.kwargs)
        self.assertEqual(self.graph.ai_daily_commentator_agent.tools, [])

    async def test_all_report_messages_share_context_without_activating(self):
        manager = self.context.get_manager()
        scope = self.context.Scope("42", "group", "123")
        active = await manager.get_active_session(scope)
        await self.rss.send_items_to_group(123, [self.item("new")], self.state)
        bindings = [
            await manager.lookup_message(scope, message_id)
            for message_id in range(1001, self.receipt_id + 1)
        ]
        self.assertEqual(len(bindings), 3)
        self.assertTrue(all(binding is not None for binding in bindings))
        report = bindings[0].conversation
        self.assertTrue(all(binding.conversation == report for binding in bindings))
        self.assertEqual(await manager.get_active_session(scope), active)
        entries = await manager.call("entries", report)
        self.assertEqual(
            [entry["kind"] for entry in entries], ["external", "assistant_publication"]
        )
        self.assertIn("评测 new", str(entries))
        self.assertNotIn("评测 new", bindings[0].content.plain_text())
        controller = importlib.import_module("rss_test_plugin.conversation_chat")
        from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

        event = GroupMessageEvent.model_validate(
            {
                "time": 1,
                "self_id": 42,
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": 1,
                "user_id": 100,
                "group_id": 123,
                "message": Message(""),
                "raw_message": "",
                "font": 0,
                "sender": {"user_id": 100, "nickname": "Alice"},
            }
        )
        event.reply = types.SimpleNamespace(message_id=1002)
        await controller.handle_message(event, self.bot)
        self.assertEqual(await manager.get_active_session(scope), report)

    async def test_manual_resends_do_not_duplicate_full_context(self):
        manager = self.context.get_manager()
        for _ in range(2):
            await self.rss.send_items_to_group(
                123, [self.item("new")], self.state, only_unsent=False
            )
        scope = self.context.Scope("42", "group", "123")
        report = (await manager.lookup_message(scope, 1001)).conversation
        entries = await manager.call("entries", report)
        self.assertEqual(sum(entry["kind"] == "external" for entry in entries), 1)
        self.assertEqual(self.bot.send_group_msg.await_count, 4)

    async def test_scheduled_retry_reuses_receipts_when_legacy_state_is_lost(self):
        for _ in range(2):
            state = self.rss.AiDailyRssState({}, set(), set())
            result = await self.rss.send_items_to_group(123, [self.item("new")], state)
            self.assertEqual(result, (1, True))
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        self.bot.call_api.assert_awaited_once()
        self.generate.assert_awaited_once()

    async def test_failed_forward_retry_skips_confirmed_overview(self):
        self.bot.call_api.side_effect = [
            ActionFailed(status="failed", retcode=1),
            {"message_id": 5000},
        ]
        await self.rss.send_items_to_group(123, [self.item("new")], self.state)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        result = await self.rss.send_items_to_group(123, [self.item("new")], self.state)
        self.assertEqual(result, (1, True))
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        self.assertEqual(self.bot.call_api.await_count, 2)

    async def test_forward_resource_id_is_not_used_as_message_id(self):
        self.bot.call_api.side_effect = None
        self.bot.call_api.return_value = {"forward_id": "resource-only"}
        await self.rss.send_items_to_group(123, [self.item("new")], self.state)
        self.assertIsNone(
            await self.context.get_manager().lookup_message(
                self.context.Scope("42", "group", "123"),
                "resource-only",
            )
        )

    async def test_network_uncertainty_stops_fallback_and_retry(self):
        self.bot.send_group_msg.side_effect = NetworkError("timeout")
        fallback = Mock(
            self_id="43", send_group_msg=AsyncMock(return_value={"message_id": 4000})
        )
        with patch.object(
            self.rss, "connected_onebot_bots", return_value=[self.bot, fallback]
        ):
            for _ in range(2):
                result = await self.rss.send_items_to_group(
                    123, [self.item("new")], self.state
                )
                self.assertEqual(result, (0, False))
        self.bot.send_group_msg.assert_awaited_once()
        fallback.send_group_msg.assert_not_awaited()

    async def test_fallback_bot_owns_message_and_later_retry_does_not_resend(self):
        self.bot.send_group_msg.side_effect = ActionFailed(status="failed", retcode=1)
        fallback = Mock(
            self_id="43", send_group_msg=AsyncMock(return_value={"message_id": 4000})
        )
        publication = self.rss.make_rss_publication(
            [self.item("new")], only_unsent=True
        )
        with patch.object(
            self.rss, "connected_onebot_bots", return_value=[self.bot, fallback]
        ):
            result = await self.rss.send_group_text(
                123, "overview", publication=publication
            )
            self.assertTrue(result)
            self.bot.send_group_msg.side_effect = None
            self.bot.send_group_msg.return_value = {"message_id": 4001}
            result = await self.rss.send_group_text(
                123, "overview", publication=publication
            )
            self.assertTrue(result)
        manager = self.context.get_manager()
        self.assertIsNotNone(
            await manager.lookup_message(self.context.Scope("43", "group", "123"), 4000)
        )
        self.assertIsNone(
            await manager.lookup_message(self.context.Scope("42", "group", "123"), 4000)
        )
        self.bot.send_group_msg.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
