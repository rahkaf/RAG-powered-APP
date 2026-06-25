"""
AI Knowledge Centre - Prometheus Metrics Middleware
Records HTTP request count and latency with cardinality-safe status labels.

Status codes are grouped into "2xx", "3xx", "4xx", "5xx" buckets to prevent
unbounded label cardinality in Prometheus.
"""

import time

from prometheus_client import Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

HTTP_REQUEST_COUNT = Counter(
    "kc_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_class"],
)
HTTP_REQUEST_LATENCY = Histogram(
    "kc_http_request_latency_seconds",
    "HTTP request latency",
    buckets=[0.1, 0.5, 1, 2, 5, 10],
)


def _status_class(status_code: int) -> str:
    """Map an HTTP status code to a cardinality-safe label like '2xx'."""
    return f"{status_code // 100}xx"


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware that records Prometheus metrics for every HTTP request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.time()
        response = await call_next(request)
        elapsed = time.time() - start

        HTTP_REQUEST_COUNT.labels(
            method=request.method,
            endpoint=request.url.path,
            status_class=_status_class(response.status_code),
        ).inc()

        HTTP_REQUEST_LATENCY.observe(elapsed)

        return response
