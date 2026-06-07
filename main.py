"""Use Gemini through a local warproxy/Cloudflare WARP proxy from Python.

Usage examples:
    # Verify that warproxy changes your egress IP.
    python main.py test-proxy
    python main.py test-proxy --proxy socks5h://myuser:mypassword@127.0.0.1:1080
    WARPROXY_URL=socks5h://127.0.0.1:1080 python main.py test-proxy

    # Send a prompt to Gemini through warproxy. Put GEMINI_API_KEY in .env first.
    python main.py gemini --prompt "hi" --model gemini-3.5-flash
    python main.py gemini --prompt "Explain WARP in one sentence."
    python main.py gemini --model gemini-2.5-flash --prompt-file prompt.txt
    echo "Write a haiku about proxies" | python main.py gemini

    docker compose --profile test up --build --abort-on-container-exit proxy-test
    docker compose --profile gemini run --rm gemini gemini --prompt "Hello from Gemini"

Install dependencies first, unless you use the Docker image or Compose services:
    python -m pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import importlib.util
import os
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


def normalize_args(raw_args: list[str]) -> list[str]:
    """Keep old `python main.py --proxy ...` usage working by adding test-proxy."""
    if any(arg in {"test-proxy", "gemini"} for arg in raw_args):
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

    return [*global_args, "test-proxy", *remaining]


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

    return run_proxy_test(args, requests)


if __name__ == "__main__":
    raise SystemExit(main())
