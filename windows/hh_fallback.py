"""Reach chatik.hh.ru through another HH front node when its own node stalls.

chatik.hh.ru resolves to a single DDoS-Guard node. When that node stalls the TLS
handshake, the api.hh.ru node serves the same host name with a valid certificate.
Only the TCP address changes: SNI and certificate checks still use chatik.hh.ru,
so a node that cannot prove it is chatik.hh.ru is rejected as before.

The retry happens inside HTTPSConnection.connect, before any request byte is sent,
so it is safe for POST /chatik/api/send as well.
"""

from contextlib import contextmanager
import socket
import ssl
from unittest.mock import patch

from urllib3.connection import HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError

HOST = "chatik.hh.ru"
FALLBACK_SOURCE = "api.hh.ru"
FIRST_TRY_TIMEOUT = 4.0
CONNECT_FAILURES = (OSError, ConnectTimeoutError)


def _addresses(host):
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)]
    except OSError:
        return []


def fallback_address():
    """First api.hh.ru address that is not one of chatik.hh.ru's own addresses."""
    own = set(_addresses(HOST))
    return next((address for address in _addresses(FALLBACK_SOURCE) if address not in own), None)


def _retryable(error):
    # A certificate that does not match is a security answer, not a stalled node.
    return isinstance(error, CONNECT_FAILURES) and not isinstance(error, ssl.SSLCertVerificationError)


def _drop_socket(connection):
    sock, connection.sock = connection.sock, None
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


@contextmanager
def chatik_fallback(observer=None, resolve=fallback_address):
    original = HTTPSConnection.connect
    state = {"address": None, "resolved": False, "announced": False}

    def attempt(connection, address, timeout):
        # urllib3 derives `host` from `_dns_host`, so a new address would also change
        # the TLS name; `server_hostname` pins SNI and certificate checks to chatik.hh.ru.
        saved = connection._dns_host, connection.timeout, connection.server_hostname
        connection.timeout = timeout
        if address is not None:
            connection._dns_host, connection.server_hostname = address, saved[2] or HOST
        try:
            return original(connection)
        finally:
            connection._dns_host, connection.timeout, connection.server_hostname = saved

    def connect(connection):
        if connection.host != HOST or connection._tunnel_host is not None:
            return original(connection)
        timeout = connection.timeout
        if state["address"] is not None:
            try:
                return attempt(connection, state["address"], timeout)
            except Exception as error:
                if not _retryable(error):
                    raise
                _drop_socket(connection)
                state["address"] = None  # The spare node failed too: back to DNS.
                return original(connection)
        short = min(timeout, FIRST_TRY_TIMEOUT) if isinstance(timeout, (int, float)) else timeout
        try:
            return attempt(connection, None, short)
        except Exception as error:
            if not _retryable(error):
                raise
            if not state["resolved"]:
                state["resolved"] = True
                state["fallback"] = resolve()
            spare = state.get("fallback")
            if spare is None:
                raise
            _drop_socket(connection)
            try:
                result = attempt(connection, spare, timeout)
            except Exception as spare_error:
                if _retryable(spare_error):
                    raise error from None
                raise
            state["address"] = spare
            if observer is not None and not state["announced"]:
                state["announced"] = True
                observer("fallback", HOST)
            return result

    with patch.object(HTTPSConnection, "connect", connect):
        yield
