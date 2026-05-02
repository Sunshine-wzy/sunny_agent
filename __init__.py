from nonebot import get_plugin_config, require
from nonebot.plugin import PluginMetadata

from .config import Config

require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_alconna")

from . import commands as commands
from . import event as event
from . import rss_daily as rss_daily

__plugin_meta__ = PluginMetadata(
    name="sunny_agent",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)
