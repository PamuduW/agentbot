from __future__ import annotations

import base64
import json
import os
import re
import ssl
from typing import Any

import httpx

MAX_RESPONSE_BYTES = 4_194_304
REQUEST_TIMEOUT_SECONDS = 30
_CLOUD_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_ISSUE_ID = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*|[1-9][0-9]*)")
_CONTENT_ID = re.compile(r"[1-9][0-9]*")
_SECRET_FIELDS = frozenset(
    {
        "authorization",
        "api_token",
        "apitoken",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "client_secret",
        "credential",
        "api_key",
    }
)


class AtlassianReadError(ValueError):
    pass


def _text(value: object, label: str, maximum: int = 8192) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise AtlassianReadError(f"invalid {label}")
    return value


def _integer(value: object, label: str, minimum: int, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AtlassianReadError(f"invalid {label}")
    return value


def _identifier(value: object, label: str, pattern: re.Pattern[str]) -> str:
    text = _text(value, label, 255)
    if not pattern.fullmatch(text):
        raise AtlassianReadError(f"invalid {label}")
    return text


class AtlassianReadClient:
    def __init__(
        self,
        cloud_id: str,
        email: str,
        jira_token: str,
        confluence_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cloud_id = _identifier(cloud_id, "ATLASSIAN_MCP_CLOUD_ID", _CLOUD_ID)
        email = _text(email, "ATLASSIAN_MCP_EMAIL", 320)
        if ":" in email or "@" not in email or any(char.isspace() for char in email):
            raise AtlassianReadError("invalid ATLASSIAN_MCP_EMAIL")
        jira_token = _text(jira_token, "JIRA_MCP_READ_TOKEN")
        confluence_token = _text(confluence_token, "CONFLUENCE_MCP_READ_TOKEN")
        self._headers = {
            product: "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
            for product, token in (("jira", jira_token), ("confluence", confluence_token))
        }
        self._secrets = (
            jira_token,
            confluence_token,
            *(header.removeprefix("Basic ") for header in self._headers.values()),
        )
        self._transport = transport

    def jira_search(
        self, jql: str, limit: int = 20, next_page_token: str | None = None
    ) -> dict[str, Any]:
        query: dict[str, object] = {
            "jql": _text(jql, "jql"),
            "maxResults": _integer(limit, "limit", 1, 100),
            "fields": "summary,status,description,project,assignee,updated",
        }
        if next_page_token is not None:
            query["nextPageToken"] = _text(next_page_token, "next_page_token")
        return self._get("jira", "/rest/api/3/search/jql", query)

    def jira_issue(self, issue_id: str) -> dict[str, Any]:
        issue = _identifier(issue_id, "issue_id", _ISSUE_ID)
        return self._get("jira", f"/rest/api/3/issue/{issue}", {"updateHistory": "false"})

    def jira_comments(self, issue_id: str, limit: int = 20, start_at: int = 0) -> dict[str, Any]:
        issue = _identifier(issue_id, "issue_id", _ISSUE_ID)
        return self._get(
            "jira", f"/rest/api/3/issue/{issue}/comment", self._offset(limit, start_at)
        )

    def jira_projects(self, limit: int = 20, start_at: int = 0) -> dict[str, Any]:
        return self._get("jira", "/rest/api/3/project/search", self._offset(limit, start_at))

    def jira_boards(self, limit: int = 20, start_at: int = 0) -> dict[str, Any]:
        return self._get("jira", "/rest/agile/1.0/board", self._offset(limit, start_at))

    def jira_sprints(self, board_id: int, limit: int = 20, start_at: int = 0) -> dict[str, Any]:
        board = _integer(board_id, "board_id", 1)
        return self._get(
            "jira", f"/rest/agile/1.0/board/{board}/sprint", self._offset(limit, start_at)
        )

    def confluence_search(
        self, cql: str, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        query = self._cursor(limit, cursor)
        query["cql"] = _text(cql, "cql")
        return self._get("confluence", "/wiki/rest/api/search", query)

    def confluence_page(self, page_id: str) -> dict[str, Any]:
        page = _identifier(page_id, "page_id", _CONTENT_ID)
        return self._get("confluence", f"/wiki/api/v2/pages/{page}", {"body-format": "storage"})

    def confluence_spaces(self, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        return self._get("confluence", "/wiki/api/v2/spaces", self._cursor(limit, cursor))

    def confluence_comments(
        self, page_id: str, kind: str = "footer", limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        page = _identifier(page_id, "page_id", _CONTENT_ID)
        if kind not in {"footer", "inline"}:
            raise AtlassianReadError("invalid kind")
        query = self._cursor(limit, cursor)
        query["body-format"] = "storage"
        return self._get("confluence", f"/wiki/api/v2/pages/{page}/{kind}-comments", query)

    @staticmethod
    def _offset(limit: int, start_at: int) -> dict[str, object]:
        return {
            "maxResults": _integer(limit, "limit", 1, 100),
            "startAt": _integer(start_at, "start_at", 0),
        }

    @staticmethod
    def _cursor(limit: int, cursor: str | None) -> dict[str, object]:
        query: dict[str, object] = {"limit": _integer(limit, "limit", 1, 100)}
        if cursor is not None:
            query["cursor"] = _text(cursor, "cursor")
        return query

    def _get(self, product: str, path: str, query: dict[str, object]) -> dict[str, Any]:
        url = f"https://api.atlassian.com/ex/{product}/{self.cloud_id}{path}"
        chunks: list[bytes] = []
        size = 0
        try:
            with httpx.Client(
                verify=ssl.create_default_context(
                    cafile=os.environ.get("SSL_CERT_FILE") or None,
                    capath=os.environ.get("SSL_CERT_DIR") or None,
                ),
                transport=self._transport,
                follow_redirects=False,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as client:
                with client.stream(
                    "GET",
                    url,
                    params={key: str(value) for key, value in query.items()},
                    headers={
                        "Accept": "application/json",
                        "Authorization": self._headers[product],
                    },
                ) as response:
                    if response.status_code != 200:
                        raise AtlassianReadError(f"Atlassian returned HTTP {response.status_code}")
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise AtlassianReadError("Atlassian response limit exceeded")
                        chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
            if not isinstance(payload, dict):
                raise AtlassianReadError("Atlassian returned invalid JSON")
        except httpx.TimeoutException:
            raise AtlassianReadError("Atlassian request timed out") from None
        except httpx.HTTPError:
            raise AtlassianReadError("Atlassian request failed") from None
        except OSError:
            raise AtlassianReadError("Atlassian TLS configuration failed") from None
        except (ValueError, UnicodeError, RecursionError) as error:
            if isinstance(error, AtlassianReadError):
                raise
            raise AtlassianReadError("Atlassian returned invalid JSON") from None
        return {
            "source": {"origin": "https://api.atlassian.com", "url": url},
            "untrusted": True,
            "data": self._redact(payload),
        }

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                self._redact(key): (
                    "[redacted by agentbot: credential field]"
                    if key.lower() in _SECRET_FIELDS
                    else self._redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[redacted by agentbot: credential]")
        return value
