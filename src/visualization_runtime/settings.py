"""Independent server-side credentials and finite visualization limits."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["code", "review"]


class ConfigurationError(ValueError):
    """Contains setting names only, never their values."""


class ProviderSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    role: Role
    api_key: SecretStr = Field(repr=False)
    base_url: str
    model: str
    timeout_seconds: float


class VisualizationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCAT_VIS_",
        env_file=None,
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )
    enabled: bool = False
    code_api_key: SecretStr | None = Field(default=None, repr=False)
    code_base_url: str | None = None
    code_model: str = Field(default="deepseek-flash", min_length=1, max_length=100)
    review_api_key: SecretStr | None = Field(default=None, repr=False)
    review_base_url: str | None = None
    review_model: str = Field(default="qwen3.7-plus", min_length=1, max_length=100)
    api_timeout_seconds: float = Field(default=120, gt=0, le=300)
    max_revisions: int = Field(default=2, ge=0, le=5)
    max_provider_requests: int = Field(default=10, ge=1, le=30)
    max_total_tokens: int = Field(default=60000, ge=1000, le=500000)
    daily_provider_requests: int = Field(default=100, ge=1, le=10000)
    daily_tokens: int = Field(default=600000, ge=1, le=5000000)
    task_timeout_seconds: int = Field(default=900, ge=1, le=3600)
    render_timeout_seconds: int = Field(default=30, ge=1, le=120)
    worker_concurrency: int = Field(default=2, ge=1, le=8)
    artifact_root: str = "var/visualizations"
    sandbox_image: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    sandbox_url: str = "http://127.0.0.1:18166"
    sandbox_key_file: str = "var/secrets/visualization-sandbox.key"

    def execution_limits(self):
        from visualization_runtime.contracts import ExecutionLimits

        return ExecutionLimits(
            max_provider_requests=self.max_provider_requests,
            max_total_tokens=self.max_total_tokens,
            max_revisions=self.max_revisions,
            daily_provider_requests=self.daily_provider_requests,
            daily_tokens=self.daily_tokens,
            wall_seconds=self.task_timeout_seconds,
        )

    @field_validator("code_base_url", "review_base_url")
    @classmethod
    def official_https_prefix(cls, value, info):
        if value is None:
            return value
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        allowed = (
            host == "api.deepseek.com"
            if info.field_name == "code_base_url"
            else host == "dashscope.aliyuncs.com"
            or host.endswith(".maas.aliyuncs.com")
            or host == "dashscope-intl.aliyuncs.com"
            or host == "dashscope-us.aliyuncs.com"
        )
        if (
            value != value.strip()
            or any(ord(c) <= 32 for c in value)
            or parsed.scheme != "https"
            or not allowed
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or parsed.path.rstrip("/").endswith("/chat/completions")
        ):
            raise ValueError("official HTTPS API prefix required; omit /chat/completions")
        return value.rstrip("/")

    @field_validator("artifact_root")
    @classmethod
    def relative_artifact_path(cls, value):
        from pathlib import PurePosixPath, PureWindowsPath

        path = PurePosixPath(value.replace("\\", "/"))
        if (
            not value
            or path.is_absolute()
            or PureWindowsPath(value).drive
            or ".." in path.parts
            or str(path) == "."
        ):
            raise ValueError("artifact root must be project-relative")
        return path.as_posix()

    def provider(self, role: Role) -> ProviderSettings:
        if role not in {"code", "review"}:
            raise ConfigurationError("unknown_provider_role")
        key, url = getattr(self, role + "_api_key"), getattr(self, role + "_base_url")
        missing = []
        if key is None or not key.get_secret_value().strip():
            missing.append(f"SCAT_VIS_{role.upper()}_API_KEY")
        if not url:
            missing.append(f"SCAT_VIS_{role.upper()}_BASE_URL")
        if missing:
            raise ConfigurationError("missing_configuration:" + ",".join(missing))
        return ProviderSettings(
            role=role,
            api_key=key,
            base_url=url,
            model=getattr(self, role + "_model"),
            timeout_seconds=self.api_timeout_seconds,
        )


def load_settings(root: Path) -> VisualizationSettings:
    return VisualizationSettings(_env_file=root / ".env", _env_file_encoding="utf-8")
