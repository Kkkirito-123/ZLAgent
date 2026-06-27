"""FastAPI 路由挂载。"""
from __future__ import annotations

from fastapi import FastAPI

from ..api import confirmations as confirmations_api
from ..api import cron as cron_api
from ..api import curator as curator_api
from ..api import delivery_targets as delivery_targets_api
from ..api import doctor as doctor_api
from ..api import gateways as gateways_api
from ..api import graph_rag as graph_rag_api
from ..api import health as health_api
from ..api import knowledge_bases as knowledge_bases_api
from ..api import llm as llm_api
from ..api import maintenance as maintenance_api
from ..api import mcp as mcp_api
from ..api import memory as memory_api
from ..api import nightly as nightly_api
from ..api import plugins as plugins_api
from ..api import review as review_api
from ..api import runtime as runtime_api
from ..api import skills as skills_api
from ..api import tools as tools_api
from ..api import weixin as weixin_api
from ..api import wiki as wiki_api
from ..harness.extensions import api as harness_api
from ..harness.observability import api as harness_obs_api


def mount_api_routes(app: FastAPI) -> None:
    """挂载所有 HTTP API 路由。"""
    app.include_router(health_api.root_router)
    app.include_router(health_api.router)
    app.include_router(runtime_api.router)
    app.include_router(graph_rag_api.router)
    app.include_router(skills_api.router)
    app.include_router(delivery_targets_api.router)
    app.include_router(cron_api.router)
    app.include_router(llm_api.router)
    app.include_router(tools_api.router)
    app.include_router(confirmations_api.router)
    app.include_router(curator_api.router)
    app.include_router(memory_api.router)
    app.include_router(knowledge_bases_api.router)
    app.include_router(maintenance_api.router)
    app.include_router(nightly_api.router)
    app.include_router(review_api.router)
    app.include_router(mcp_api.router)
    app.include_router(wiki_api.router)
    app.include_router(weixin_api.router)
    app.include_router(gateways_api.build_router())
    app.include_router(doctor_api.build_router())
    app.include_router(plugins_api.build_router())
    app.include_router(harness_api.router)
    app.include_router(harness_obs_api.router)
