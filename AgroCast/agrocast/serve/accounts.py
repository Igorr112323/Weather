from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse

from agrocast.core.contracts import EmptyQuery, ListQuery
from agrocast.identity.credentials import IdentityError, Principal
from agrocast.serve.errors import ERROR_RESPONSES
from agrocast.serve.responses import (

    AccountDeleteBody,
    AccountExportResponse,
    CropResponse, CropsResponse, SubscriptionResponse, SubscriptionsResponse, JobResponse, JobsResponse,
    LoginResponse, SessionResponse, UserResponse, UsersResponse, EventsResponse, FieldResponse, FieldsResponse,
    PublicationResponse, PublicationsResponse,
)
from agrocast.serve.account_models import (
    CropBody, FieldBody, LoginBody, PasswordBody, SubscriptionBody, UserBody, UserUpdateBody,
)
from agrocast.serve.pilot import historical_result
from agrocast.serve.security import SESSION_COOKIE

router = APIRouter(responses=ERROR_RESPONSES)
STATIC = Path(__file__).resolve().parents[2] / "static"
PageQuery = Annotated[ListQuery, Query()]
NoQuery = Annotated[EmptyQuery, Query()]


def current_principal(request: Request):
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise IdentityError("auth_required", 401)
    return principal


Actor = Annotated[Principal, Depends(current_principal)]


def clear_session(response):
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


@router.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(STATIC / "login.html", media_type="text/html")


@router.get("/login.js", include_in_schema=False)
def login_script():
    return FileResponse(STATIC / "login.js", media_type="application/javascript")


@router.post("/api/auth/login", response_model=LoginResponse)
def login(body: LoginBody, request: Request, response: Response, query: NoQuery = EmptyQuery()):
    identity = request.app.state.identity
    result = identity.login(body.username, body.password.get_secret_value())
    response.set_cookie(
        SESSION_COOKIE, result.token, max_age=identity.session_seconds,
        expires=datetime.fromtimestamp(result.expires_at, tz=timezone.utc),
        path="/", secure=True, httponly=True, samesite="lax",
    )
    return {"user": result.principal.public(), "csrf_token": result.principal.csrf_token, "expires_at": result.expires_at}


@router.get("/api/auth/me", response_model=SessionResponse)
def me(actor: Actor, query: NoQuery = EmptyQuery()):
    return {"user": actor.public(), "csrf_token": actor.csrf_token}


@router.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response, actor: Actor, query: NoQuery = EmptyQuery()):
    request.app.state.identity.logout(actor)
    clear_session(response)


@router.post("/api/auth/password", status_code=204)
def change_password(body: PasswordBody, request: Request, response: Response, actor: Actor, query: NoQuery = EmptyQuery()):
    request.app.state.identity.change_password(actor, body.current_password.get_secret_value(), body.new_password.get_secret_value())
    clear_session(response)


@router.get("/api/admin/users", response_model=UsersResponse)
def list_users(request: Request, actor: Actor, query: PageQuery):
    return {"users": request.app.state.identity.list_users(actor, query.limit)}


@router.post("/api/admin/users", response_model=UserResponse, status_code=201)
def create_user(body: UserBody, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"user": request.app.state.identity.create_user(actor, body.username, body.password.get_secret_value(), body.role)}


@router.patch("/api/admin/users/{user_id}", response_model=UserResponse)
def update_user(user_id: UUID, body: UserUpdateBody, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"user": request.app.state.identity.update_user(actor, str(user_id), body.role, body.active)}


@router.get("/api/admin/events", response_model=EventsResponse)
def audit_events(request: Request, actor: Actor, query: PageQuery):
    return {"events": request.app.state.identity.events(actor, query.limit)}


@router.get("/api/account/export", response_model=AccountExportResponse)
def account_export(request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"export": request.app.state.identity.export_user(actor)}


@router.delete("/api/account", status_code=204)
def account_delete(body: AccountDeleteBody, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    request.app.state.identity.delete_account(actor, body.password)


@router.get("/api/fields", response_model=FieldsResponse)
def list_fields(request: Request, actor: Actor, query: PageQuery):
    return {"fields": request.app.state.identity.list_resources("fields", actor, query.limit)}


@router.get("/api/fields/{field_id}", response_model=FieldResponse)
def get_field(field_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"field": request.app.state.identity.get_resource("fields", actor, str(field_id))}


@router.post("/api/fields", response_model=FieldResponse, status_code=201)
def create_field(body: FieldBody, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"field": request.app.state.identity.write_resource("fields", actor, body.model_dump(mode="json"))}


@router.put("/api/fields/{field_id}", response_model=FieldResponse)
def update_field(field_id: UUID, body: FieldBody, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"field": request.app.state.identity.write_resource("fields", actor, body.model_dump(mode="json"), str(field_id))}


@router.delete("/api/fields/{field_id}", status_code=204)
def delete_field(field_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    request.app.state.identity.delete_resource("fields", actor, str(field_id))


@router.get("/api/crops", response_model=CropsResponse)
def list_crops(request: Request, actor: Actor, query: PageQuery):
    return {"crops": request.app.state.identity.list_resources("crops", actor, query.limit)}


@router.get("/api/crops/{crop_id}", response_model=CropResponse)
def get_crop(crop_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"crop": request.app.state.identity.get_resource("crops", actor, str(crop_id))}


@router.post("/api/crops", response_model=CropResponse, status_code=201)
def create_crop(body: CropBody, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"crop": request.app.state.identity.write_resource("crops", actor, body.model_dump(mode="json"))}


@router.put("/api/crops/{crop_id}", response_model=CropResponse)
def update_crop(crop_id: UUID, body: CropBody, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"crop": request.app.state.identity.write_resource("crops", actor, body.model_dump(mode="json"), str(crop_id))}


@router.delete("/api/crops/{crop_id}", status_code=204)
def delete_crop(crop_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    request.app.state.identity.delete_resource("crops", actor, str(crop_id))


@router.get("/api/subscriptions", response_model=SubscriptionsResponse)
def list_subscriptions(request: Request, actor: Actor, query: PageQuery):
    return {"subscriptions": request.app.state.identity.list_resources("subscriptions", actor, query.limit)}


@router.get("/api/subscriptions/{subscription_id}", response_model=SubscriptionResponse)
def get_subscription(subscription_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"subscription": request.app.state.identity.get_resource("subscriptions", actor, str(subscription_id))}


@router.post("/api/subscriptions", response_model=SubscriptionResponse, status_code=201)
def create_subscription(body: SubscriptionBody, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"subscription": request.app.state.identity.write_resource("subscriptions", actor, body.model_dump(mode="json"))}


@router.put("/api/subscriptions/{subscription_id}", response_model=SubscriptionResponse)
def update_subscription(subscription_id: UUID, body: SubscriptionBody, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return {"subscription": request.app.state.identity.write_resource("subscriptions", actor, body.model_dump(mode="json"), str(subscription_id))}


@router.delete("/api/subscriptions/{subscription_id}", status_code=204)
def delete_subscription(subscription_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    request.app.state.identity.delete_resource("subscriptions", actor, str(subscription_id))


@router.get("/api/jobs", response_model=JobsResponse)
def list_jobs(request: Request, actor: Actor, query: PageQuery):
    return historical_result({"jobs": request.app.state.identity.list_resources("jobs", actor, query.limit)})


@router.get("/api/job/{job_id}", response_model=JobResponse)
@router.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return historical_result({"job": request.app.state.identity.get_resource("jobs", actor, str(job_id))})


@router.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    request.app.state.identity.delete_resource("jobs", actor, str(job_id))


@router.get("/api/publications", response_model=PublicationsResponse)
def list_publications(request: Request, actor: Actor, query: PageQuery):
    return historical_result({"publications": request.app.state.identity.list_resources("publications", actor, query.limit)})


@router.get("/api/publications/{publication_id}", response_model=PublicationResponse)
def get_publication(publication_id: UUID, request: Request, actor: Actor, query: NoQuery = EmptyQuery()):
    return historical_result({"publication": request.app.state.identity.get_resource("publications", actor, str(publication_id))})
