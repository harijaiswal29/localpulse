"""Agent eval harness (spec §12.2) — the gate for any prompt or model change."""

from localpulse.evals.dataset import ALL_CASES, SUITES
from localpulse.evals.models import (
    DEFAULT_BAR,
    CaseResult,
    Dimension,
    EvalBar,
    EvalReport,
    Sample,
    compare_to_baseline,
)
from localpulse.evals.runner import EvalRunner, model_map_for, select

__all__ = [
    "ALL_CASES",
    "SUITES",
    "DEFAULT_BAR",
    "CaseResult",
    "Dimension",
    "EvalBar",
    "EvalReport",
    "EvalRunner",
    "Sample",
    "compare_to_baseline",
    "model_map_for",
    "select",
]
