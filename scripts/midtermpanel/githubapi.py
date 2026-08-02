"""Read-only GitHub access for preflight and finalize.

Read-only by construction, not by convention: there is no method here that
writes. Publishing a status goes through `midtermpanel.status`, which is a
separate module with a separate allowlist, so "what can this code change?" has a
one-file answer.

Every response is parsed by `preflight.parse_api_json` — size-capped, duplicate
keys refused, errors reported without echoing the body. The body is
server-controlled text arriving in a workflow that will shortly hold a provider
key, and the cheapest time to stop trusting it is before it is parsed.

The repository is addressed by NUMERIC ID rather than by `owner/name`. A name can
be transferred or reused; the numeric id cannot, and the trusted lane pins the
same value for the same reason.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from .errors import refuse
from .preflight import parse_api_json

API_ROOT = "https://api.github.com"


class ReadOnlyGitHub:
    """The four reads preflight and finalize need, and nothing else."""

    def __init__(self, *, token: str, repository_numeric_id: int, opener=None,
                 timeout: int = 30):
        if not token or not isinstance(token, str):
            refuse("category=github_client_without_token")
        from trustedlane.identity import assert_repository_numeric_id
        assert_repository_numeric_id(repository_numeric_id)
        self._token = token
        self._id = repository_numeric_id
        self._opener = opener or urllib.request.urlopen
        self._timeout = int(timeout)
        self.calls = []

    def _get(self, path: str, *, where: str):
        url = f"{API_ROOT}/repositories/{self._id}{path}"
        self.calls.append(where)
        request = urllib.request.Request(url, method="GET")  # noqa: S310
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        try:
            # S310: the URL is built from a fixed https root and a validated
            # integer id; no caller-supplied scheme can reach it.
            with self._opener(  # noqa: S310 - fixed https root + validated int id
                    request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Status only. A GitHub error body can echo request headers, and the
            # one header here is a bearer token.
            refuse(f"category=github_api_error where={where} "
                   f"http_status={exc.code}")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            refuse(f"category=github_api_transport_error where={where} "
                   f"exception_class={type(exc).__name__}")
        return parse_api_json(raw, where=where)

    def open_pull_requests(self) -> list:
        got = self._get("/pulls?state=open&per_page=100", where="pulls")
        if not isinstance(got, list):
            refuse("category=pulls_response_not_a_list")
        return got

    def default_branch_head(self) -> str:
        got = self._get("/branches/main", where="branches/main")
        sha = str(((got or {}).get("commit") or {}).get("sha") or "")
        from .status import assert_candidate_sha
        return assert_candidate_sha(sha, field="main.commit.sha")

    def check_runs(self, head_sha: str) -> list:
        got = self._get(f"/commits/{head_sha}/check-runs?per_page=100",
                        where="check-runs")
        runs = (got or {}).get("check_runs")
        if not isinstance(runs, list):
            refuse("category=check_runs_response_malformed")
        return runs

    def changed_files(self, pr_number: int) -> list:
        got = self._get(f"/pulls/{int(pr_number)}/files?per_page=300",
                        where="pull-files")
        if not isinstance(got, list):
            refuse("category=pull_files_response_not_a_list")
        return [str(entry.get("filename") or "") for entry in got
                if isinstance(entry, dict)]

    def commit_statuses(self, head_sha: str) -> list:
        """Published commit statuses on the exact head.

        Used by finalize to find a `pending` the panel left behind. Note this is
        the statuses surface, not check runs: it is where this panel publishes,
        so it is where an unresolved publication would be."""
        got = self._get(f"/commits/{head_sha}/statuses?per_page=100",
                        where="statuses")
        if not isinstance(got, list):
            refuse("category=statuses_response_not_a_list")
        return got
