"""Construct reviewed HTTP configuration only from the backend's settings."""

from gbar_core.transport import ModelProvenance
from scat_model_runtime.backends.base import BackendError, BackendHealth, CancelResult
from scat_model_runtime.backends.bento_http import BentoHttpBackend, BentoHttpConfig
from scat_model_runtime.backends.gbar_codec import GbarTransportCodec


class UnavailableBackend:
    def health(self):
        return BackendHealth(False, "backend_configuration_invalid")

    def execute(self, request):
        raise BackendError("not_ready")

    def cancel(self, request_id):
        return CancelResult(False, "not_started")

    def close(self):
        pass


def configured_http_backend(settings):
    def required(name):
        value = getattr(settings, "SCAT_GBAR_V23_" + name, None)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("missing server backend configuration")
        return value.strip()

    expected = ModelProvenance(
        source_uri=required("SOURCE_URI"),
        checkpoint_sha256=required("CHECKPOINT_SHA256"),
        config_sha256=required("CONFIG_SHA256"),
        source_tree_sha256=required("BENTO_SOURCE_TREE_SHA256"),
        runtime_sha256=required("BENTO_RUNTIME_SHA256"),
        schema_sha256=required("BENTO_SCHEMA_SHA256"),
    )
    config = BentoHttpConfig(
        endpoint=required("BENTO_ENDPOINT"),
        release_id=required("BENTO_RELEASE_ID"),
        connect_timeout_seconds=getattr(settings, "SCAT_GBAR_V23_BENTO_CONNECT_TIMEOUT_SECONDS", 5),
        first_byte_timeout_seconds=getattr(
            settings, "SCAT_GBAR_V23_BENTO_FIRST_BYTE_TIMEOUT_SECONDS", 120
        ),
        total_timeout_seconds=getattr(settings, "SCAT_GBAR_V23_BENTO_TOTAL_TIMEOUT_SECONDS", 125),
    )
    return BentoHttpBackend(config, GbarTransportCodec(expected))
