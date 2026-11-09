from enum import StrEnum
from json import JSONDecodeError, loads
from typing import Annotated, Any, Literal

from dotenv import find_dotenv
from pydantic import (
    BeforeValidator,
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    computed_field,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from schema.models import (
    AllModelEnum,
    AnthropicModelName,
    AWSModelName,
    AzureOpenAIModelName,
    DeepseekModelName,
    FakeModelName,
    GoogleModelName,
    GroqModelName,
    OllamaModelName,
    OpenAICompatibleName,
    OpenAIModelName,
    OpenRouterModelName,
    Provider,
    VertexAIModelName,
)


class DatabaseType(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"
    MONGO = "mongo"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    def to_logging_level(self) -> int:
        """Convert to Python logging level constant."""
        import logging

        mapping = {
            LogLevel.DEBUG: logging.DEBUG,
            LogLevel.INFO: logging.INFO,
            LogLevel.WARNING: logging.WARNING,
            LogLevel.ERROR: logging.ERROR,
            LogLevel.CRITICAL: logging.CRITICAL,
        }
        return mapping[self]


class AnalysisIdentityMode(StrEnum):
    """Identity source used by browser-facing analysis artifact endpoints."""

    DISABLED = "disabled"
    SINGLE_USER = "single_user"
    TRUSTED_PROXY = "trusted_proxy"


def check_str_is_http(x: str) -> str:
    http_url_adapter = TypeAdapter(HttpUrl)
    return str(http_url_adapter.validate_python(x))


def check_visualization_base_url(value: str) -> str:
    """Accept the same-origin root or one absolute HTTP(S) Vue base URL."""

    if value == "/":
        return value
    return check_str_is_http(value).rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=find_dotenv(),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        validate_default=False,
    )
    MODE: str | None = None

    HOST: str = "0.0.0.0"
    PORT: int = 8080
    GRACEFUL_SHUTDOWN_TIMEOUT: int = 30
    LOG_LEVEL: LogLevel = LogLevel.WARNING
    OBSERVABILITY_JSON_LOGS: bool = True
    OBSERVABILITY_SLOW_REQUEST_MS: float = Field(default=2000.0, ge=1.0, le=600000.0)
    OBSERVABILITY_SLOW_MONGO_MS: float = Field(default=1000.0, ge=1.0, le=600000.0)
    OBSERVABILITY_ERROR_RATE_MIN_REQUESTS: int = Field(default=20, ge=5, le=10000)
    OBSERVABILITY_ERROR_RATE_THRESHOLD: float = Field(default=0.2, gt=0.0, le=1.0)
    OBSERVABILITY_ERROR_RATE_WINDOW_SECONDS: float = Field(default=300.0, ge=10.0, le=86400.0)
    OBSERVABILITY_ALERT_COOLDOWN_SECONDS: float = Field(default=300.0, ge=1.0, le=86400.0)
    REQUEST_MAX_BODY_BYTES: int = Field(default=8_000_000, ge=1024, le=100_000_000)
    REQUEST_MAX_CONCURRENCY: int = Field(default=64, ge=1, le=1024)

    # Optional shared coordination/cache layer. MongoDB remains authoritative;
    # an unset or unavailable Redis endpoint must not block single-instance use.
    REDIS_URL: SecretStr | None = None
    REDIS_KEY_PREFIX: str = Field(default="scat", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    REDIS_CONNECT_TIMEOUT_SECONDS: float = Field(default=0.5, gt=0.0, le=30.0)
    REDIS_SOCKET_TIMEOUT_SECONDS: float = Field(default=0.5, gt=0.0, le=30.0)
    REDIS_CONFIRMATION_MAX_BYTES: int = Field(default=262_144, ge=1024, le=4_000_000)
    REDIS_MATERIALIZATION_MAX_BYTES: int = Field(default=1_000_000, ge=1024, le=8_000_000)
    REDIS_QUALITY_SNAPSHOT_MAX_BYTES: int = Field(default=8_000_000, ge=1024, le=32_000_000)
    REDIS_HTTP_MAX_CONCURRENCY: int = Field(default=128, ge=1, le=4096)
    REDIS_HTTP_RATE_LIMIT: int = Field(default=600, ge=1, le=1_000_000)
    REDIS_HTTP_RATE_WINDOW_SECONDS: int = Field(default=60, ge=1, le=3600)
    REDIS_HTTP_SLOT_TTL_SECONDS: int = Field(default=120, ge=10, le=3600)
    REDIS_TASK_EVENT_MAXLEN: int = Field(default=10_000, ge=100, le=1_000_000)

    HERMES_BASE_URL: str = "http://127.0.0.1:18081"
    HERMES_API_KEY_FILE: str = "var/secrets/hermes-api.key"
    HERMES_STATE_FILE: str = "var/platform/hermes.sqlite"
    HERMES_TASK_STATE_FILE: str = "var/platform/model-tasks.sqlite"
    HERMES_RESULT_STATE_FILE: str = "var/platform/hermes-results.sqlite"
    HERMES_SKILL_JOBS_ENABLED: bool = True
    HERMES_WORKFLOWS_ENABLED: bool = True
    HERMES_MAX_ACTIVE_PER_USER: int = Field(default=1, ge=1, le=100)
    HERMES_MAX_ACTIVE_TOTAL: int = Field(default=3, ge=1, le=100)

    AUTH_SECRET: SecretStr | None = None
    # SINGLE_USER supports private service profiles. Public Hermes uses the
    # signed portal directory in TRUSTED_PROXY mode; headers alone are insufficient.
    ANALYSIS_IDENTITY_MODE: AnalysisIdentityMode = AnalysisIdentityMode.DISABLED
    ANALYSIS_SINGLE_USER_ID: str | None = None
    # Historical records without explicit ownership belong only to this
    # server-controlled migration identity, never to the first new reader.
    ANALYSIS_LEGACY_OWNER_ID: str | None = None
    PORTAL_IDENTITIES_FILE: str | None = None
    PORTAL_SIGNING_KEY_FILE: str | None = None

    OPENAI_API_KEY: SecretStr | None = None
    DEEPSEEK_API_KEY: SecretStr | None = None
    DEEPSEEK_BASE_URL: Annotated[str, BeforeValidator(check_str_is_http)] = (
        "https://api.deepseek.com"
    )
    ANTHROPIC_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    GOOGLE_APPLICATION_CREDENTIALS: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    USE_AWS_BEDROCK: bool = False
    OLLAMA_MODEL: str | None = None
    OLLAMA_BASE_URL: str | None = None
    USE_FAKE_MODEL: bool = False
    OPENROUTER_API_KEY: str | None = None

    # If DEFAULT_MODEL is None, it will be set in model_post_init
    DEFAULT_MODEL: AllModelEnum | None = None  # type: ignore[assignment]
    AVAILABLE_MODELS: set[AllModelEnum] = set()  # type: ignore[assignment]

    # Set openai compatible api, mainly used for proof of concept
    COMPATIBLE_MODEL: str | None = None
    COMPATIBLE_API_KEY: SecretStr | None = None
    COMPATIBLE_BASE_URL: str | None = None

    OPENWEATHERMAP_API_KEY: SecretStr | None = None

    # MCP Configuration
    GITHUB_PAT: SecretStr | None = None
    MCP_GITHUB_SERVER_URL: str = "https://api.githubcopilot.com/mcp/"

    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_PROJECT: str = "default"
    LANGCHAIN_ENDPOINT: Annotated[str, BeforeValidator(check_str_is_http)] = (
        "https://api.smith.langchain.com"
    )
    LANGCHAIN_API_KEY: SecretStr | None = None

    LANGFUSE_TRACING: bool = False
    LANGFUSE_HOST: Annotated[str, BeforeValidator(check_str_is_http)] = "https://cloud.langfuse.com"
    LANGFUSE_PUBLIC_KEY: SecretStr | None = None
    LANGFUSE_SECRET_KEY: SecretStr | None = None

    # Database Configuration
    DATABASE_TYPE: DatabaseType = (
        DatabaseType.SQLITE
    )  # Options: DatabaseType.SQLITE or DatabaseType.POSTGRES
    SQLITE_DB_PATH: str = "checkpoints.db"

    # PostgreSQL Configuration
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: SecretStr | None = None
    POSTGRES_HOST: str | None = None
    POSTGRES_PORT: int | None = None
    POSTGRES_DB: str | None = None
    POSTGRES_APPLICATION_NAME: str = "agent-service-toolkit"
    POSTGRES_MIN_CONNECTIONS_PER_POOL: int = 1
    POSTGRES_MAX_CONNECTIONS_PER_POOL: int = 1

    # MongoDB Configuration
    MONGO_HOST: str | None = None
    MONGO_PORT: int | None = None
    MONGO_DB: str | None = None
    MONGO_USER: str | None = None
    MONGO_PASSWORD: SecretStr | None = None
    MONGO_AUTH_SOURCE: str | None = None
    MONGO_TLS: bool = False  # opt-in TLS for MongoDB; set to True for production/Atlas

    # SCAT business database configuration
    FLIGHT_MONGO_URI: SecretStr = SecretStr("mongodb://127.0.0.1:27017")
    FLIGHT_MONGO_DB: str = "flight_data"

    SCAT_FLIGHTS_COLLECTION: str = "scat_flights"
    SCAT_AIRSPACE_COLLECTION: str = "airspace_configs"
    UAV_FLIGHTS_COLLECTION: str = "uav_flights"
    ANALYSIS_RUNS_COLLECTION: str = "analysis_runs"
    ANALYSIS_ARTIFACTS_COLLECTION: str = "analysis_artifacts"
    # Zero keeps research run metadata indefinitely. Positive values set an
    # absolute expires_at consumed by the MongoDB TTL index.
    ANALYSIS_RUN_RETENTION_DAYS: int = Field(default=0, ge=0, le=3650)
    ANALYSIS_ARTIFACT_RETENTION_DAYS: int = Field(default=0, ge=0, le=3650)
    ANALYSIS_ARTIFACT_MAX_BYTES: int = Field(default=8_000_000, ge=1024, le=15_000_000)
    ANALYSIS_VISUALIZATION_BASE_URL: Annotated[
        str,
        BeforeValidator(check_visualization_base_url),
    ] = "http://localhost:5173"
    ANALYSIS_CODE_VERSION: str = Field(default="0.1.0", min_length=1, max_length=120)
    SCAT_EXPERIMENT_ARTIFACT_DIR: str = Field(
        default="var/scat-experiment-artifacts",
        min_length=1,
        max_length=500,
    )
    # Experiment workers atomically claim a MongoDB-backed lease and renew it
    # while evaluating. Recovery/requeue policy is intentionally separate.
    SCAT_EXPERIMENT_LEASE_SECONDS: float = Field(default=300.0, ge=30.0, le=3600.0)
    SCAT_EXPERIMENT_HEARTBEAT_SECONDS: float = Field(default=30.0, ge=5.0, le=600.0)
    SCAT_EXPERIMENT_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=20)
    SCAT_EXPERIMENT_RETRY_BASE_SECONDS: float = Field(default=60.0, ge=0.0, le=86400.0)
    SCAT_EXPERIMENT_RETRY_MAX_SECONDS: float = Field(default=3600.0, ge=0.0, le=86400.0)
    FLIGHT_MONGO_CONNECT_TIMEOUT_MS: int = Field(default=5000, ge=100, le=60000)
    FLIGHT_MONGO_SERVER_SELECTION_TIMEOUT_MS: int = Field(default=5000, ge=100, le=60000)
    FLIGHT_MONGO_SOCKET_TIMEOUT_MS: int = Field(default=15000, ge=100, le=120000)
    FLIGHT_MONGO_OPERATION_TIMEOUT_MS: int = Field(default=30000, ge=100, le=300000)
    SCAT_MATERIALIZATION_CACHE_TTL_SECONDS: float = Field(default=30.0, ge=0.0, le=3600.0)
    SCAT_MATERIALIZATION_CACHE_MAX_ENTRIES: int = Field(default=512, ge=1, le=10000)
    SCAT_QUALITY_SNAPSHOT_TTL_SECONDS: float = Field(default=300.0, ge=0.0, le=86400.0)
    SCAT_MATERIALIZATION_CONCURRENCY: int = Field(default=4, ge=1, le=16)

    # Reviewed SCAT GBAR v23 task-model package. Heavy model loading remains lazy.
    SCAT_GBAR_V23_ENABLED: bool = False
    SCAT_GBAR_V23_BACKEND: Literal["subprocess", "bento_http"] = "subprocess"
    SCAT_GBAR_V23_SHADOW_REFERENCES: tuple[str, ...] = ()
    SCAT_GBAR_V23_SHADOW_REPORT_DIR: str | None = None
    SCAT_GBAR_V23_OBSERVE: bool = False
    SCAT_GBAR_V23_COMPARISON_SUBPROCESS_ENABLED: bool = False
    SCAT_GBAR_V23_BENTO_ENDPOINT: str | None = None
    SCAT_GBAR_V23_BENTO_RELEASE_ID: str | None = None
    SCAT_GBAR_V23_BENTO_SOURCE_TREE_SHA256: str | None = None
    SCAT_GBAR_V23_BENTO_RUNTIME_SHA256: str | None = None
    SCAT_GBAR_V23_BENTO_SCHEMA_SHA256: str | None = None
    SCAT_GBAR_V23_BENTO_CONNECT_TIMEOUT_SECONDS: float = Field(default=5, gt=0, le=30)
    SCAT_GBAR_V23_BENTO_FIRST_BYTE_TIMEOUT_SECONDS: float = Field(default=120, gt=0, le=120)
    SCAT_GBAR_V23_BENTO_TOTAL_TIMEOUT_SECONDS: float = Field(default=125, gt=0, le=300)
    SCAT_GBAR_V23_PYTHON_EXECUTABLE: str | None = None
    SCAT_GBAR_V23_WORKER_SCRIPT: str | None = None
    SCAT_GBAR_V23_SOURCE_DIR: str | None = None
    SCAT_GBAR_V23_CONFIG: str | None = None
    SCAT_GBAR_V23_CHECKPOINT: str | None = None
    SCAT_GBAR_V23_SOURCE_URI: str | None = None
    SCAT_GBAR_V23_WORKER_SCRIPT_SHA256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    SCAT_GBAR_V23_CONFIG_SHA256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    SCAT_GBAR_V23_CHECKPOINT_SHA256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    SCAT_GBAR_V23_DEVICE: str = "cuda"
    SCAT_GBAR_V23_STARTUP_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, le=600)
    SCAT_GBAR_V23_PREDICTION_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, le=600)
    SCAT_GBAR_V23_CIRCUIT_FAILURE_THRESHOLD: int = Field(default=3, ge=1, le=20)
    SCAT_GBAR_V23_CIRCUIT_RECOVERY_SECONDS: float = Field(default=30.0, gt=0, le=3600.0)

    # Frozen, un-fine-tuned M8 RRF knowledge retriever. It runs in the
    # isolated scat-rag-train environment and is reachable on loopback only.
    SCAT_RAG_BASELINE_ENABLED: bool = False
    SCAT_RAG_BASELINE_URL: str = "http://127.0.0.1:8091"
    SCAT_RAG_BASELINE_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=60)

    # Azure OpenAI Settings
    AZURE_OPENAI_API_KEY: SecretStr | None = None
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_VERSION: str = "2024-02-15-preview"
    AZURE_OPENAI_DEPLOYMENT_MAP: dict[str, str] = Field(
        default_factory=dict, description="Map of model names to Azure deployment IDs"
    )

    def model_post_init(self, __context: Any, /) -> None:
        api_keys = {
            Provider.OPENAI: self.OPENAI_API_KEY,
            Provider.OPENAI_COMPATIBLE: self.COMPATIBLE_BASE_URL and self.COMPATIBLE_MODEL,
            Provider.DEEPSEEK: self.DEEPSEEK_API_KEY,
            Provider.ANTHROPIC: self.ANTHROPIC_API_KEY,
            Provider.GOOGLE: self.GOOGLE_API_KEY,
            Provider.VERTEXAI: self.GOOGLE_APPLICATION_CREDENTIALS,
            Provider.GROQ: self.GROQ_API_KEY,
            Provider.AWS: self.USE_AWS_BEDROCK,
            Provider.OLLAMA: self.OLLAMA_MODEL,
            Provider.FAKE: self.USE_FAKE_MODEL,
            Provider.AZURE_OPENAI: self.AZURE_OPENAI_API_KEY,
            Provider.OPENROUTER: self.OPENROUTER_API_KEY,
        }
        active_keys = [k for k, v in api_keys.items() if v]
        if not active_keys:
            # The deterministic SCAT-only service profile does not require an LLM.
            # Agent startup validation remains responsible for rejecting a missing
            # model when the full service profile is used.
            return

        for provider in active_keys:
            match provider:
                case Provider.OPENAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenAIModelName.GPT_5_NANO
                    self.AVAILABLE_MODELS.update(set(OpenAIModelName))
                case Provider.OPENAI_COMPATIBLE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenAICompatibleName.OPENAI_COMPATIBLE
                    self.AVAILABLE_MODELS.update(set(OpenAICompatibleName))
                case Provider.DEEPSEEK:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = DeepseekModelName.DEEPSEEK_FLASH
                    self.AVAILABLE_MODELS.update(set(DeepseekModelName))
                case Provider.ANTHROPIC:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AnthropicModelName.HAIKU_45
                    self.AVAILABLE_MODELS.update(set(AnthropicModelName))
                case Provider.GOOGLE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = GoogleModelName.GEMINI_20_FLASH
                    self.AVAILABLE_MODELS.update(set(GoogleModelName))
                case Provider.VERTEXAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = VertexAIModelName.GEMINI_20_FLASH
                    self.AVAILABLE_MODELS.update(set(VertexAIModelName))
                case Provider.GROQ:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = GroqModelName.LLAMA_31_8B
                    self.AVAILABLE_MODELS.update(set(GroqModelName))
                case Provider.AWS:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AWSModelName.BEDROCK_HAIKU
                    self.AVAILABLE_MODELS.update(set(AWSModelName))
                case Provider.OLLAMA:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OllamaModelName.OLLAMA_GENERIC
                    self.AVAILABLE_MODELS.update(set(OllamaModelName))
                case Provider.OPENROUTER:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenRouterModelName.GEMINI_25_FLASH
                    self.AVAILABLE_MODELS.update(set(OpenRouterModelName))
                case Provider.FAKE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = FakeModelName.FAKE
                    self.AVAILABLE_MODELS.update(set(FakeModelName))
                case Provider.AZURE_OPENAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AzureOpenAIModelName.AZURE_GPT_4O_MINI
                    self.AVAILABLE_MODELS.update(set(AzureOpenAIModelName))
                    # Validate Azure OpenAI settings if Azure provider is available
                    if not self.AZURE_OPENAI_API_KEY:
                        raise ValueError("AZURE_OPENAI_API_KEY must be set")
                    if not self.AZURE_OPENAI_ENDPOINT:
                        raise ValueError("AZURE_OPENAI_ENDPOINT must be set")
                    if not self.AZURE_OPENAI_DEPLOYMENT_MAP:
                        raise ValueError("AZURE_OPENAI_DEPLOYMENT_MAP must be set")

                    # Parse deployment map if it's a string
                    if isinstance(self.AZURE_OPENAI_DEPLOYMENT_MAP, str):
                        try:
                            self.AZURE_OPENAI_DEPLOYMENT_MAP = loads(
                                self.AZURE_OPENAI_DEPLOYMENT_MAP
                            )
                        except JSONDecodeError as e:
                            raise ValueError(f"Invalid AZURE_OPENAI_DEPLOYMENT_MAP JSON: {e}")

                    # Validate required deployments exist
                    required_models = {"gpt-4o", "gpt-4o-mini"}
                    missing_models = required_models - set(self.AZURE_OPENAI_DEPLOYMENT_MAP.keys())
                    if missing_models:
                        raise ValueError(f"Missing required Azure deployments: {missing_models}")
                case _:
                    raise ValueError(f"Unknown provider: {provider}")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def BASE_URL(self) -> str:
        return f"http://{self.HOST}:{self.PORT}"

    def is_dev(self) -> bool:
        return self.MODE == "dev"


settings = Settings()
