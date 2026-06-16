"""CRUD + manual trigger for :class:`CronJob` records."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db.models import CronJob, DeliveryTarget
from ..db.session import get_db

router = APIRouter(prefix="/api/cron", tags=["cron"])


class CronJobIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cron_expr: str = Field(
        description="Standard 5-field cron expression, e.g. '*/1 * * * *'.",
    )
    timezone: str = Field(default="Asia/Shanghai")
    instruction: str = Field(default="", description="Message/content the job should deliver.")
    skill_hint: Optional[str] = Field(
        default=None,
        description="Optional skill id to run when the job fires (reserved; not used in v0.2).",
    )
    delivery_target_id: Optional[int] = Field(
        default=None,
        description="Which delivery target receives the output. Required for push-style jobs.",
    )
    enabled: bool = True
    run_once: bool = False
    lead_minutes: int = Field(default=1, ge=0, le=60)
    # v0.10
    pre_script_path: Optional[str] = Field(
        default=None,
        description=(
            "Workspace-relative path to a .py / .sh script. The script is"
            " run before each tick and its stdout is passed to the LLM as"
            " ground-truth data. Path traversal escapes are rejected."
        ),
    )
    pre_script_timeout_seconds: int = Field(
        default=30,
        ge=1, le=300,
        description="Hard timeout for pre_script execution. Seconds.",
    )


class CronJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    cron_expr: str
    timezone: str
    instruction: str
    skill_hint: Optional[str]
    delivery_target_id: Optional[int]
    enabled: bool
    run_once: bool
    lead_minutes: int
    last_run_at: Optional[datetime]
    last_status: Optional[str]
    last_error: Optional[str]
    pre_script_path: Optional[str]
    pre_script_timeout_seconds: int
    created_at: datetime
    updated_at: datetime


def _validate_cron_expr(expr: str) -> None:
    try:
        croniter(expr)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid cron expression: {exc}") from exc


def _validate_delivery_target(db: Session, target_id: Optional[int]) -> None:
    if target_id is None:
        return
    target = db.get(DeliveryTarget, target_id)
    if target is None:
        raise HTTPException(
            status_code=400, detail=f"delivery_target_id {target_id} does not exist"
        )


@router.get("", response_model=list[CronJobOut])
def list_jobs(db: Session = Depends(get_db)) -> list[CronJob]:
    return list(db.query(CronJob).order_by(CronJob.id.asc()).all())


@router.post("", response_model=CronJobOut, status_code=201)
def create_job(payload: CronJobIn, db: Session = Depends(get_db)) -> CronJob:
    _validate_cron_expr(payload.cron_expr)
    _validate_delivery_target(db, payload.delivery_target_id)

    existing = db.query(CronJob).filter(CronJob.name == payload.name).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="cron job name already exists")

    job = CronJob(
        name=payload.name,
        cron_expr=payload.cron_expr,
        timezone=payload.timezone,
        instruction=payload.instruction,
        skill_hint=payload.skill_hint,
        delivery_target_id=payload.delivery_target_id,
        enabled=payload.enabled,
        run_once=payload.run_once,
        lead_minutes=payload.lead_minutes,
        pre_script_path=payload.pre_script_path,
        pre_script_timeout_seconds=payload.pre_script_timeout_seconds,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.get("/{job_id}", response_model=CronJobOut)
def get_job(job_id: int, db: Session = Depends(get_db)) -> CronJob:
    job = db.get(CronJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="cron job not found")
    return job


@router.put("/{job_id}", response_model=CronJobOut)
def update_job(
    job_id: int, payload: CronJobIn, db: Session = Depends(get_db)
) -> CronJob:
    job = db.get(CronJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="cron job not found")
    _validate_cron_expr(payload.cron_expr)
    _validate_delivery_target(db, payload.delivery_target_id)

    job.name = payload.name
    job.cron_expr = payload.cron_expr
    job.timezone = payload.timezone
    job.instruction = payload.instruction
    job.skill_hint = payload.skill_hint
    job.delivery_target_id = payload.delivery_target_id
    job.enabled = payload.enabled
    job.run_once = payload.run_once
    job.lead_minutes = payload.lead_minutes
    job.pre_script_path = payload.pre_script_path
    job.pre_script_timeout_seconds = payload.pre_script_timeout_seconds
    db.commit()
    db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: int, db: Session = Depends(get_db)) -> None:
    job = db.get(CronJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="cron job not found")
    db.delete(job)
    db.commit()


@router.post("/{job_id}/run")
async def run_once(
    job_id: int, request: Request, db: Session = Depends(get_db)
) -> dict:
    job = db.get(CronJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="cron job not found")

    scheduler = getattr(request.app.state, "cron_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=500, detail="cron scheduler not initialized")

    db.expunge(job)
    try:
        result = await scheduler.execute(job)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"runner failed: {exc}") from exc
    return {"status": "ok", "result": result}
