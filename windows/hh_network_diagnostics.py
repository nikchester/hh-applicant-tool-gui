"""Bounded, metadata-only observations of HH Requests traffic (never a retry policy)."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import platform
import re
import sys
import time
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

import requests
import urllib3
from urllib3.util import Timeout


def target(request):
    """Only constant origins/routes: even a subdomain can contain private data."""
    try:
        url = urlsplit(request.url)
        hh = (url.scheme == "https" and url.hostname is not None
              and (url.hostname == "hh.ru" or url.hostname.endswith(".hh.ru")))
        if not hh:
            return None
        origin = ("https://" + url.hostname if url.hostname in
                  {"hh.ru", "api.hh.ru", "chatik.hh.ru"} and url.port in (None, 443)
                  else "other")
        routes = {
            "/": "bootstrap", "/applicant/my_resumes": "discovery",
            "/chatik/api/unread": "chat-list", "/chatik/api/chats": "chat-list",
            "/chatik/api/chat_data": "chat-read", "/chatik/api/send": "chat-send",
            "/token": "token-refresh", "/account/captcha": "captcha",
            "/captcha": "captcha", "/captcha/picture": "captcha",
            "/me": "discovery", "/resumes/mine": "discovery",
        }
        route = url.path if url.path in routes else "other"
        if re.fullmatch(r"/chat/[0-9]+", url.path):
            route = "/chat/{id}"
        elif re.fullmatch(r"/resumes/[^/]+", url.path):
            route = "/resumes/{id}"
        method = request.method if request.method in {"GET", "POST", "HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"} else "other"
        phase = "chat-read" if route == "/chat/{id}" else routes.get(route, "discovery")
        return {"origin": origin, "route": route, "phase": phase,
                "method": method}
    except (ValueError, TypeError, AttributeError):
        return None


def finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def transport_options(request, options):
    proxy = requests.utils.select_proxy(request.url, options.get("proxies") or {})
    proxy_info = {"mode": "proxied" if proxy else "direct"}
    if proxy:
        proxy_info.update(scheme="other", port=None, loopback=False)
        try:
            parsed = urlsplit(proxy if "://" in proxy else "http://" + proxy)
            proxy_info["scheme"] = parsed.scheme if parsed.scheme in {
                "http", "https", "socks4", "socks4a", "socks5", "socks5h"} else "other"
            proxy_info["port"] = parsed.port
            host = parsed.hostname
            if host == "localhost":
                proxy_info["loopback"] = True
            else:
                try:
                    proxy_info["loopback"] = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    pass
        except (TypeError, ValueError):
            pass
    timeout = options.get("timeout")
    if isinstance(timeout, Timeout):
        # Read the configured values, not read_timeout (which depends on a started timer).
        timing = {"kind": "urllib3", "connect": finite(timeout._connect),
                  "read": finite(timeout._read), "total": finite(timeout.total)}
    elif isinstance(timeout, tuple) and len(timeout) == 2:
        timing = {"kind": "pair", "connect": finite(timeout[0]), "read": finite(timeout[1])}
    else:
        timing = {"kind": "scalar", "connect": finite(timeout), "read": finite(timeout)}
    verify = options.get("verify", True)
    return {"proxy": proxy_info, "tls": "disabled" if not verify else
            "custom_ca" if isinstance(verify, (str, bytes, Path)) else "verified",
            "client_cert": bool(options.get("cert")), "timeout": timing,
            "stream": bool(options.get("stream", False))}


class NoDiagnostics:
    def guard(self, request):
        return nullcontext()

    def sending(self, request):
        return nullcontext()

    def decision(self, event, **facts):
        pass


class NetworkDiagnostics:
    MAX_BYTES = 512 * 1024
    RESERVE = 2048

    def __init__(self, failure_facts, directory=None, *, max_bytes=MAX_BYTES):
        self.failure_facts = failure_facts
        self.run_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.stack = ContextVar("hh_diagnostic_stack", default=())
        self.path, self.file = None, None
        self.size, self.limit = 0, max(4096, max_bytes)
        self.warned = self.broken = self.capped = self.finished = False
        try:
            directory = Path(directory) if directory is not None else Path.home() / ".local/state/pocketnode-hh/run"
            directory.mkdir(parents=True, exist_ok=True)
            candidate = directory / f"hh-chats-{self.run_id}.jsonl"
            self.file = candidate.open("xb")  # Never truncate or claim a previous log.
            self.path = candidate
        except OSError:
            self._warn()
        version = lambda value: value if isinstance(value, str) and re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", value) else "other"
        self.event("run_started", python=version(platform.python_version()),
                   requests=version(requests.__version__), urllib3=version(urllib3.__version__))

    def _warn(self):
        self.broken = True
        if not self.warned:
            self.warned = True
            try:
                print("HH network diagnostics unavailable or incomplete; network policy unchanged.", file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass

    def event(self, event, *, final=False, **facts):
        if (self.file is None or self.broken
                or ((self.capped or self.finished) and not final)):
            return
        try:
            record = {"event": event, "run_id": self.run_id,
                      "utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                      "monotonic_ms": round((time.monotonic() - self.started) * 1000, 3), **facts}
            line = (json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
            if self.size + len(line) > self.limit - (0 if final else self.RESERVE):
                if not final:
                    self.event("log_capped", final=True)
                    self.capped = True
                return
            self.file.write(line)
            self.file.flush()
            self.size += len(line)
        except (OSError, ValueError, TypeError):
            self._warn()

    @contextmanager
    def guard(self, request):
        facts = target(request)
        if facts is None:
            # Exclude local model traffic, but still distinguish an abort inside
            # a nested guard (including a blocked non-HH redirect) in its parent.
            try:
                yield
            except BaseException:
                parents = self.stack.get()
                if parents:
                    parents[-1]["child_abort"] = True
                raise
            return
        parents = self.stack.get()
        if parents and facts["route"] == "other":
            facts["phase"] = parents[-1]["phase"]
        frame = {"request_id": uuid.uuid4().hex, "hop_id": None, "hops": 0,
                 "failure": False, "child_abort": False, "redirect": False, **facts}
        frame["logical_request_id"] = parents[0]["logical_request_id"] if parents else frame["request_id"]
        frame["parent_request_id"] = parents[-1]["request_id"] if parents else None
        frame["parent_hop_id"] = parents[-1]["hop_id"] if parents else None
        token = self.stack.set((*parents, frame))
        self.event("guard_started", **self._ids(frame), **facts)
        try:
            yield
        except BaseException:
            self.event("guard_aborted", **self._ids(frame))
            if parents:
                parents[-1]["child_abort"] = True
            raise
        else:
            self.event("guard_completed", **self._ids(frame))
        finally:
            self.stack.reset(token)

    @staticmethod
    def _ids(frame):
        return {key: frame[key] for key in ("request_id", "logical_request_id", "parent_request_id", "parent_hop_id", "hop_id")}

    @contextmanager
    def sending(self, request):
        frames = self.stack.get()
        if not frames or target(request) is None:
            yield
            return
        frame = frames[-1]
        before = frame["hops"]
        frame.update(hop_id=None, failure=False, child_abort=False, redirect=False)
        self.event("send_started", **self._ids(frame))
        try:
            yield
        except BaseException:
            stage = ("child_abort" if frame["child_abort"] else "observed_failure" if frame["failure"]
                     else "redirect_processing" if frame["redirect"] else "send_processing")
            # These are lifecycle markers, not another transport failure count.
            self.event("send_aborted", stage=stage, **self._ids(frame))
            raise
        else:
            self.event("send_completed", **self._ids(frame))
        finally:
            if frame["hops"] == before:
                self.event("adapter_gap", **self._ids(frame))

    def decision(self, event, **facts):
        frames = self.stack.get()
        if frames:
            self.event(event, **self._ids(frames[-1]), **facts)

    def _failure(self, frame, ids, stage, error):
        frame["failure"] = True
        try:
            category, detail = self.failure_facts(error)
        except Exception:
            category, detail = "unknown", "diagnostic facts unavailable"
        self.event("hop_failed", **ids, stage=stage, category=category, detail=detail)

    def adapter_send(self, original, adapter, *args, **kwargs):
        options = dict(zip(("request", "stream", "timeout", "verify", "cert", "proxies"), args))
        options.update(kwargs)
        request = options.get("request")
        facts = target(request)
        frames = self.stack.get()
        if facts is None or not frames:
            return original(adapter, *args, **kwargs)
        frame = frames[-1]
        if facts["route"] == "other":
            facts["phase"] = frame["phase"]
        frame["hop_id"] = uuid.uuid4().hex
        frame["hops"] += 1
        ids = self._ids(frame)
        try:
            effective = transport_options(request, options)
        except Exception:
            effective = {"options_unavailable": True}
        self.event("hop_started", **ids, **facts, **effective)
        try:
            response = original(adapter, *args, **kwargs)
        except BaseException as error:
            self._failure(frame, ids, "response_headers", error)
            raise
        status = response.status_code
        redirect = "location" in response.headers
        frame["redirect"] = redirect and status in (301, 302, 303, 307, 308)
        self.event("headers_received", **ids, status=status if type(status) is int else None,
                   redirect_present=redirect)
        # Requests consumes this lazily (including redirect bodies). Never change
        # stream, peek at bytes, or consume a response on behalf of the caller.
        if isinstance(response, requests.Response) and not response._content_consumed:
            original_iter = response.iter_content
            started = completed = failed = False

            def iter_content(*a, **kw):
                # Preserve synchronous argument validation as well as lazy reads.
                chunks = original_iter(*a, **kw)
                uncached = not response._content_consumed

                def observed_chunks():
                    nonlocal started, completed, failed
                    if not uncached:
                        yield from chunks
                        return
                    if not started:
                        started = True
                        self.event("body_started", **ids)
                    try:
                        yield from chunks
                    except Exception as error:
                        if not failed:
                            failed = True
                            self._failure(frame, ids, "response_body", error)
                        raise
                    else:
                        if not completed:
                            completed = True
                            self.event("body_completed", **ids)

                return observed_chunks()

            response.iter_content = iter_content
        return response

    @contextmanager
    def installed(self):
        original = requests.adapters.HTTPAdapter.send

        def observed(adapter, *args, **kwargs):
            return self.adapter_send(original, adapter, *args, **kwargs)

        with patch.object(requests.adapters.HTTPAdapter, "send", observed):
            yield

    def finish(self, *, successful, complete):
        if self.finished:
            return
        self.finished = True
        self.event("run_finished", final=True, successful=successful, complete=complete,
                   capped=self.capped)
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                self._warn()
        if self.path is None:
            return
        if successful and complete and not self.broken and not self.capped:
            try:
                self.path.unlink()  # Only the exclusive file created by this instance.
                return
            except OSError:
                self._warn()
        try:
            print(f"HH network diagnostic log preserved: {self.path}", flush=True)
        except (OSError, ValueError):
            self._warn()
