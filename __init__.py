from nonebot import get_plugin_config, require
from nonebot.plugin import PluginMetadata

from .config import Config

require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_alconna")

from . import commands as commands
from . import event as event
from . import rss_daily as rss_daily
from . import tibo_monitor as tibo_monitor

__plugin_meta__ = PluginMetadata(
    name="sunny_agent",
    description="Sunny 群聊 Agent、AI 早报与 Tibo 推文监控",
    usage="/rss open|close|today；/tibo open|close|latest",
    config=Config,
)

config = get_plugin_config(Config)
