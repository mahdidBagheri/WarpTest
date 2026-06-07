"""Use Gemini through a local warproxy/Cloudflare WARP proxy from Python.

Usage examples:
    # Verify that warproxy changes your egress IP.
    python main.py test-proxy
    python main.py test-proxy --proxy socks5h://myuser:mypassword@127.0.0.1:1080
    WARPROXY_URL=socks5h://127.0.0.1:1080 python main.py test-proxy

    # Send a prompt to Gemini through warproxy. Put GEMINI_API_KEY in .env first.
    cp .env.example .env
    # Edit .env so it contains: GEMINI_API_KEY=your_real_api_key
    python main.py gemini --prompt "hi" --model gemini-3.5-flash

    # Or set the key only for the current shell/session instead of using .env:
    export GEMINI_API_KEY="your_real_api_key"
    python main.py gemini --prompt "hi" --model gemini-3.5-flash
    python main.py gemini --prompt "Explain WARP in one sentence."
    python main.py gemini --model gemini-2.5-flash --prompt-file prompt.txt
    echo "Write a haiku about proxies" | python main.py gemini

    docker compose --profile test up --build --abort-on-container-exit proxy-test
    docker compose --profile gemini run --rm gemini --prompt "hi"

    # Ask Docker Compose to restart/recreate warproxy, then verify the proxy IP.
    python main.py rotate-warproxy --recreate

    # Rotate warproxy before each request and verify 10 unique proxied IPs.
    python main.py rotate-requests --count 10 --recreate

Install dependencies first, unless you use the Docker image or Compose services:
    python -m pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_PROXY_URL = "socks5h://127.0.0.1:1080"
DEFAULT_TEST_URL = "https://api.ipify.org?format=json"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"


def load_env_file(env_file: str) -> None:
    """Load environment variables from a .env file when python-dotenv is installed."""
    env_path = Path(env_file)
    if not env_path.exists():
        return

    if importlib.util.find_spec("dotenv") is None:
        print(
            "Warning: .env file found but python-dotenv is not installed. "
            "Install dependencies with: python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return

    from dotenv import load_dotenv

    load_dotenv(env_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test warproxy or send Gemini prompts through warproxy."
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the env file containing GEMINI_API_KEY. Defaults to '.env'.",
    )

    subparsers = parser.add_subparsers(dest="command")

    proxy_parser = subparsers.add_parser(
        "test-proxy",
        help="Verify that HTTP/HTTPS requests are routed through warproxy.",
    )
    add_proxy_arguments(proxy_parser)
    proxy_parser.add_argument(
        "--url",
        default=DEFAULT_TEST_URL,
        help=f"URL used to verify the outgoing IP address. Defaults to {DEFAULT_TEST_URL!r}.",
    )
    proxy_parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Number of proxied request attempts. Useful while warproxy starts. Defaults to 1.",
    )
    proxy_parser.add_argument(
        "--retry-delay",
        type=float,
        default=3.0,
        help="Seconds to wait between proxied request attempts. Defaults to 3.",
    )

    gemini_parser = subparsers.add_parser(
        "gemini",
        help="Send a prompt to Gemini and print the output through warproxy.",
    )
    add_proxy_arguments(gemini_parser)
    gemini_parser.add_argument(
        "--api-key-env",
        default="GEMINI_API_KEY",
        help="Environment variable containing the Gemini API key. Defaults to GEMINI_API_KEY.",
    )
    gemini_parser.add_argument(
        "--model",
        default=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        help=(
            "Gemini model to call. Defaults to GEMINI_MODEL or "
            f"{DEFAULT_GEMINI_MODEL!r}."
        ),
    )
    gemini_parser.add_argument(
        "--prompt",
        help="Prompt text to send to Gemini. If omitted, stdin is used.",
    )
    gemini_parser.add_argument(
        "--prompt-file",
        help="Path to a UTF-8 file containing the prompt. Cannot be used with --prompt.",
    )
    gemini_parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Optional Gemini sampling temperature.",
    )
    gemini_parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="Optional maximum number of output tokens Gemini may return.",
    )

    rotate_parser = subparsers.add_parser(
        "rotate-warproxy",
        help="Restart or recreate warproxy and check whether the proxy IP changes.",
    )
    add_proxy_arguments(rotate_parser)
    rotate_parser.add_argument(
        "--url",
        default=DEFAULT_TEST_URL,
        help=f"URL used to verify the outgoing IP address. Defaults to {DEFAULT_TEST_URL!r}.",
    )
    rotate_parser.add_argument(
        "--retries",
        type=int,
        default=12,
        help="Number of post-rotation proxy check attempts. Defaults to 12.",
    )
    rotate_parser.add_argument(
        "--retry-delay",
        type=float,
        default=5.0,
        help="Seconds to wait between post-rotation proxy checks. Defaults to 5.",
    )
    rotate_parser.add_argument(
        "--service",
        default="warproxy",
        help="Docker Compose service to restart/recreate. Defaults to 'warproxy'.",
    )
    rotate_parser.add_argument(
        "--recreate",
        action="store_true",
        help="Use `docker compose up -d --force-recreate` instead of `docker compose restart`.",
    )
    rotate_parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Optional docker-compose.yml path. Can be provided more than once.",
    )
    rotate_parser.add_argument(
        "--project-directory",
        help="Optional Docker Compose project directory to pass through.",
    )

    rotate_requests_parser = subparsers.add_parser(
        "rotate-requests",
        help="Rotate warproxy before each request and verify the proxied IPs are unique.",
    )
    add_proxy_arguments(rotate_requests_parser)
    rotate_requests_parser.add_argument(
        "--url",
        default=DEFAULT_TEST_URL,
        help=(
            "URL to request through the proxy. It must return JSON with an 'ip' "
            f"field for verification. Defaults to {DEFAULT_TEST_URL!r}."
        ),
    )
    rotate_requests_parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of rotate-and-request attempts to run. Defaults to 10.",
    )
    rotate_requests_parser.add_argument(
        "--retries",
        type=int,
        default=12,
        help="Number of post-rotation proxy check attempts per request. Defaults to 12.",
    )
    rotate_requests_parser.add_argument(
        "--retry-delay",
        type=float,
        default=5.0,
        help="Seconds to wait between post-rotation proxy checks. Defaults to 5.",
    )
    rotate_requests_parser.add_argument(
        "--service",
        default="warproxy",
        help="Docker Compose service to restart/recreate. Defaults to 'warproxy'.",
    )
    rotate_requests_parser.add_argument(
        "--recreate",
        action="store_true",
        help="Use `docker compose up -d --force-recreate` instead of `docker compose restart`.",
    )
    rotate_requests_parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Optional docker-compose.yml path. Can be provided more than once.",
    )
    rotate_requests_parser.add_argument(
        "--project-directory",
        help="Optional Docker Compose project directory to pass through.",
    )

    # Backward compatible default: running `python main.py` still tests the proxy.
    parser.set_defaults(command="test-proxy")
    return parser


def add_proxy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--proxy",
        default=os.getenv("WARPROXY_URL", DEFAULT_PROXY_URL),
        help=(
            "Proxy URL to use. Defaults to WARPROXY_URL or "
            f"{DEFAULT_PROXY_URL!r}. Use socks5h:// so DNS also goes through the proxy."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds. Defaults to 30.",
    )


def build_proxies(proxy_url: str) -> dict[str, str]:
    """Build a requests-compatible proxy map for HTTP and HTTPS."""
    return {
        "http": proxy_url,
        "https": proxy_url,
    }


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
    response = requests_module.get(
        url,
        proxies=build_proxies(proxy_url),
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def read_prompt(prompt: str | None, prompt_file: str | None) -> str:
    """Read prompt text from --prompt, --prompt-file, or stdin."""
    if prompt and prompt_file:
        raise ValueError("Use either --prompt or --prompt-file, not both.")

    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8")

    if prompt is not None:
        return prompt

    if sys.stdin.isatty():
        raise ValueError("Provide --prompt, --prompt-file, or pipe prompt text on stdin.")

    return sys.stdin.read()


def build_genai_client(api_key: str, proxy_url: str, genai_module: Any) -> Any:
    """Create a Google Gen AI SDK client configured for warproxy."""
    http_options = {
        "client_args": {"proxy": proxy_url},
        "async_client_args": {"proxy": proxy_url},
    }
    return genai_module.Client(api_key=api_key, http_options=http_options)


def build_generation_config(
    temperature: float | None,
    max_output_tokens: int | None,
) -> dict[str, Any] | None:
    """Create optional Google Gen AI SDK generation config values."""
    generation_config: dict[str, Any] = {}
    if temperature is not None:
        generation_config["temperature"] = temperature
    if max_output_tokens is not None:
        generation_config["max_output_tokens"] = max_output_tokens
    return generation_config or None


def extract_gemini_text(response: Any) -> str:
    """Extract text from a Google Gen AI SDK generate_content response."""
    text = getattr(response, "text", None)
    if text:
        return text
    return f"Gemini returned no text. Full response: {response}"


def call_gemini(
    prompt: str,
    api_key: str,
    model: str,
    proxy_url: str,
    temperature: float | None,
    max_output_tokens: int | None,
    genai_module: Any,
) -> str:
    """Send a prompt to Gemini through the configured proxy and return generated text."""
    client = build_genai_client(api_key, proxy_url, genai_module)
    config = build_generation_config(temperature, max_output_tokens)
    kwargs: dict[str, Any] = {
        "model": model,
        "contents": prompt,
    }
    if config is not None:
        kwargs["config"] = config

    response = client.models.generate_content(**kwargs)
    return extract_gemini_text(response)


def extract_ip(result: dict[str, Any] | None) -> str | None:
    """Return the `ip` value from an IP check response, when present."""
    if isinstance(result, dict):
        ip_address = result.get("ip")
        if isinstance(ip_address, str):
            return ip_address
    return None


def fetch_proxy_ip_with_retries(
    url: str,
    proxy_url: str,
    timeout: float,
    retries: int,
    retry_delay: float,
    requests_module: Any,
) -> dict[str, Any] | None:
    """Fetch the proxied IP, retrying while warproxy starts or reconnects."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fetch_proxy_ip(url, proxy_url, timeout, requests_module)
        except requests_module.exceptions.InvalidSchema as exc:
            print(
                "Proxy request failed because SOCKS support is missing.\n"
                "Install it with: python -m pip install 'requests[socks]'\n"
                f"Original error: {exc}",
                file=sys.stderr,
            )
            return None
        except requests_module.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                break
            print(
                f"Proxy attempt {attempt}/{retries} failed: {exc}. "
                f"Retrying in {retry_delay:g}s...",
                file=sys.stderr,
            )
            time.sleep(retry_delay)

    print(f"Proxy request failed: {last_error}", file=sys.stderr)
    return None


def build_compose_command(args: argparse.Namespace) -> list[str]:
    """Build the Docker Compose command used to rotate warproxy."""
    command = ["docker", "compose"]
    for compose_file in args.compose_file:
        command.extend(["--file", compose_file])
    if args.project_directory:
        command.extend(["--project-directory", args.project_directory])

    if args.recreate:
        command.extend(["up", "-d", "--force-recreate", args.service])
    else:
        command.extend(["restart", args.service])

    return command


def run_compose_command(command: list[str]) -> int:
    """Run Docker Compose and return its exit code, with a clear Docker-missing error."""
    print(f"Running: {' '.join(command)}")
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError:
        print(
            "Failed to run Docker Compose because the `docker` command was not found. "
            "Run this command on the Docker host, or install Docker CLI.",
            file=sys.stderr,
        )
        return 2

    if completed.returncode != 0:
        print(
            f"Docker Compose command failed with exit code {completed.returncode}.",
            file=sys.stderr,
        )
    return completed.returncode


def run_rotate_warproxy(args: argparse.Namespace, requests_module: Any) -> int:
    """Restart/recreate warproxy and report whether the proxied IP changed."""
    print(f"Checking proxy IP before rotating {args.service!r}...")
    before_result = fetch_proxy_ip_with_retries(
        args.url,
        args.proxy,
        args.timeout,
        retries=1,
        retry_delay=args.retry_delay,
        requests_module=requests_module,
    )
    before_ip = extract_ip(before_result)
    print(f"Proxy IP before rotation: {before_ip if before_ip else 'unavailable'}")

    command = build_compose_command(args)
    compose_exit_code = run_compose_command(command)
    if compose_exit_code != 0:
        return compose_exit_code

    print(f"Waiting for proxy {args.proxy!r} after rotation...")
    after_result = fetch_proxy_ip_with_retries(
        args.url,
        args.proxy,
        args.timeout,
        args.retries,
        args.retry_delay,
        requests_module,
    )
    after_ip = extract_ip(after_result)
    print(f"Proxy IP after rotation:  {after_ip if after_ip else 'unavailable'}")

    if not after_ip:
        return 1
    if before_ip and before_ip != after_ip:
        print("Success: proxy IP changed after rotating warproxy.")
    elif before_ip == after_ip:
        print(
            "warproxy restarted successfully, but the proxy IP did not change. "
            "Cloudflare WARP chooses the egress IP, so a restart/recreate cannot "
            "guarantee a new IP every time."
        )
    else:
        print("warproxy rotation completed and the proxy is responding.")

    return 0


def run_rotate_requests(args: argparse.Namespace, requests_module: Any) -> int:
    """Rotate warproxy before each request and verify every observed IP is unique."""
    if args.count < 1:
        print("--count must be at least 1.", file=sys.stderr)
        return 2

    print(
        f"Sending {args.count} proxied request(s) to {args.url!r}, "
        f"rotating {args.service!r} before each request."
    )
    print(
        "Note: Cloudflare WARP chooses the egress IP; rotating warproxy cannot "
        "guarantee a new IP every time."
    )

    observed_ips: list[str] = []
    failures = 0
    for request_number in range(1, args.count + 1):
        print(f"\nRequest {request_number}/{args.count}: rotating {args.service!r}...")
        compose_exit_code = run_compose_command(build_compose_command(args))
        if compose_exit_code != 0:
            return compose_exit_code

        result = fetch_proxy_ip_with_retries(
            args.url,
            args.proxy,
            args.timeout,
            args.retries,
            args.retry_delay,
            requests_module,
        )
        ip_address = extract_ip(result)
        if not ip_address:
            failures += 1
            print(f"Request {request_number}: FAILED no 'ip' field in response: {result}")
            continue

        is_duplicate = ip_address in observed_ips
        previous_ip = observed_ips[-1] if observed_ips else None
        changed = previous_ip is None or ip_address != previous_ip
        observed_ips.append(ip_address)
        print(
            f"Request {request_number}: ip={ip_address} "
            f"changed_from_previous={'n/a' if previous_ip is None else changed} "
            f"unique_so_far={not is_duplicate}"
        )
        if is_duplicate:
            failures += 1

    unique_ips = list(dict.fromkeys(observed_ips))
    print("\nObserved proxied IPs:")
    for index, ip_address in enumerate(observed_ips, start=1):
        duplicate_marker = (
            " (duplicate)" if observed_ips.index(ip_address) != index - 1 else ""
        )
        print(f"  {index:02d}. {ip_address}{duplicate_marker}")

    if failures:
        print(
            f"IP rotation verification failed: saw {len(unique_ips)} unique IP(s) "
            f"across {args.count} request(s)."
        )
        return 1

    print(
        f"Success: all {args.count} proxied request(s) used unique IPs "
        "after warproxy rotation."
    )
    return 0


def run_proxy_test(args: argparse.Namespace, requests_module: Any) -> int:
    print(f"Testing proxy: {args.proxy}")
    print(f"Test URL: {args.url}")

    direct_result = fetch_direct_ip(args.url, args.timeout, requests_module)

    proxy_result = None
    last_error: requests_module.RequestException | None = None
    for attempt in range(1, args.retries + 1):
        try:
            proxy_result = fetch_proxy_ip(args.url, args.proxy, args.timeout, requests_module)
            break
        except requests_module.exceptions.InvalidSchema as exc:
            print(
                "Proxy test failed because SOCKS support is missing.\n"
                "Install it with: python -m pip install 'requests[socks]'\n"
                f"Original error: {exc}",
                file=sys.stderr,
            )
            return 2
        except requests_module.RequestException as exc:
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

    direct_ip = extract_ip(direct_result)
    proxy_ip = extract_ip(proxy_result)

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


def run_gemini(args: argparse.Namespace) -> int:
    try:
        prompt = read_prompt(args.prompt, args.prompt_file).strip()
    except OSError as exc:
        print(f"Failed to read prompt file: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not prompt:
        print("Prompt is empty.", file=sys.stderr)
        return 2

    api_key = os.getenv(args.api_key_env)
    if not api_key:
        print(
            f"Missing Gemini API key. Add {args.api_key_env}=your_api_key to {args.env_file} "
            f"or export {args.api_key_env} in your shell.",
            file=sys.stderr,
        )
        return 2

    if (
        importlib.util.find_spec("google") is None
        or importlib.util.find_spec("google.genai") is None
    ):
        print(
            "The google-genai package is required. Install it with: "
            "python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    print(
        f"Sending prompt to Gemini model {args.model!r} through proxy {args.proxy!r}...",
        file=sys.stderr,
    )

    from google import genai

    try:
        output = call_gemini(
            prompt=prompt,
            api_key=api_key,
            model=args.model,
            proxy_url=args.proxy,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
            genai_module=genai,
        )
    except Exception as exc:
        print(f"Gemini request failed: {exc}", file=sys.stderr)
        return 1

    print(output)
    return 0


GEMINI_OPTION_NAMES = {
    "--api-key-env",
    "--max-output-tokens",
    "--model",
    "--prompt",
    "--prompt-file",
    "--temperature",
}


def option_name(arg: str) -> str:
    """Return an argparse option name without any inline value."""
    return arg.split("=", 1)[0]


def normalize_args(raw_args: list[str]) -> list[str]:
    """Add the most likely subcommand when callers omit it.

    This keeps old `python main.py --proxy ...` usage working as `test-proxy`,
    and lets Docker Compose users run `docker compose ... gemini --prompt hi`
    even though Compose replaces the service command when extra args are given.
    """
    known_commands = {"test-proxy", "gemini", "rotate-warproxy", "rotate-requests"}
    if any(arg in known_commands for arg in raw_args):
        return raw_args
    if any(arg in {"-h", "--help"} for arg in raw_args):
        return raw_args

    global_args: list[str] = []
    remaining = list(raw_args)
    if remaining and remaining[0] == "--env-file":
        global_args = remaining[:2]
        remaining = remaining[2:]
    elif remaining and remaining[0].startswith("--env-file="):
        global_args = remaining[:1]
        remaining = remaining[1:]

    command = "gemini" if is_gemini_command(remaining) else "test-proxy"
    return [*global_args, command, *remaining]


def is_gemini_command(args: list[str]) -> bool:
    """Infer the Gemini subcommand when Gemini-only options are present."""
    return any(option_name(arg) in GEMINI_OPTION_NAMES for arg in args)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(normalize_args(sys.argv[1:]))
    load_env_file(args.env_file)

    if args.command == "gemini":
        return run_gemini(args)

    if importlib.util.find_spec("requests") is None:
        print(
            "The requests package is required. Install it with: "
            "python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    import requests

    if args.command == "rotate-warproxy":
        return run_rotate_warproxy(args, requests)
    if args.command == "rotate-requests":
        return run_rotate_requests(args, requests)
    return run_proxy_test(args, requests)


if __name__ == "__main__":
    raise SystemExit(main())
