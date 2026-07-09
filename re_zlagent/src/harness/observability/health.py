"""Standard harness doctor checks."""

from __future__ import annotations

from harness.facade import HarnessFacade

from .doctor import DoctorCheck, DoctorRunner, DoctorStatus


def check_harness_facade(facade: HarnessFacade) -> DoctorCheck:
    """Check whether core harness components are available through the facade."""

    runtime = facade.runtime()
    inventory = facade.inventory()
    missing: list[str] = []
    if not runtime.tool_registry:
        missing.append("tool_registry")
    if not runtime.task_store:
        missing.append("task_store")

    metadata = {
        "components": runtime.to_dict()["components"],
        "inventory_counts": runtime.inventory_counts,
        "inventory_errors": list(inventory.errors),
    }
    if missing:
        return DoctorCheck(
            name="harness.facade",
            status=DoctorStatus.ERROR,
            message="missing required harness components: " + ", ".join(missing),
            metadata=metadata,
        )
    if inventory.errors:
        return DoctorCheck(
            name="harness.facade",
            status=DoctorStatus.WARN,
            message="inventory has subsystem errors",
            metadata=metadata,
        )
    return DoctorCheck(
        name="harness.facade",
        status=DoctorStatus.OK,
        message="harness facade ready",
        metadata=metadata,
    )


def build_harness_doctor(facade: HarnessFacade) -> DoctorRunner:
    """Build a doctor runner with standard harness readiness checks."""

    runner = DoctorRunner()
    runner.register("harness.facade", lambda: check_harness_facade(facade))
    return runner
