from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_PAGES = 5
MAX_RESPONSE_BYTES = 4_194_304
DEFAULT_FILE_BYTES = 512 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_DIFF_BYTES = 1024 * 1024
MAX_LOG_LINES = 2_000
REQUEST_TIMEOUT_SECONDS = 30.0
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_SEARCH_SCOPES = frozenset({"blobs", "commits", "issues", "merge_requests", "notes"})


class GitLabReadError(Exception):
    pass


@dataclass(frozen=True)
class GitLabReadRoute:
    name: str
    method: str
    path: str


GITLAB_READ_ROUTES = (
    GitLabReadRoute("gitlab_project", "GET", "/projects/{project_id}"),
    GitLabReadRoute("gitlab_repository_tree", "GET", "/projects/{project_id}/repository/tree"),
    GitLabReadRoute(
        "gitlab_repository_file", "GET", "/projects/{project_id}/repository/files/{file_path}/raw"
    ),
    GitLabReadRoute("gitlab_commits", "GET", "/projects/{project_id}/repository/commits"),
    GitLabReadRoute("gitlab_refs", "GET", "/projects/{project_id}/repository/{kind}"),
    GitLabReadRoute("gitlab_issues", "GET", "/projects/{project_id}/issues"),
    GitLabReadRoute("gitlab_merge_requests", "GET", "/projects/{project_id}/merge_requests"),
    GitLabReadRoute(
        "gitlab_merge_request_diff", "GET", "/projects/{project_id}/merge_requests/{iid}/diffs"
    ),
    GitLabReadRoute("gitlab_notes", "GET", "/projects/{project_id}/{kind}/{iid}/notes"),
    GitLabReadRoute("gitlab_members", "GET", "/projects/{project_id}/members"),
    GitLabReadRoute("gitlab_releases", "GET", "/projects/{project_id}/releases"),
    GitLabReadRoute("gitlab_pipelines", "GET", "/projects/{project_id}/pipelines"),
    GitLabReadRoute("gitlab_jobs", "GET", "/projects/{project_id}/jobs"),
    GitLabReadRoute("gitlab_job_log", "GET", "/projects/{project_id}/jobs/{job_id}/trace"),
    GitLabReadRoute("gitlab_search", "GET", "/projects/{project_id}/search"),
)


@dataclass(frozen=True)
class GitLabHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes


class GitLabTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
    ) -> GitLabHttpResponse: ...


class HttpxGitLabTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
    ) -> GitLabHttpResponse:
        with httpx.Client(follow_redirects=False, timeout=timeout) as client:
            with client.stream(method, url, headers=headers) as response:
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65_536):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        break
                return GitLabHttpResponse(
                    response.status_code,
                    response.headers,
                    bytes(content),
                )


class GitLabReadClient:
    def __init__(
        self,
        origin: str,
        token: str,
        *,
        transport: GitLabTransport | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        parsed = urlparse(origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise GitLabReadError("GitLab origin must be a fixed HTTPS origin")
        if (
            not isinstance(token, str)
            or not token.strip()
            or any(character in token for character in "\r\n")
        ):
            raise GitLabReadError("GitLab read token is missing or invalid")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.api_root = f"{self.origin}/api/v4"
        self.token = token
        self.transport = transport or HttpxGitLabTransport()
        self.sleeper = sleeper or time.sleep

    def project(self, project_id: str | int) -> dict[str, Any]:
        return self._json(f"/projects/{self._segment(project_id, 'project_id')}")

    def repository_tree(
        self,
        project_id: str | int,
        *,
        path: str | None = None,
        ref: str | None = None,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        query = self._page_query(per_page, max_pages)
        if path is not None:
            query["path"] = self._repository_path(path)
        if ref is not None:
            query["ref"] = self._text(ref, "ref")
        return self._json(
            f"/projects/{self._segment(project_id, 'project_id')}/repository/tree",
            query,
            paginate=True,
            max_pages=max_pages,
        )

    def repository_file(
        self,
        project_id: str | int,
        file_path: str,
        *,
        ref: str,
        max_bytes: int = DEFAULT_FILE_BYTES,
    ) -> dict[str, Any]:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 0 < max_bytes <= MAX_FILE_BYTES
        ):
            raise GitLabReadError("max_bytes must be between 1 and 2097152")
        encoded_path = quote(self._repository_path(file_path), safe="")
        path = f"/projects/{self._segment(project_id, 'project_id')}/repository/files/{encoded_path}/raw"
        body, source = self._request(
            path,
            {"ref": self._text(ref, "ref")},
            max_bytes,
            "file",
        )
        return self._envelope(source, {"text": body.decode("utf-8", errors="replace")})

    def commits(
        self,
        project_id: str | int,
        *,
        sha: str | None = None,
        include_diff: bool = False,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        base = f"/projects/{self._segment(project_id, 'project_id')}/repository/commits"
        if include_diff and sha is None:
            raise GitLabReadError("commit diff requires sha")
        if sha is not None:
            base += f"/{self._segment(sha, 'sha')}"
            if include_diff:
                base += "/diff"
                return self._json(base, limit=DEFAULT_DIFF_BYTES, limit_label="diff")
            return self._json(base)
        return self._paged(base, per_page, max_pages)

    def refs(
        self,
        project_id: str | int,
        *,
        kind: str,
        ref: str | None = None,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        if kind not in {"branches", "tags"}:
            raise GitLabReadError("ref kind must be branches or tags")
        path = f"/projects/{self._segment(project_id, 'project_id')}/repository/{kind}"
        if ref is not None:
            return self._json(f"{path}/{self._segment(ref, 'ref')}")
        return self._paged(path, per_page, max_pages)

    def issues(
        self,
        project_id: str | int,
        *,
        iid: int | None = None,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        path = f"/projects/{self._segment(project_id, 'project_id')}/issues"
        return (
            self._json(f"{path}/{self._positive_id(iid, 'iid')}")
            if iid is not None
            else self._paged(path, per_page, max_pages)
        )

    def merge_requests(
        self,
        project_id: str | int,
        *,
        iid: int | None = None,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        path = f"/projects/{self._segment(project_id, 'project_id')}/merge_requests"
        return (
            self._json(f"{path}/{self._positive_id(iid, 'iid')}")
            if iid is not None
            else self._paged(path, per_page, max_pages)
        )

    def merge_request_diff(
        self, project_id: str | int, iid: int, *, max_bytes: int = DEFAULT_DIFF_BYTES
    ) -> dict[str, Any]:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 0 < max_bytes <= MAX_RESPONSE_BYTES
        ):
            raise GitLabReadError("diff max_bytes is invalid")
        path = f"/projects/{self._segment(project_id, 'project_id')}/merge_requests/{self._positive_id(iid, 'iid')}/diffs"
        return self._json(path, limit=max_bytes, limit_label="diff")

    def notes(
        self,
        project_id: str | int,
        *,
        kind: str,
        iid: int,
        note_id: int | None = None,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        if kind not in {"issues", "merge_requests"}:
            raise GitLabReadError("note kind must be issues or merge_requests")
        path = f"/projects/{self._segment(project_id, 'project_id')}/{kind}/{self._positive_id(iid, 'iid')}/notes"
        if note_id is not None:
            return self._json(f"{path}/{self._positive_id(note_id, 'note_id')}")
        return self._paged(path, per_page, max_pages)

    def members(
        self,
        project_id: str | int,
        *,
        include_inherited: bool = False,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        suffix = "/all" if include_inherited else ""
        path = f"/projects/{self._segment(project_id, 'project_id')}/members{suffix}"
        return self._paged(path, per_page, max_pages)

    def releases(
        self,
        project_id: str | int,
        *,
        tag_name: str | None = None,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        path = f"/projects/{self._segment(project_id, 'project_id')}/releases"
        if tag_name is not None:
            return self._json(f"{path}/{self._segment(tag_name, 'tag_name')}")
        return self._paged(path, per_page, max_pages)

    def pipelines(
        self,
        project_id: str | int,
        *,
        pipeline_id: int | None = None,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        path = f"/projects/{self._segment(project_id, 'project_id')}/pipelines"
        if pipeline_id is not None:
            return self._json(f"{path}/{self._positive_id(pipeline_id, 'pipeline_id')}")
        return self._paged(path, per_page, max_pages)

    def jobs(
        self,
        project_id: str | int,
        *,
        pipeline_id: int | None = None,
        job_id: int | None = None,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        project = self._segment(project_id, "project_id")
        if job_id is not None:
            return self._json(f"/projects/{project}/jobs/{self._positive_id(job_id, 'job_id')}")
        path = (
            f"/projects/{project}/pipelines/{self._positive_id(pipeline_id, 'pipeline_id')}/jobs"
            if pipeline_id is not None
            else f"/projects/{project}/jobs"
        )
        return self._paged(path, per_page, max_pages)

    def job_log(self, project_id: str | int, job_id: int) -> dict[str, Any]:
        path = f"/projects/{self._segment(project_id, 'project_id')}/jobs/{self._positive_id(job_id, 'job_id')}/trace"
        body, source = self._request(path, {}, MAX_RESPONSE_BYTES)
        lines = body.decode("utf-8", errors="replace").splitlines()
        truncated = len(lines) > MAX_LOG_LINES
        return self._envelope(
            source,
            {"text": "\n".join(lines[-MAX_LOG_LINES:]), "truncated": truncated},
        )

    def search(
        self,
        project_id: str | int,
        *,
        scope: str,
        search: str,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        if scope not in _SEARCH_SCOPES:
            raise GitLabReadError("search scope is not allowed")
        query = self._page_query(per_page, max_pages)
        query.update({"scope": scope, "search": self._text(search, "search", maximum=256)})
        return self._json(
            f"/projects/{self._segment(project_id, 'project_id')}/search",
            query,
            paginate=True,
            max_pages=max_pages,
        )

    def _paged(self, path: str, per_page: int, max_pages: int) -> dict[str, Any]:
        return self._json(
            path,
            self._page_query(per_page, max_pages),
            paginate=True,
            max_pages=max_pages,
        )

    def _json(
        self,
        path: str,
        query: dict[str, object] | None = None,
        *,
        paginate: bool = False,
        max_pages: int = 1,
        limit: int = MAX_RESPONSE_BYTES,
        limit_label: str = "response",
    ) -> dict[str, Any]:
        pages: list[Any] = []
        total = 0
        current_query = dict(query or {})
        source = ""
        for page_index in range(max_pages):
            body, source = self._request(path, current_query, limit - total, limit_label)
            total += len(body)
            try:
                value = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise GitLabReadError("GitLab returned malformed JSON") from error
            if not paginate:
                return self._envelope(source, value)
            pages.extend(value if isinstance(value, list) else [value])
            next_page = self._last_header("X-Next-Page")
            if not next_page:
                return self._envelope(source, pages, pages=page_index + 1)
            current_query["page"] = self._positive_id_string(next_page, "next page")
        return self._envelope(source, pages, pages=max_pages, truncated=True)

    def _request(
        self,
        path: str,
        query: dict[str, object],
        limit: int,
        limit_label: str = "response",
    ) -> tuple[bytes, str]:
        if limit <= 0:
            raise GitLabReadError(f"GitLab {limit_label} limit exceeded")
        suffix = f"?{urlencode(query)}" if query else ""
        url = f"{self.api_root}{path}{suffix}"
        headers = {"Accept": "application/json", "PRIVATE-TOKEN": self.token}
        for attempt in range(3):
            try:
                response = self.transport.request(
                    "GET",
                    url,
                    headers,
                    REQUEST_TIMEOUT_SECONDS,
                    limit,
                )
            except TimeoutError as error:
                raise GitLabReadError("GitLab request timed out") from error
            except Exception as error:
                raise GitLabReadError("GitLab request failed") from error
            self._last_headers = response.headers
            if 300 <= response.status_code < 400:
                location = self._header(response.headers, "Location")
                target = urljoin(url, location or "")
                if self._origin(target) != self.origin:
                    raise GitLabReadError("GitLab cross-origin redirect blocked")
                url = target
                continue
            if response.status_code in _RETRY_STATUSES and attempt < 2:
                self.sleeper(self._retry_after(response.headers))
                continue
            if response.status_code >= 400:
                raise GitLabReadError(f"GitLab returned HTTP {response.status_code}")
            if len(response.content) > limit:
                raise GitLabReadError(f"GitLab {limit_label} limit exceeded")
            return response.content, url
        raise GitLabReadError("GitLab redirect or retry limit exceeded")

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        return next((value for key, value in headers.items() if key.lower() == name.lower()), None)

    def _last_header(self, name: str) -> str | None:
        return self._header(getattr(self, "_last_headers", {}), name)

    @classmethod
    def _segment(cls, value: str | int, label: str) -> str:
        return quote(cls._text(str(value), label), safe="")

    @staticmethod
    def _text(value: str, label: str, *, maximum: int = 1024) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or any(character in value for character in "\x00\r\n")
        ):
            raise GitLabReadError(f"{label} is invalid")
        return value

    @classmethod
    def _repository_path(cls, value: str) -> str:
        path = cls._text(value, "path", maximum=4096)
        if (
            path.startswith(("/", "\\"))
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise GitLabReadError("repository path is invalid")
        return path

    @staticmethod
    def _positive_id(value: int | None, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GitLabReadError(f"{label} must be a positive integer")
        return value

    @classmethod
    def _positive_id_string(cls, value: str, label: str) -> int:
        if not value.isdigit():
            raise GitLabReadError(f"{label} must be a positive integer")
        return cls._positive_id(int(value), label)

    @staticmethod
    def _page_query(per_page: int, max_pages: int) -> dict[str, object]:
        if (
            isinstance(per_page, bool)
            or not isinstance(per_page, int)
            or not 1 <= per_page <= MAX_PAGE_SIZE
        ):
            raise GitLabReadError("per_page must be between 1 and 100")
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= MAX_PAGES
        ):
            raise GitLabReadError("max_pages must be between 1 and 5")
        return {"page": 1, "per_page": per_page}

    @classmethod
    def _retry_after(cls, headers: Mapping[str, str]) -> float:
        raw = cls._header(headers, "Retry-After")
        try:
            return min(max(float(raw or "1"), 0.0), 30.0)
        except ValueError:
            return 1.0

    def _envelope(self, source: str, data: Any, **metadata: object) -> dict[str, Any]:
        return {
            "source": {"origin": self.origin, "url": source},
            "untrusted": True,
            "data": data,
            **metadata,
        }
