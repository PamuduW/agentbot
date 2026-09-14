from __future__ import annotations

import unittest
from collections.abc import Callable

from src.gitlab_read_client import (
    GITLAB_READ_ROUTES,
    GitLabHttpResponse,
    GitLabReadClient,
    GitLabReadError,
)


class FakeTransport:
    def __init__(
        self,
        responses: list[GitLabHttpResponse | Exception],
        redirect_target: FakeTransport | None = None,
    ) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, str], float, int]] = []
        self.redirect_target = redirect_target

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
    ) -> GitLabHttpResponse:
        self.requests.append((method, url, headers, timeout, max_bytes))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(
    status: int = 200,
    body: bytes = b"{}",
    **headers: str,
) -> GitLabHttpResponse:
    return GitLabHttpResponse(status, headers, body)


class GitLabReadClientTests(unittest.TestCase):
    def client(
        self,
        *responses: GitLabHttpResponse | Exception,
        sleeper: Callable[[float], None] | None = None,
    ) -> tuple[GitLabReadClient, FakeTransport]:
        transport = FakeTransport(list(responses) or [response()])
        return (
            GitLabReadClient(
                "https://gitlab.example.com",
                "token-value",
                transport=transport,
                sleeper=sleeper,
            ),
            transport,
        )

    def test_every_declared_route_is_get(self) -> None:
        self.assertEqual({"GET"}, {route.method for route in GITLAB_READ_ROUTES})
        self.assertEqual(15, len(GITLAB_READ_ROUTES))

    def test_origin_and_token_fail_before_transport(self) -> None:
        transport = FakeTransport([response()])
        for origin in (
            "http://gitlab.example.com",
            "https://user@gitlab.example.com",
            "https://gitlab.example.com/other",
            "https://gitlab.example.com?query=yes",
        ):
            with self.subTest(origin=origin), self.assertRaisesRegex(GitLabReadError, "origin"):
                GitLabReadClient(origin, "token-value", transport=transport)
        for token in ("", " ", "line\nbreak"):
            with self.subTest(token=token), self.assertRaisesRegex(GitLabReadError, "token"):
                GitLabReadClient("https://gitlab.example.com", token, transport=transport)
        self.assertEqual([], transport.requests)

    def test_project_ids_and_paths_are_encoded_and_traversal_is_rejected(self) -> None:
        client, transport = self.client(response(body=b'{"id":1}'))
        client.project("group/project")
        self.assertIn("/api/v4/projects/group%2Fproject", transport.requests[0][1])
        self.assertNotIn("token-value", transport.requests[0][1])
        with self.assertRaisesRegex(GitLabReadError, "path"):
            client.repository_file("group/project", "../secret", ref="main")
        self.assertEqual(1, len(transport.requests))

    def test_every_public_operation_constructs_only_reviewed_gets(self) -> None:
        payloads = [response(body=b"[]") for _ in range(15)]
        client, transport = self.client(*payloads)
        operations = (
            lambda: client.project("1"),
            lambda: client.repository_tree("1"),
            lambda: client.repository_file("1", "README.md", ref="main"),
            lambda: client.commits("1"),
            lambda: client.refs("1", kind="branches"),
            lambda: client.issues("1"),
            lambda: client.merge_requests("1"),
            lambda: client.merge_request_diff("1", 2),
            lambda: client.notes("1", kind="issues", iid=2),
            lambda: client.members("1"),
            lambda: client.releases("1"),
            lambda: client.pipelines("1"),
            lambda: client.jobs("1", pipeline_id=3),
            lambda: client.job_log("1", 4),
            lambda: client.search("1", scope="blobs", search="needle"),
        )
        for operation in operations:
            operation()
        self.assertEqual(15, len(transport.requests))
        self.assertEqual({"GET"}, {request[0] for request in transport.requests})
        self.assertTrue(
            all(
                request[2] == {"Accept": "application/json", "PRIVATE-TOKEN": "token-value"}
                for request in transport.requests
            )
        )
        self.assertTrue(all(request[3] == 30 for request in transport.requests))

    def test_pagination_is_bounded_and_rejects_oversized_page_requests(self) -> None:
        client, transport = self.client(
            response(body=b"[]", **{"X-Next-Page": "2"}),
            response(body=b"[]", **{"X-Next-Page": "3"}),
        )
        result = client.repository_tree("1", per_page=20, max_pages=2)
        self.assertEqual([], result["data"])
        self.assertEqual(2, result["pages"])
        self.assertTrue(result["truncated"])
        self.assertEqual(2, len(transport.requests))
        self.assertIn("per_page=20", transport.requests[0][1])
        self.assertIn("page=2", transport.requests[1][1])
        with self.assertRaisesRegex(GitLabReadError, "per_page"):
            client.repository_tree("1", per_page=101)
        with self.assertRaisesRegex(GitLabReadError, "max_pages"):
            client.repository_tree("1", max_pages=6)

    def test_retry_after_is_honored_but_auth_and_not_found_never_retry(self) -> None:
        sleeps: list[float] = []
        client, transport = self.client(
            response(429, b'{"message":"slow"}', **{"Retry-After": "2"}),
            response(body=b'{"id":1}'),
            sleeper=sleeps.append,
        )
        client.project("1")
        self.assertEqual([2.0], sleeps)
        self.assertEqual(2, len(transport.requests))
        for status in (401, 403, 404):
            with self.subTest(status=status):
                client, transport = self.client(response(status, b'{"message":"no"}'))
                with self.assertRaisesRegex(GitLabReadError, f"HTTP {status}"):
                    client.project("1")
                self.assertEqual(1, len(transport.requests))

    def test_cross_origin_redirect_never_receives_the_token(self) -> None:
        client, transport = self.client(
            response(302, b"", Location="https://evil.example/api/v4/projects/1")
        )
        with self.assertRaisesRegex(GitLabReadError, "cross-origin redirect"):
            client.project("1")
        self.assertEqual(1, len(transport.requests))

    def test_timeout_and_upstream_errors_are_sanitized(self) -> None:
        for failure, message in (
            (TimeoutError("secret-host-detail"), "timed out"),
            (RuntimeError("secret-host-detail"), "request failed"),
        ):
            with self.subTest(message=message):
                client, _transport = self.client(failure)
                with self.assertRaisesRegex(GitLabReadError, message) as caught:
                    client.project("1")
                self.assertNotIn("secret-host-detail", str(caught.exception))

    def test_response_file_diff_and_log_limits_fail_closed(self) -> None:
        client, _ = self.client(response(body=b"x" * (4_194_304 + 1)))
        with self.assertRaisesRegex(GitLabReadError, "response limit"):
            client.project("1")

        client, _ = self.client(response(body=b"x" * (512 * 1024 + 1)))
        with self.assertRaisesRegex(GitLabReadError, "file limit"):
            client.repository_file("1", "large.txt", ref="main")
        with self.assertRaisesRegex(GitLabReadError, "max_bytes"):
            client.repository_file("1", "large.txt", ref="main", max_bytes=2_097_153)

        client, _ = self.client(response(body=b'[{"diff":"' + b"x" * 100 + b'"}]'))
        with self.assertRaisesRegex(GitLabReadError, "diff limit"):
            client.merge_request_diff("1", 2, max_bytes=32)

        lines = "\n".join(str(index) for index in range(2100)).encode()
        client, _ = self.client(response(body=lines))
        result = client.job_log("1", 2)
        self.assertEqual(2000, len(result["data"]["text"].splitlines()))
        self.assertTrue(result["data"]["truncated"])


if __name__ == "__main__":
    unittest.main()
