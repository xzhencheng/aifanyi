import hashlib
import hmac
import ipaddress
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from .config import secret_value
from .db import Event, Record, now
from .errors import require

DELAYS = [60, 300, 900, 3600, 21600, 86400]


class CallbackRetry(Exception):
    def __init__(self, retry_after=0):
        self.retry_after = retry_after


def retry_after_seconds(value):
    try:
        return min(86400, max(0, int(value)))
    except (TypeError, ValueError):
        try:
            return min(
                86400, max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            )
        except (TypeError, ValueError, OverflowError):
            return 0


def approved_addresses(url, settings):
    parsed = urlparse(url)
    require(
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.hostname in settings.callback_allowed_hosts
        and not parsed.fragment,
        "CALLBACK_URL_REJECTED",
        "回调地址必须是登记的 HTTPS 目标。",
        422,
    )
    networks = [ipaddress.ip_network(x) for x in settings.callback_allowed_cidrs]
    ips = {
        ipaddress.ip_address(x[4][0])
        for x in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    }

    def permitted(ip):
        actual = getattr(ip, "ipv4_mapped", None) or ip
        return not (
            actual.is_loopback or actual.is_link_local or actual.is_multicast or actual.is_unspecified
        ) and any(ip in net for net in networks)

    require(
        ips and all(permitted(ip) for ip in ips),
        "CALLBACK_URL_REJECTED",
        "回调目标地址不在审核后的网络范围。",
        422,
    )
    return parsed, sorted(str(ip) for ip in ips)


def validate_url(url, settings):
    approved_addresses(url, settings)
    return url


class PinnedTransport(httpx.HTTPTransport):
    """Connect to the checked IP while retaining the original Host and TLS certificate verification."""

    def __init__(self, settings):
        super().__init__(trust_env=False, retries=0)
        self.settings = settings

    def handle_request(self, request):
        parsed, addresses = approved_addresses(str(request.url), self.settings)
        request.extensions["sni_hostname"] = parsed.hostname
        # Host header is already derived from the original URL, including a non-default port.
        request.url = request.url.copy_with(host=addresses[0])
        return super().handle_request(request)


def sign(secret, timestamp, event_id, raw):
    return hmac.new(secret.encode(), f"{timestamp}.{event_id}.{raw}".encode(), hashlib.sha256).hexdigest()


def deliver(db, settings, work):
    with db.session() as s:
        event = s.get(Event, work.target)
        endpoint = s.get(Record, work.data["endpoint_id"])
        require(
            event and endpoint and endpoint.data.get("enabled"), "CALLBACK_DISABLED", "事件或回调端点不可用。"
        )
        data = endpoint.data
        client = s.get(Record, data["client_id"])
        require(client and client.data.get("enabled"), "CALLBACK_DISABLED", "调用系统已停用。")
        require(data.get("expires", 0) > now(), "CALLBACK_DISABLED", "回调端点已过期。")
        require(event.type in data["events"], "CALLBACK_DISABLED", "端点未订阅该事件。")
        secret = secret_value(data["secret_env"])
        require(len(secret) >= 32, "CALLBACK_SECRET_MISSING", "回调密钥未配置。")
        ts = str(int(now()))
        headers = {
            "Content-Type": "application/json",
            "X-Translation-Key-Id": data["key_id"],
            "X-Translation-Timestamp": ts,
            "X-Translation-Event-Id": event.id,
            "X-Translation-Signature": "v1=" + sign(secret, ts, event.id, event.raw_body),
        }
        raw = event.raw_body
    with httpx.Client(
        timeout=10, follow_redirects=False, trust_env=False, transport=PinnedTransport(settings)
    ) as client:
        response = client.post(data["url"], headers=headers, content=raw.encode())
    if 200 <= response.status_code < 300:
        return {"status": "delivered", "response_code": response.status_code}
    require(
        response.status_code in {408, 429} or response.status_code >= 500,
        "CALLBACK_PERMANENT_FAILURE",
        "端点拒绝通知或返回重定向。",
    )
    raise CallbackRetry(retry_after_seconds(response.headers.get("Retry-After")))
