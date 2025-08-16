from nonebot import get_plugin_config
from nonebot import require
from nonebot.plugin import PluginMetadata

from .config import Config
from . import event
from . import commands


require("nonebot_plugin_localstore")
require("nonebot_plugin_alconna")

__plugin_meta__ = PluginMetadata(
    name="sunny_agent",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)
