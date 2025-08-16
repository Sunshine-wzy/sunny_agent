from mem0 import Memory
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
memory = Memory.from_config(config)