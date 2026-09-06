from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from agrocast.core.contracts import EmptyQuery, JobCancelBody, QueueEventsQuery
from agrocast.identity.credentials import IdentityError
from agrocast.serve.accounts import Actor
from agrocast.serve.errors import ERROR_RESPONSES
from agrocast.serve.responses import DurableJobResponse, QueueEventsResponse, QueueStatsResponse

router = APIRouter(responses=ERROR_RESPONSES)
NoQuery = Annotated[EmptyQuery, Query()]
EventsQuery = Annotated[QueueEventsQuery, Query()]


def _durable_view(request: Request, actor, job_id: str):
    row = request.app.state.identity.get_resource("jobs", actor, job_id)
    if row.get("queue_kind") is None:
        raise IdentityError("resource_not_found", 404)
    return request.app.state.queue.durable_view(row)


@router.get("/api/queue/jobs/{job_id}", response_model=DurableJobResponse)
def durable_job(job_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"job": _durable_view(request, actor, str(job_id))}


@router.post("/api/jobs/{job_id}/cancel", response_model=DurableJobResponse)
def cancel_job(job_id: UUID, request: Request, actor: Actor, body: JobCancelBody = JobCancelBody(), query: NoQuery = EmptyQuery()):
    queue = request.app.state.queue
    result = queue.cancel(actor, str(job_id), body.reason)
    if result["status"] == "running":
        row = request.app.state.identity.get_resource("jobs", actor, str(job_id))
        return {"job": queue.durable_view(row)}
    return {"job": _durable_view(request, actor, str(job_id))}


@router.get("/api/jobs/{job_id}/events", response_model=QueueEventsResponse)
def job_events(job_id: UUID, request: Request, actor: Actor, query: EventsQuery):
    request.app.state.identity.get_resource("jobs", actor, str(job_id))
    return {"events": request.app.state.queue.list_events(str(job_id), since=query.since, limit=query.limit)}


@router.get("/api/queue/stats", response_model=QueueStatsResponse)
def queue_stats(request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return request.app.state.queue.stats(actor)
