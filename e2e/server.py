"""API only: no scheduler, marketplace, messenger, or paid AI clients."""

import asyncio
import os
import socket

import uvicorn
from app.api.main import create_app
from app.config import load_settings
from app.db.engine import build_engine
from app.db.session import build_session_factory

from e2e.runtime import database_url


async def main() -> None:
    engine = build_engine(database_url())
    sock = socket.socket(fileno=int(os.environ["WB_E2E_SOCKET_FD"]))
    if sock.getsockname()[0] != "127.0.0.1":
        raise RuntimeError("E2E server must use a loopback socket")
    try:
        app = create_app(build_session_factory(engine), load_settings())
        config = uvicorn.Config(app, log_level="warning", access_log=False)
        await uvicorn.Server(config).serve(sockets=[sock])
    finally:
        sock.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
