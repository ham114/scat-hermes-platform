"""Authorized business-data resolvers for task-model packages."""

from scat_model_runtime.resolvers.scat_comparison import ScatComparisonInputResolver
from scat_model_runtime.resolvers.scat_gbar import ScatGbarRegressionInputResolver

__all__ = ["ScatComparisonInputResolver", "ScatGbarRegressionInputResolver"]
