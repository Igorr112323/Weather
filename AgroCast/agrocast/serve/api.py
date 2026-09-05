from agrocast.serve.security import AccessGuard


def get_config():
    raise RuntimeError("Legacy engine API is retired")


async def _retired(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


async def app(scope, receive, send):
    await AccessGuard(_retired, retired=True)(scope, receive, send)
