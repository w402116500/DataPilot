from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """DataLink 独立配置，只读取 ``DATALINK_`` 前缀的环境变量。"""

    model_config = SettingsConfigDict(env_prefix="DATALINK_", extra="ignore")

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8100, ge=1, le=65535)
    source_root: Path = Field(default=Path("storage/datasources"))
    database_path: Path = Field(default=Path("storage/datalink/datalink.db"))
    llm_provider: str = Field(default="openai-compatible", min_length=1, max_length=80)
    llm_model: str = Field(default="", max_length=200)
    llm_base_url: str = Field(default="", max_length=500)
    llm_api_key: SecretStr | None = None
    llm_temperature: float = Field(default=0.0, ge=0, le=1)
    llm_max_tokens: int = Field(default=16384, ge=1, le=32768)
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    embedding_model: str | None = Field(default=None, max_length=200)
    control_url: str = "http://127.0.0.1:8011"
    service_token: SecretStr | None = None
    build_timeout_seconds: float = Field(default=600, gt=0, le=1800)

    @property
    def model_configured(self) -> bool:
        """仅报告模型配置是否齐全，绝不读取或返回密钥文本。"""

        return bool(
            self.llm_model
            and self.llm_base_url
            and self.llm_api_key
            and self.llm_api_key.get_secret_value()
        )


def load_settings(**values: object) -> Settings:
    """从项目根目录的 .env 加载服务配置，避免依赖启动工作目录。"""

    return Settings(_env_file=PROJECT_ENV_FILE, **values)
