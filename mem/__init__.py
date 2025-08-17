from typing import Optional
from mem0 import AsyncMemory
from openai import OpenAI


config = {
    "llm": {
        "provider": "openai",
        "config": {
            "model": "qwen3-235b-a22b",
            "temperature": 0.2,
            "max_tokens": 2000
        }
    },
    "embedder": {
        "provider": "openai",
        "config": {
            "model": "qwen3-embedding-4b",
            "embedding_dims": 2560
        }
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "sunny_agent",
            "url": "http://127.0.0.1:6333",
            "embedding_model_dims": 2560
        }
    }
}

openai_client = OpenAI()
_memory_instance: Optional[AsyncMemory] = None


async def get_memory() -> AsyncMemory:
    """获取内存实例，如果不存在则创建"""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = await AsyncMemory.from_config(config)
    return _memory_instance

async def add_memory(messages, user_id: str):
    memory = await get_memory()
    try:
        result = await memory.add(messages, user_id=user_id)
        print(f"Memory added successfully ({user_id})")
        return result
    except Exception as e:
        print(f"Failed to add memory ({user_id}): {e}")