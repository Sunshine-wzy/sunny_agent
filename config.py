from pydantic import BaseModel, Field


class Config(BaseModel):
    sunny_agent_ai_daily_enabled: bool = False
    sunny_agent_ai_daily_rss_url: str = "https://daily.juya.uk/rss.xml"
    sunny_agent_ai_daily_hour: int = Field(default=9, ge=0, le=23)
    sunny_agent_ai_daily_minute: int = Field(default=0, ge=0, le=59)
    sunny_agent_ai_daily_timezone: str = "Asia/Shanghai"
    sunny_agent_ai_daily_max_items: int = Field(default=1, ge=1, le=10)
    sunny_agent_ai_daily_message_max_chars: int = Field(default=1500, ge=500)
    sunny_agent_ai_daily_message_delay_min_seconds: float = Field(default=3.0, ge=0)
    sunny_agent_ai_daily_message_delay_max_seconds: float = Field(default=6.0, ge=0)
    sunny_agent_ai_daily_send_retry_times: int = Field(default=1, ge=0, le=5)
    sunny_agent_ai_daily_send_retry_delay_min_seconds: float = Field(default=10.0, ge=0)
    sunny_agent_ai_daily_send_retry_delay_max_seconds: float = Field(default=30.0, ge=0)
    sunny_agent_tibo_username: str = "thsottiaux"
    sunny_agent_tibo_api_base_url: str = "https://api.vxtwitter.com"
    sunny_agent_tibo_poll_interval_seconds: int = Field(default=600, ge=60)
    sunny_agent_tibo_request_timeout_seconds: float = Field(default=30.0, gt=0)
    sunny_agent_tibo_timezone: str = "Asia/Shanghai"
    sunny_agent_tibo_exclude_replies: bool = True
    sunny_agent_tibo_exclude_reposts: bool = True
    sunny_agent_tibo_send_retry_times: int = Field(default=1, ge=0, le=5)
    sunny_agent_tibo_send_retry_delay_seconds: float = Field(default=10.0, ge=0)
    sunny_agent_flayer_instruction_url: str = "http://127.0.0.1:32123/instructions"
    sunny_agent_flayer_instruction_token: str = ""
    sunny_agent_flayer_default_username: str = "Sunshine_wzy"
    sunny_agent_flayer_instruction_timeout_seconds: float = Field(default=130.0, gt=0)
    sunny_agent_image_generation_base_url: str = "https://www.geek2api.com/v1"
    sunny_agent_image_generation_api_key: str = ""
    sunny_agent_image_generation_model: str = "gpt-image-2"
    sunny_agent_image_generation_size: str = "1024x1024"
    sunny_agent_image_generation_timeout_seconds: float = Field(default=180.0, gt=0)
