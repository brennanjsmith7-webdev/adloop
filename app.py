"""Vercel ASGI entry point. Missing configuration fails closed with HTTP 503."""
from adloop.hosted import HostedApplication

app = HostedApplication()
