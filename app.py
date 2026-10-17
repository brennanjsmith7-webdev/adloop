"""Vercel's FastAPI entry point wraps the authenticated MCP ASGI service."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from adloop.hosted import HostedApplication

hosted = HostedApplication()

@asynccontextmanager
async def lifespan(app):
    if hosted.inner is None:
        yield
    else:
        async with hosted.inner.lifespan(app):
            yield

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/", hosted)
