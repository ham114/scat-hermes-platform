"""Task-model packages that hide model-specific data flows."""

from scat_model_runtime.packages.comparison_v1 import (
    CFPL_METHOD_ID,
    CFPL_METHOD_VERSION,
    COMPARISON_V1_MODEL_KEY,
    COMPARISON_V1_MODEL_VERSION,
    ComparisonV1ModelPackage,
)
from scat_model_runtime.packages.gbar_v23 import (
    GBAR_V23_INPUT_UNITS,
    GBAR_V23_OUTPUT_UNITS,
    GbarV23RegressionPackage,
)

__all__ = [
    "CFPL_METHOD_ID",
    "CFPL_METHOD_VERSION",
    "COMPARISON_V1_MODEL_KEY",
    "COMPARISON_V1_MODEL_VERSION",
    "GBAR_V23_INPUT_UNITS",
    "GBAR_V23_OUTPUT_UNITS",
    "ComparisonV1ModelPackage",
    "GbarV23RegressionPackage",
]
