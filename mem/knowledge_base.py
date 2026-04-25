# knowledge_base.py
"""群组知识库管理模块"""

from typing import List, Tuple, Optional
from pydantic import BaseModel, Field, ValidationError
import nonebot_plugin_localstore as store
import json
import time

class KnowledgeItem(BaseModel):
    """知识库条目模型"""
    name: str = Field(...)
    text: str = Field(...)
    timestamp: int = Field(default_factory=lambda: int(time.time()))


class GroupKnowledgeBase(BaseModel):
    """群组知识库模型"""
    items: List[KnowledgeItem] = Field(default_factory=list)


knowledge_base_file = store.get_plugin_data_file("group_knowledge_base.json")
group_knowledge_bases: dict[str, GroupKnowledgeBase] = {}


def _load_group_knowledge_bases():
    """Load group_knowledge_bases from the JSON file into Pydantic models."""
    global group_knowledge_bases
    loaded_data = {}
    try:
        # 尝试读取文件内容
        json_content = knowledge_base_file.read_text()
        if json_content:  # 确保内容不为空，避免解析空字符串
            loaded_data = json.loads(json_content)
    except (FileNotFoundError, json.JSONDecodeError):
        # 如果文件不存在或解析失败，就使用空字典
        loaded_data = {}
    except Exception as e:
        print(f"Error reading or parsing group_knowledge_base.json: {e}")
        loaded_data = {}
    
    current_group_knowledge_bases: dict[str, GroupKnowledgeBase] = {}
    if isinstance(loaded_data, dict):
        for group_id_str, data in loaded_data.items():
            try:
                current_group_knowledge_bases[group_id_str] = GroupKnowledgeBase.model_validate(data)
            except ValidationError as e:
                print(f"Skipping invalid GroupKnowledgeBase data for group {group_id_str}: {e}")
                current_group_knowledge_bases[group_id_str] = GroupKnowledgeBase()
            except Exception as e:
                print(f"Unexpected error processing group {group_id_str}: {e}")
                current_group_knowledge_bases[group_id_str] = GroupKnowledgeBase()
    
    group_knowledge_bases = current_group_knowledge_bases


_load_group_knowledge_bases()


def _save_group_knowledge_bases():
    """Helper function to save the current state of group_knowledge_bases to the file."""
    serializable_data = {
        group_id_str: kb.model_dump()
        for group_id_str, kb in group_knowledge_bases.items()
    }
    knowledge_base_file.write_text(json.dumps(serializable_data, indent=4, ensure_ascii=False))


async def add_knowledge_to_group(group_id: int, name: str, text: str) -> bool:
    """
    向群组添加知识库条目
    
    Args:
        group_id: 群组ID
        name: 知识库条目名称
        text: 知识库内容
        
    Returns:
        bool: 是否添加成功
    """
    group_id_str = str(group_id)
    
    # 如果该群组不存在，则创建一个新的 GroupKnowledgeBase 实例
    if group_id_str not in group_knowledge_bases:
        group_knowledge_bases[group_id_str] = GroupKnowledgeBase()
    
    kb = group_knowledge_bases[group_id_str]
    
    # 检查名称是否已存在
    for item in kb.items:
        if item.name == name:
            return False  # 名称重复
    
    new_item = KnowledgeItem(name=name, text=text)
    kb.items.append(new_item)
    _save_group_knowledge_bases()
    return True

async def remove_knowledge_from_group(
    group_id: int, 
    name: Optional[str] = None, 
    index: Optional[int] = None
) -> bool:
    """
    从群组删除知识库条目
    
    Args:
        group_id: 群组ID
        name: 要删除的条目名称
        index: 要删除的条目序号(1开始)
        
    Returns:
        bool: 是否删除成功
    """
    group_id_str = str(group_id)
    
    if group_id_str not in group_knowledge_bases:
        return False
    
    kb = group_knowledge_bases[group_id_str]
    
    if not kb.items:
        return False
    
    try:
        if index is not None:
            # 按序号删除
            if 1 <= index <= len(kb.items):
                kb.items.pop(index - 1)  # 转换为0开始的索引
                _save_group_knowledge_bases()
                return True
            return False
        
        elif name is not None:
            # 按名称删除
            for i, item in enumerate(kb.items):
                if item.name == name:
                    kb.items.pop(i)
                    _save_group_knowledge_bases()
                    return True
            return False
        
        return False
    except Exception as e:
        print(f"Error removing knowledge from group {group_id}: {e}")
        return False


def get_group_knowledge_base(group_id: int) -> Optional[GroupKnowledgeBase]:
    """获取群组知识库对象"""
    return group_knowledge_bases.get(str(group_id))


def get_group_knowledge_list(group_id: int) -> List[str]:
    """
    获取群组知识库列表
    
    Args:
        group_id: 群组ID
        
    Returns:
        List[str]: 返回 name 的列表
    """
    kb = get_group_knowledge_base(group_id)
    if kb:
        return [item.name for item in kb.items]
    return []


def search_knowledge_in_group(group_id: int, query: str) -> List[Tuple[str, str]]:
    """
    在群组知识库中搜索
    
    Args:
        group_id: 群组ID
        query: 搜索关键词
        
    Returns:
        List[Tuple[str, str]]: 匹配的(name, text)列表
    """
    kb = get_group_knowledge_base(group_id)
    if not kb:
        return []
    
    results = []
    query_lower = query.lower()
    
    for item in kb.items:
        # 在名称或内容中搜索
        if query_lower in item.name.lower() or query_lower in item.text.lower():
            results.append((item.name, item.text))
    
    return results


def get_knowledge_count(group_id: int) -> int:
    """获取群组知识库条目数量"""
    kb = get_group_knowledge_base(group_id)
    if kb:
        return len(kb.items)
    return 0
