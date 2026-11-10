"""A tiny fake customer server for demos and manual testing.

It receives webhooks at POST /hook, checks the signature the way a real customer should, and can be told to
misbehave so you can watch the platform's retries, dead-letter queue and circuit breaker at work.

Run:    uvicorn demo.receiver:app --port 9000

Control it:
    PUT    /_demo/secret    {"secret": "whsec_..."}    the endpoint's signing secret, so signatures can be checked
    PUT    /_demo/mode      {"mode": "ok"}             ok | error500 | gone | slow | flaky
    GET    /_demo/received                             what it has accepted (with whether the signature was valid)
    DELETE /_demo/received                             forget everything
"""

import asyncio
import os
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Request, Response
from pydantic import BaseModel

from app.signing import verify_signature

MODES = ("ok", "error500", "gone", "slow", "flaky")
FLAKY_FAILURES = 2  # in "flaky" mode every event fails this many times before it is accepted

app = FastAPI(title="Demo webhook receiver")
state: dict = {"secret": os.environ.get("RECEIVER_SECRET"), "mode": "ok", "received": [], "tries": {}}


class SecretBody(BaseModel):
    secret: str


class ModeBody(BaseModel):
    mode: Literal["ok", "error500", "gone", "slow", "flaky"]


@app.put("/_demo/secret")
def set_secret(body: SecretBody) -> dict:
    state["secret"] = body.secret
    return {"ok": True}


@app.put("/_demo/mode")
def set_mode(body: ModeBody) -> dict:
    state["mode"] = body.mode
    state["tries"] = {}
    return {"mode": body.mode}


@app.get("/_demo/received")
def received() -> dict:
    return {"mode": state["mode"], "count": len(state["received"]), "items": state["received"]}


@app.delete("/_demo/received")
def clear() -> dict:
    state["received"] = []
    state["tries"] = {}
    return {"ok": True}


@app.post("/hook")
async def hook(
    request: Request,
    webhook_id: Annotated[str, Header()],
    webhook_timestamp: Annotated[str, Header()],
    webhook_signature: Annotated[str, Header()],
) -> Response:
    body = await request.body()
    valid = bool(state["secret"]) and verify_signature(
        state["secret"], webhook_id, webhook_timestamp, webhook_signature, body
    )
    if not valid:
        return Response("invalid signature", status_code=401)

    mode = state["mode"]
    if mode == "error500":
        return Response("simulated outage", status_code=500)
    if mode == "gone":
        return Response("this URL no longer exists", status_code=410)
    if mode == "slow":
        await asyncio.sleep(30)  # longer than the platform's request timeout
    if mode == "flaky":
        tries = state["tries"].get(webhook_id, 0) + 1
        state["tries"][webhook_id] = tries
        if tries <= FLAKY_FAILURES:
            return Response("simulated hiccup", status_code=503)

    state["received"].append(
        {
            "webhook_id": webhook_id,
            "attempt": request.headers.get("x-webhook-attempt"),
            "signature_valid": True,
            "body": body.decode(),
        }
    )
    return Response("received", status_code=200)
