from typing import Any, Optional
from pydantic import BaseModel, Field, ValidationError

import nonebot_plugin_localstore as store
import json


class GroupMem(BaseModel):
    enabled: bool = Field(default=False)


group_mem_file = store.get_plugin_data_file("group_mem.json")
group_mems: dict[str, GroupMem] = {}


def _load_group_mems():
    """Load group_mems from the JSON file into Pydantic models."""
    global group_mems
    loaded_data = {}
    try:
        # 尝试读取文件内容
        json_content = group_mem_file.read_text()
        if json_content: # 确保内容不为空，避免解析空字符串
            loaded_data = json.loads(json_content)
    except (FileNotFoundError, json.JSONDecodeError):
        # 如果文件不存在或解析失败，就使用空字典
        loaded_data = {}
    except Exception as e:
        print(f"Error reading or parsing group_mem.json: {e}")
        loaded_data = {}
    current_group_mems: dict[str, GroupMem] = {}
    if isinstance(loaded_data, dict):
        for group_id_str, data in loaded_data.items():
            try:
                current_group_mems[group_id_str] = GroupMem.model_validate(data)
            except ValidationError as e:
                print(f"Skipping invalid GroupMem data for group {group_id_str}: {e}")
                current_group_mems[group_id_str] = GroupMem() # 默认值
            except Exception as e:
                print(f"Unexpected error processing group {group_id_str}: {e}")
                current_group_mems[group_id_str] = GroupMem() # 默认值
    
    group_mems = current_group_mems


_load_group_mems()


def _save_group_mems():
    """Helper function to save the current state of group_mems to the file."""
    serializable_data = {
        group_id_str: group_mem.model_dump()
        for group_id_str, group_mem in group_mems.items()
    }
    group_mem_file.write_text(json.dumps(serializable_data, indent=4, ensure_ascii=False))

def set_group_mem_enabled(group_id: int, enabled: bool):
    """
    Sets the 'enabled' status for a specific group in group_mems and saves it to file.
    Args:
        group_id (int): The ID of the group.
        enabled (bool): The desired enabled status (True to enable, False to disable).
    """
    group_id_str = str(group_id)
    
    # 如果该群组不存在，则创建一个新的 GroupMem 实例
    if group_id_str not in group_mems:
        group_mems[group_id_str] = GroupMem()
    
    # 直接修改 Pydantic 实例的属性
    group_mems[group_id_str].enabled = enabled
    
    # 保存更改
    _save_group_mems()

def get_group_mem(group_id: int) -> Optional[GroupMem]:
    return group_mems.get(str(group_id))

def get_group_mem_enabled(group_id: int) -> bool:
    group_mem = get_group_mem(group_id)
    if group_mem:
        return group_mem.enabled
    return False