from typing import Any, Optional
from pydantic import BaseModel, Field, ValidationError

import nonebot_plugin_localstore as store
import json


class GroupSora(BaseModel):
    enabled: bool = Field(default=False)


group_sora_file = store.get_plugin_data_file("group_sora.json")
group_soras: dict[str, GroupSora] = {}


def _load_group_soras():
    """Load group_soras from the JSON file into Pydantic models."""
    global group_soras
    loaded_data = {}
    try:
        # 尝试读取文件内容
        json_content = group_sora_file.read_text()
        if json_content:  # 确保内容不为空，避免解析空字符串
            loaded_data = json.loads(json_content)
    except (FileNotFoundError, json.JSONDecodeError):
        # 如果文件不存在或解析失败，就使用空字典
        loaded_data = {}
    except Exception as e:
        print(f"Error reading or parsing group_sora.json: {e}")
        loaded_data = {}

    current_group_soras: dict[str, GroupSora] = {}
    if isinstance(loaded_data, dict):
        for group_id_str, data in loaded_data.items():
            try:
                current_group_soras[group_id_str] = GroupSora.model_validate(data)
            except ValidationError as e:
                print(f"Skipping invalid GroupSora data for group {group_id_str}: {e}")
                current_group_soras[group_id_str] = GroupSora()  # 默认值
            except Exception as e:
                print(f"Unexpected error processing group {group_id_str}: {e}")
                current_group_soras[group_id_str] = GroupSora()  # 默认值

    group_soras = current_group_soras


_load_group_soras()


def _save_group_soras():
    """Helper function to save the current state of group_soras to the file."""
    serializable_data = {
        group_id_str: group_sora.model_dump()
        for group_id_str, group_sora in group_soras.items()
    }
    group_sora_file.write_text(json.dumps(serializable_data, indent=4, ensure_ascii=False))


def set_group_sora_enabled(group_id: int, enabled: bool):
    """
    Sets the 'enabled' status for a specific group in group_soras and saves it to file.
    Args:
        group_id (int): The ID of the group.
        enabled (bool): The desired enabled status (True to enable, False to disable).
    """
    group_id_str = str(group_id)

    # 如果该群组不存在，则创建一个新的 GroupSora 实例
    if group_id_str not in group_soras:
        group_soras[group_id_str] = GroupSora()

    # 直接修改 Pydantic 实例的属性
    group_soras[group_id_str].enabled = enabled

    # 保存更改
    _save_group_soras()


def get_group_sora(group_id: int) -> Optional[GroupSora]:
    return group_soras.get(str(group_id))


def is_group_sora_enabled(group_id: int) -> bool:
    group_sora = get_group_sora(group_id)
    if group_sora:
        return group_sora.enabled
    return False
