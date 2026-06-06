from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request

from feature_skills_webapp.web.broadcaster import Broadcaster


async def events(request: Request) -> EventSourceResponse:
    broadcaster: Broadcaster | None = getattr(request.app.state, "broadcaster", None)

    async def stream():
        yield {"data": "changed"}
        if broadcaster is None:
            return
        q = broadcaster.register()
        try:
            while True:
                msg = await q.get()
                yield {"data": msg}
        finally:
            broadcaster.unregister(q)

    return EventSourceResponse(stream())
