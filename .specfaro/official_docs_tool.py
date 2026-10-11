"""Read-only official-document tool for an OpenHands repair conversation.

The executor runs in the worker controller, outside the agent's checkout.
Every successful read records the fetched content hash and UTC time.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal
import urllib.parse
import urllib.error
import urllib.request

from pydantic import Field
from openhands.sdk.tool import (Action, Observation, ToolAnnotations,
                                ToolDefinition, ToolExecutor, register_tool)


CATALOG = "https://dotte.duckdns.org/api/knowledge/search"
HOSTS = {"shopify": {"shopify.dev", "docs.shopify.dev"},
         "stripe": {"docs.stripe.com"}}
MAX_SEARCHES = 8
MAX_READS = 5
MAX_BODY = 500_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _get(url: str, allowed_hosts: set[str], *, redirects: int = 0) -> tuple[bytes, str]:
    current = url
    for _ in range(redirects + 1):
        parsed = urllib.parse.urlsplit(current)
        if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
                or parsed.username or parsed.password or parsed.port):
            raise ValueError("Document URL left the approved HTTPS hosts")
        request = urllib.request.Request(current, headers={"User-Agent": "specfaro-openhands-worker/0.1"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
                if response.geturl() != current:
                    raise ValueError("Document request redirected unexpectedly")
                body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise ValueError("Document response exceeds the tool limit")
            return body, current
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 307, 308} or not exc.headers.get("Location"):
                raise
            current = urllib.parse.urljoin(current, exc.headers["Location"])
    raise ValueError("Too many official-document redirects")


def _official(url: str, provider: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in HOSTS[provider]
                and parsed.username is None and parsed.password is None and parsed.port is None
                and not parsed.fragment)
    except (KeyError, ValueError):
        return False


class OfficialDocsAction(Action):
    operation: Literal["search", "read"] = Field(description="Search the catalog or read one search result")
    query: str | None = Field(default=None, description="Two to 120 search characters")
    url: str | None = Field(default=None, description="Exact official URL returned by search")


class OfficialDocsObservation(Observation):
    result: str = Field(description="Bounded search results or official page excerpt with provenance")


class OfficialDocsExecutor(ToolExecutor[OfficialDocsAction, OfficialDocsObservation]):
    def __init__(self, provider: str, version: str, evidence_path: str):
        if provider not in HOSTS or not version:
            raise ValueError("Provider and version are required for official document lookup")
        self.provider = provider
        self.version = version
        self.evidence_path = Path(evidence_path)
        self.allowed_urls: set[str] = set()
        self.searches = 0
        self.reads = 0

    def __call__(self, action: OfficialDocsAction, conversation=None) -> OfficialDocsObservation:
        try:
            if action.operation == "search":
                if not action.query or not 2 <= len(action.query) <= 120 or self.searches >= MAX_SEARCHES:
                    raise ValueError("Invalid query or search budget exhausted")
                self.searches += 1
                search_text = (action.query if self.version in action.query
                               else f"{self.version} {action.query}")
                query = urllib.parse.urlencode({"provider": self.provider, "q": search_text})
                raw, _ = _get(CATALOG + "?" + query, {"dotte.duckdns.org"})
                results = json.loads(raw)["results"][:8]
                results = [item for item in results
                           if isinstance(item, dict) and _official(str(item.get("url", "")), self.provider)]
                self.allowed_urls.update(item["url"] for item in results)
                return OfficialDocsObservation.from_text(
                    text=json.dumps({"provider": self.provider, "target_version": self.version,
                                     "results": results}, ensure_ascii=False)[:12000],
                    result=json.dumps(results, ensure_ascii=False)[:12000],
                )
            if not action.url or action.url not in self.allowed_urls or self.reads >= MAX_READS:
                raise ValueError("Read a returned official search result within the tool budget")
            self.reads += 1
            fetch_url = action.url
            parsed = urllib.parse.urlsplit(fetch_url)
            if (self.provider == "shopify" and parsed.hostname == "shopify.dev"
                    and parsed.path.startswith("/changelog/")
                    and not parsed.path.startswith("/changelog/posts/")):
                slug = parsed.path.removeprefix("/changelog/").strip("/")
                if slug and "/" not in slug:
                    fetch_url = "https://shopify.dev/changelog/posts/" + slug + ".md"
            raw, final_url = _get(fetch_url, HOSTS[self.provider], redirects=2)
            body = raw.decode("utf-8", errors="replace")
            evidence = {"provider": self.provider, "requested_version": self.version,
                        "url": action.url, "final_url": final_url,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "sha256": hashlib.sha256(raw).hexdigest(), "body_excerpt": body[:6000]}
            self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
            with self.evidence_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(evidence, ensure_ascii=False) + "\n")
            return OfficialDocsObservation.from_text(
                text=json.dumps({key: value for key, value in evidence.items() if key != "body_excerpt"})
                     + "\n" + evidence["body_excerpt"],
                result=evidence["body_excerpt"],
            )
        except (ValueError, KeyError, TypeError, OSError, urllib.error.URLError,
                json.JSONDecodeError) as exc:
            return OfficialDocsObservation.from_text(
                text=f"Official documentation unavailable: {str(exc)[:200]}",
                result="", is_error=True,
            )


class OfficialDocsTool(ToolDefinition[OfficialDocsAction, OfficialDocsObservation]):
    @classmethod
    def create(cls, conv_state, *, provider: str, version: str, evidence_path: str):
        return [cls(
            description="Search Specfaro's official documentation catalog for the configured API provider, "
                        "then read an official result. Search before reading. Results include provenance.",
            action_type=OfficialDocsAction,
            observation_type=OfficialDocsObservation,
            annotations=ToolAnnotations(title="Official API documentation", readOnlyHint=True,
                                        destructiveHint=False, idempotentHint=True,
                                        openWorldHint=False),
            executor=OfficialDocsExecutor(provider, version, evidence_path),
        )]


register_tool(OfficialDocsTool.name, OfficialDocsTool)
