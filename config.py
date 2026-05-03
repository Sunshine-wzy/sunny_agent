from pydantic import BaseModel, Field


class Config(BaseModel):
    sunny_agent_ai_daily_enabled: bool = False
    sunny_agent_ai_daily_rss_url: str = "https://imjuya.github.io/juya-ai-daily/rss.xml"
    sunny_agent_ai_daily_hour: int = Field(default=9, ge=0, le=23)
    sunny_agent_ai_daily_minute: int = Field(default=0, ge=0, le=59)
    sunny_agent_ai_daily_timezone: str = "Asia/Shanghai"
    sunny_agent_ai_daily_max_items: int = Field(default=1, ge=1, le=10)
    sunny_agent_ai_daily_message_max_chars: int = Field(default=1500, ge=500)
    sunny_agent_ai_daily_message_delay_min_seconds: float = Field(default=30.0, ge=0)
    sunny_agent_ai_daily_message_delay_max_seconds: float = Field(default=60.0, ge=0)
    sunny_agent_ai_daily_send_retry_times: int = Field(default=1, ge=0, le=5)
    sunny_agent_ai_daily_send_retry_delay_min_seconds: float = Field(default=10.0, ge=0)
    sunny_agent_ai_daily_send_retry_delay_max_seconds: float = Field(default=30.0, ge=0)
