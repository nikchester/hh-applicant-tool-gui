"""Run the owned ChatMock process with metadata-only network diagnostics."""

from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import platform
import re
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4
from unittest.mock import patch

from chatmock import cli
from chatmock.app import create_app
from chatmock.config import CHATGPT_RESPONSES_URL
from flask import g, has_request_context, request
import requests


GENERATION_PATHS = {"/v1/chat/completions", "/v1/completions", "/v1/responses"}
CONNECT_ATTEMPTS = 4
CONNECT_TIMEOUT = 10
CONNECT_BACKOFFS = (2, 4, 8)  # Seconds before attempts 2, 3 and 4.
# Normal worst case ~55 s. Socket timeouts do not bound an attempt (DNS, several
# addresses, handshake reads), so this caps retries well below the HH client's
# 120 s OpenAI timeout, which also covers generation.
CONNECT_RETRY_BUDGET = 60


def write_diagnostic(prefix, diagnostic):
    try:
        # JSON escaping prevents multiline messages and terminal control sequences.
        print(prefix + " " + json.dumps(diagnostic), file=sys.stderr, flush=True)
    except Exception:
        pass  # Diagnostics must never change the request's result.


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def endpoint(url):
    """Only the origin: never credentials, path, query, or fragment."""
    try:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return "unavailable"
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except (TypeError, ValueError):
        return "unavailable"


def exception_details(error):
    """Follow wrapped transport exceptions without serializing their secret-bearing text."""
    pending, seen, chain = [error], set(), []
    while pending and len(chain) < 16:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        item = {"type": type(current).__module__ + "." + type(current).__name__}
        for name in ("errno", "winerror"):
            value = getattr(current, name, None)
            if isinstance(value, int):
                item[name] = value
        chain.append(item)
        related = [current.__cause__]
        if not current.__suppress_context__:
            related.append(current.__context__)
        related.extend((getattr(current, "reason", None),
                        getattr(current, "original_error", None)))
        related.extend(current.args)
        pending.extend(value for value in related if isinstance(value, BaseException))

    types = {item["type"].rsplit(".", 1)[-1] for item in chain}
    codes = {item[key] for item in chain for key in ("errno", "winerror") if key in item}
    if "gaierror" in types:
        kind = "dns"
    elif "SSLError" in types or "SSLCertVerificationError" in types:
        kind = "tls"
    elif "ConnectionResetError" in types or codes & {10054, 104}:
        kind = "connection_reset"
    elif "ConnectTimeout" in types or "ConnectTimeoutError" in types:
        kind = "connect_timeout"
    elif "ReadTimeout" in types or "ReadTimeoutError" in types:
        kind = "read_timeout"
    elif "ProxyError" in types:
        kind = "proxy"
    else:
        kind = "transport"
    return {"failure_kind": kind, "exception_chain": chain}


def begin_request_diagnostic():
    if request.method == "POST" and request.path in GENERATION_PATHS:
        # Generate locally; an untrusted incoming header is not a diagnostic identifier.
        g.diagnostic_request_id = uuid4().hex


def trace_upstream_send(sender, session, prepared, **kwargs):
    if not (has_request_context() and request.method == "POST"
            and request.path in GENERATION_PATHS
            and urlsplit(prepared.url).hostname == urlsplit(CHATGPT_RESPONSES_URL).hostname):
        return sender(session, prepared, **kwargs)

    started = time.monotonic()
    proxy = requests.utils.select_proxy(prepared.url, kwargs.get("proxies") or {})
    context = {
        "request_id": g.get("diagnostic_request_id"), "upstream_id": uuid4().hex,
        "started_at": utc_now(), "method": prepared.method,
        "target": endpoint(prepared.url),
        "proxy": endpoint(proxy) if proxy else None,
        "tls_verification": kwargs.get("verify", True) is not False,
    }

    def log(event, phase, **fields):
        write_diagnostic("[ChatMock network]", {
            **context, "event": event, "phase": phase, "at": utc_now(),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1), **fields,
        })

    # Requests does not expose a reliable boundary between DNS/TCP/TLS and waiting
    # for headers. Do not misreport this combined phase as a connection failure.
    phase = "connect_or_response_headers" if kwargs.get("stream") else "request_and_body"
    log("request_started", phase)
    # Only this exact generation endpoint gets a bounded connection budget.
    # Preserve the upstream read timeout, proxy selection and TLS verification.
    timeout = kwargs.get("timeout")
    retry_allowed = prepared.method == "POST" and prepared.url == CHATGPT_RESPONSES_URL
    read_timeout = None
    if retry_allowed and isinstance(timeout, (int, float)):
        read_timeout = timeout
        kwargs["timeout"] = (min(timeout, CONNECT_TIMEOUT), timeout)
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        attempt_started = time.monotonic()
        try:
            response = sender(session, prepared, **kwargs)
            break
        except Exception as error:
            details = exception_details(error)
            # urllib3 2.x runs the TLS handshake with the connect timeout but
            # reports its stall as ReadTimeout, before any request bytes are sent.
            # A real read timeout cannot fire before the full read timeout.
            if (read_timeout is not None and isinstance(error, requests.ReadTimeout)
                    and details["failure_kind"] == "read_timeout"
                    and time.monotonic() - attempt_started < read_timeout / 2):
                details["failure_kind"] = "tls_handshake_timeout"
            # Requests explicitly documents ConnectTimeout as safe to retry.
            # Never infer safety from HTTP 502, error text, OS codes, generic
            # ConnectionError, or a failure while consuming the response body.
            retryable = retry_allowed and (
                isinstance(error, requests.ConnectTimeout)
                and details["failure_kind"] == "connect_timeout"
                or details["failure_kind"] == "tls_handshake_timeout")
            # Do not start an attempt that could not fail within the budget.
            over_budget = (retryable and attempt < CONNECT_ATTEMPTS
                           and time.monotonic() - started + CONNECT_BACKOFFS[attempt - 1]
                           + CONNECT_TIMEOUT > CONNECT_RETRY_BUDGET)
            if over_budget:
                details["retry_budget_exhausted"] = True
            log("request_failed", phase, attempt=attempt, **details)
            g.generation_failure = {
                "kind": details["failure_kind"], "attempts": attempt,
                "connect_retry_exhausted": retryable and (attempt == CONNECT_ATTEMPTS or over_budget),
            }
            if not retryable or attempt == CONNECT_ATTEMPTS or over_budget:
                raise
            log("retry_scheduled", "connect", attempt=attempt,
                next_attempt=attempt + 1, backoff_seconds=CONNECT_BACKOFFS[attempt - 1])
            time.sleep(CONNECT_BACKOFFS[attempt - 1])
    g.pop("generation_failure", None)

    headers = {}
    for name in ("x-request-id", "request-id", "cf-ray", "retry-after"):
        value = response.headers.get(name)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.: -]{1,128}", value):
            headers[name] = value
    log("response_received", phase, status=response.status_code, headers=headers)
    if kwargs.get("stream"):
        original_iter = response.iter_content

        def iter_content(*args, **options):
            try:
                yield from original_iter(*args, **options)
            except GeneratorExit:
                log("body_read_stopped", "response_body")
                raise
            except Exception as error:
                log("request_failed", "response_body", **exception_details(error))
                raise
            else:
                log("body_read_complete", "response_body")

        # Both .content and .iter_lines() read via iter_content(); preserve the
        # original Response, streaming behavior, exceptions, and close semantics.
        response.iter_content = iter_content
    return response


@contextmanager
def upstream_diagnostics():
    sender = requests.sessions.Session.send

    def traced_send(session, prepared, **kwargs):
        return trace_upstream_send(sender, session, prepared, **kwargs)

    # Process-local only. Outside generation requests every call passes through.
    with patch.object(requests.sessions.Session, "send", traced_send):
        yield


def log_runtime():
    versions = {"python": platform.python_version()}
    for package in ("chatmock", "requests", "urllib3"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "unknown"
    write_diagnostic("[ChatMock runtime]", {"at": utc_now(), "versions": versions})


def safe_error_message(message):
    """Bound diagnostic output and mask common credential formats in upstream errors."""
    # Transport exception strings may include proxy credentials or request queries.
    message = re.sub(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+", "[URL REDACTED]", message)
    message = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", message)
    message = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = re.sub(
        r'''(?i)(\b(?:access_token|refresh_token|id_token|api_key)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
        lambda match: match[1] + "[REDACTED]",
        message,
    )
    return message if len(message) <= 2000 else message[:2000] + " [truncated]"


def log_error_response(response):
    if (
        response.status_code < 400
        or request.method != "POST"
        or request.path not in GENERATION_PATHS
        or response.is_streamed
    ):
        return response

    if failure := g.get("generation_failure"):
        # Do not return the installed package's raw exception string to callers.
        response.set_data(json.dumps({"error": {
            "message": "ChatGPT connection establishment timed out; check connectivity and gateway logs."
            if failure["kind"] in {"connect_timeout", "tls_handshake_timeout"} else
            "ChatGPT transport failed; check gateway logs; no automatic replay.",
            "generation_failure": {**failure, "request_id": g.get("diagnostic_request_id")},
        }}))
        response.content_type = "application/json"
    payload = response.get_json(silent=True)
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    detail = safe_error_message(message) if isinstance(message, str) else "error.message unavailable"
    diagnostic = {
        "method": request.method,
        "path": request.path,
        "status": response.status_code,
        "error.message": detail,
    }
    if request_id := g.get("diagnostic_request_id"):
        diagnostic["request_id"] = request_id
    write_diagnostic("[ChatMock error]", diagnostic)
    return response


def create_diagnostic_app(*args, **kwargs):
    app = create_app(*args, **kwargs)
    app.before_request(begin_request_diagnostic)
    app.after_request(log_error_response)
    return app


def main():
    # Wrap only this process's factory; do not modify the installed ChatMock package.
    log_runtime()
    with patch.object(cli, "create_app", create_diagnostic_app), upstream_diagnostics():
        cli.main()


if __name__ == "__main__":
    main()
