"""Tracing (OpenTelemetry): one trace per conversation, the same module design as the other projects.

    conversation (root, opened when the session starts)
      ├─ turn                 (one per customer message; agent arch, tool calls, latency)
      │    ├─ router          (routed arch: which specialist)
      │    ├─ llm             (model, tokens, cache, cost, seconds)
      │    ├─ tool            (name, tier, outcome, gate rule, amount at stake)
      │    └─ ...
      └─ approval.decision    (a supervisor approves or rejects, in a later request)

Every customer message is its own HTTP request, possibly on another Lambda instance, so the session stores the
root's ids and each turn is started as a child of that root: the whole conversation is one trace.

Spans follow the OpenTelemetry GenAI conventions for model calls (gen_ai.*) and set OpenInference span kinds so
LLM-aware viewers (Arize Phoenix locally) lay them out. Tracing is off unless an exporter is configured:
  ORDER_OPS_TRACE_ENDPOINT=http://localhost:6006/v1/traces   any OTLP/HTTP backend (Phoenix locally)
  ORDER_OPS_TRACE_XRAY=1                                     AWS X-Ray's OTLP endpoint, signed with the
                                                             function's own role (needs Transaction Search)
Spans carry ids, tool names, outcomes, gate rules, amounts, tokens, cost and timing. No customer or agent text and
no tool arguments (emails, names and addresses are personal data). ORDER_OPS_TRACE_CONTENT=1 adds the text, for
local debugging only.

Local viewer:  uvx --python 3.12 --from "arize-phoenix==21.0.0" phoenix serve   (UI on http://localhost:6006)
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

SERVICE = "order-ops-agent"
_provider: Any = None
_tracer: trace.Tracer = trace.get_tracer("order_ops")


def setup(service: str = SERVICE) -> bool:
    """Install an exporter if one is configured. Idempotent. Returns whether tracing is on."""
    if _provider is not None:
        return True
    endpoint = os.environ.get("ORDER_OPS_TRACE_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    xray = os.environ.get("ORDER_OPS_TRACE_XRAY") == "1"
    if not (endpoint or xray):
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    if xray:
        region = os.environ.get("AWS_REGION", "us-west-2")
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"https://xray.{region}.amazonaws.com/v1/traces", session=_sigv4_session(region))))
    install(provider)
    return True


def install(provider: Any) -> None:
    """Use this tracer provider (setup() calls it; tests pass one with an in-memory exporter)."""
    global _provider, _tracer
    _provider = provider
    _tracer = provider.get_tracer("order_ops")


def enabled() -> bool:
    return _provider is not None


def flush(timeout_ms: int = 2000) -> None:
    """Send buffered spans now. Lambda freezes the process after the response, so the API calls this per request."""
    if _provider is not None:
        _provider.force_flush(timeout_ms)


def content_allowed() -> bool:
    return os.environ.get("ORDER_OPS_TRACE_CONTENT") == "1"


@contextmanager
def span(name: str, kind: str = "CHAIN", **attrs: Any) -> Iterator[trace.Span]:
    """A span with an OpenInference kind and plain attributes (None values are dropped)."""
    with _tracer.start_as_current_span(name) as s:
        s.set_attribute("openinference.span.kind", kind)
        set_attrs(s, **attrs)
        yield s


def set_attrs(s: trace.Span, **attrs: Any) -> None:
    for k, v in attrs.items():
        if v is not None:
            s.set_attribute(k, v)


# ---------- one trace per conversation ----------
def start_conversation(session_id: str, **attrs: Any) -> dict | None:
    """Open and close the root span of a conversation; returns its ids to store with the session (None when off)."""
    if _provider is None:
        return None
    with span("conversation", "AGENT", **{"session.id": session_id}, **attrs) as s:
        ctx = s.get_span_context()
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


@contextmanager
def in_conversation(ids: dict | None) -> Iterator[None]:
    """Spans opened inside this block become children of the conversation's root span."""
    if not ids or _provider is None:
        yield
        return
    parent = SpanContext(trace_id=int(ids["trace_id"], 16), span_id=int(ids["span_id"], 16), is_remote=True,
                         trace_flags=TraceFlags(TraceFlags.SAMPLED))
    token = otel_context.attach(trace.set_span_in_context(NonRecordingSpan(parent)))
    try:
        yield
    finally:
        otel_context.detach(token)


def xray_id(trace_id: str) -> str:
    """X-Ray shows the same id as 1-<first 8 hex>-<last 24 hex>."""
    return f"1-{trace_id[:8]}-{trace_id[8:]}"


def trace_url(ids: dict | None) -> str | None:
    """Link for the supervisor console, from ORDER_OPS_TRACE_URL with {trace_id} / {xray_id} placeholders."""
    template = os.environ.get("ORDER_OPS_TRACE_URL")
    if not ids or not template:
        return None
    return template.format(trace_id=ids["trace_id"], xray_id=xray_id(ids["trace_id"]))


# ---------- AWS X-Ray: OTLP over HTTP, SigV4-signed ----------
def _sigv4_session(region: str):
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session as BotoSession

    credentials = BotoSession().get_credentials()

    class SigV4Session(requests.Session):
        def request(self, method, url, data=None, headers=None, **kwargs):  # noqa: ANN001
            req = AWSRequest(method=method, url=url, data=data, headers=dict(headers or {}))
            SigV4Auth(credentials.get_frozen_credentials(), "xray", region).add_auth(req)
            return super().request(method, url, data=data, headers=dict(req.headers), **kwargs)

    return SigV4Session()
