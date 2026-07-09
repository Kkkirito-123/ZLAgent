"""Application layer surfaces."""

from .application import AgentApplication, ApplicationResult
from .bootstrap import (
    ApplicationBootstrapConfig,
    ApplicationContainer,
    build_application_container,
)
from .dispatcher import ApplicationDispatcher, DispatchResult
from .operator import (
    ApprovalResponse,
    ApprovalService,
    OperatorResponse,
    OperatorService,
    control_result_to_dict,
    runtime_result_to_dict,
)


def __getattr__(name: str):
    if name in {"build_parser", "run_cli"}:
        from .cli import build_parser, run_cli

        return {"build_parser": build_parser, "run_cli": run_cli}[name]
    raise AttributeError(name)

__all__ = [
    "AgentApplication",
    "ApplicationBootstrapConfig",
    "ApplicationContainer",
    "ApplicationDispatcher",
    "ApplicationResult",
    "ApprovalResponse",
    "ApprovalService",
    "DispatchResult",
    "OperatorResponse",
    "OperatorService",
    "build_parser",
    "build_application_container",
    "control_result_to_dict",
    "runtime_result_to_dict",
    "run_cli",
]
