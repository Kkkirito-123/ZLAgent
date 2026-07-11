"""Application layer surfaces."""

from typing import Any

from .application import AgentApplication, ApplicationResult
from .bootstrap import (
    ApplicationBootstrapConfig,
    ApplicationContainer,
    ApplicationRuntimeContainer,
    build_application_container,
    build_application_runtime,
)
from .dispatcher import ApplicationDispatcher, DispatchResult
from .local import LocalTaskAdapter, worker_tick_to_dict
from .operator import (
    ApprovalResponse,
    ApprovalService,
    OperatorResponse,
    OperatorService,
    control_result_to_dict,
    runtime_result_to_dict,
)


def __getattr__(name: str) -> Any:
    if name in {"build_parser", "run_cli"}:
        from .cli import build_parser, run_cli

        return {"build_parser": build_parser, "run_cli": run_cli}[name]
    raise AttributeError(name)


__all__ = [
    "AgentApplication",
    "ApplicationBootstrapConfig",
    "ApplicationContainer",
    "ApplicationRuntimeContainer",
    "ApplicationDispatcher",
    "ApplicationResult",
    "ApprovalResponse",
    "ApprovalService",
    "DispatchResult",
    "LocalTaskAdapter",
    "OperatorResponse",
    "OperatorService",
    "build_parser",
    "build_application_container",
    "build_application_runtime",
    "control_result_to_dict",
    "runtime_result_to_dict",
    "run_cli",
    "worker_tick_to_dict",
]
