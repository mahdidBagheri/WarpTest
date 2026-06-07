"""Test a local warproxy/Cloudflare WARP proxy from Python.

Usage examples:
    python main.py
    python main.py --proxy socks5h://myuser:mypassword@127.0.0.1:1080
    WARPROXY_URL=socks5h://127.0.0.1:1080 python main.py
    docker compose --profile test up --build --abort-on-container-exit proxy-test

Install dependency first, unless you use the Docker image or Compose test service:
    python -m pip install "requests[socks]"
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any


DEFAULT_PROXY_URL = "socks5h://127.0.0.1:1080"
DEFAULT_TEST_URL = "https://api.ipify.org?format=json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test whether HTTP/HTTPS requests are routed through warproxy."
    )
    parser.add_argument(
        "--proxy",
        default=os.getenv("WARPROXY_URL", DEFAULT_PROXY_URL),
        help=(
            "Proxy URL to test. Defaults to WARPROXY_URL or "
            f"{DEFAULT_PROXY_URL!r}. Use socks5h:// so DNS also goes through the proxy."
        ),
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_TEST_URL,
        help=f"URL used to verify the outgoing IP address. Defaults to {DEFAULT_TEST_URL!r}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds. Defaults to 30.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Number of proxied request attempts. Useful while warproxy starts. Defaults to 1.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=3.0,
        help="Seconds to wait between proxied request attempts. Defaults to 3.",
    )
    return parser


def fetch_direct_ip(url: str, timeout: float, requests_module: Any) -> dict[str, Any] | None:
    """Fetch the current public IP without the proxy, if possible."""
    try:
        response = requests_module.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests_module.RequestException as exc:
        print(f"Warning: direct request failed: {exc}", file=sys.stderr)
        return None


def fetch_proxy_ip(
    url: str, proxy_url: str, timeout: float, requests_module: Any
) -> dict[str, Any]:
    """Fetch the public IP through the configured proxy."""
    proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }
    response = requests_module.get(url, proxies=proxies, timeout=timeout)
    response.raise_for_status()
    return response.json()


def main() -> int:
    args = build_parser().parse_args()

    print(f"Testing proxy: {args.proxy}")
    print(f"Test URL: {args.url}")

    try:
        import requests
    except ModuleNotFoundError:
        print(
            "The requests package is required. Install it with: "
            "python -m pip install 'requests[socks]'",
            file=sys.stderr,
        )
        return 2

    direct_result = fetch_direct_ip(args.url, args.timeout, requests)

    proxy_result = None
    last_error: requests.RequestException | None = None
    for attempt in range(1, args.retries + 1):
        try:
            proxy_result = fetch_proxy_ip(args.url, args.proxy, args.timeout, requests)
            break
        except requests.exceptions.InvalidSchema as exc:
            print(
                "Proxy test failed because SOCKS support is missing.\n"
                "Install it with: python -m pip install 'requests[socks]'\n"
                f"Original error: {exc}",
                file=sys.stderr,
            )
            return 2
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= args.retries:
                break
            print(
                f"Proxy attempt {attempt}/{args.retries} failed: {exc}. "
                f"Retrying in {args.retry_delay:g}s...",
                file=sys.stderr,
            )
            time.sleep(args.retry_delay)

    if proxy_result is None:
        print(f"Proxy test failed: {last_error}", file=sys.stderr)
        return 1

    print(f"Direct result: {direct_result if direct_result is not None else 'unavailable'}")
    print(f"Proxy result:  {proxy_result}")

    direct_ip = direct_result.get("ip") if isinstance(direct_result, dict) else None
    proxy_ip = proxy_result.get("ip") if isinstance(proxy_result, dict) else None

    if not proxy_ip:
        print("Proxy request succeeded, but the response did not include an 'ip' field.")
        return 0

    if direct_ip and direct_ip != proxy_ip:
        print("Success: proxy appears to be working because the IP changed.")
    elif direct_ip == proxy_ip:
        print(
            "Proxy request succeeded, but the IP matched the direct request. "
            "This can happen if WARP egress is the same network or if traffic is not being proxied."
        )
    else:
        print("Proxy request succeeded.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
