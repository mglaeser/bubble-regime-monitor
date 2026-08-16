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

#: Pagination for the open pull request list. Bounded so a pathological or
#: hostile response cannot make this loop for ever, and the bound is a REFUSAL
#: rather than a truncation — see `open_pull_requests`.
PULL_REQUEST_PAGE_SIZE = 100
MAX_PULL_REQUEST_PAGES = 20

#: The same, for the changed-file list. 100 because that is GitHub's cap on
#: `per_page` for every list endpoint — asking for more is silently clamped,
#: which is how this read spent its life truncated at 100 while requesting
#: 300. 40 pages is 4000 changed paths; beyond that the run refuses rather
#: than classify risk from a partial diff. See `changed_files`.
CHANGED_FILE_PAGE_SIZE = 100
MAX_CHANGED_FILE_PAGES = 40


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
        """Every open pull request, across pages — or a refusal.

        Paginated rather than first-page-only because a caller now draws a
        conclusion from ABSENCE. `preflight.classify_candidate` reads "no open
        pull request at this head" as "it merged, or the author pushed again"
        and ends the run quietly, publishing no status.

        While the only reader refused on absence, a truncated first page was
        merely a wrong red — loud, and visibly wrong. Now the same truncation
        would be a review that silently did not happen. So the read either
        completes or says that it did not: a page bound is a bound on how much
        this will fetch, never a licence to answer from a partial list."""
        collected: list = []
        for page in range(1, MAX_PULL_REQUEST_PAGES + 1):
            got = self._get(
                f"/pulls?state=open&per_page={PULL_REQUEST_PAGE_SIZE}"
                f"&page={page}",
                where="pulls")
            if not isinstance(got, list):
                refuse("category=pulls_response_not_a_list")
            collected.extend(got)
            if len(got) < PULL_REQUEST_PAGE_SIZE:
                return collected
        refuse(f"category=open_pull_requests_did_not_end "
               f"pages={MAX_PULL_REQUEST_PAGES} collected={len(collected)} — "
               "the list did not terminate within the page bound, so absence "
               "cannot be concluded from it and the run refuses rather than "
               "quietly reviewing nothing")

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
        """Every changed path, across pages — or a refusal.

        The same rule as `open_pull_requests`, and this is the read where it
        actually costs a control rather than a colour. `preflight.
        classify_changed_files` concludes `high_risk` from what this returns,
        and it concludes it from ABSENCE: no path under `.github/workflows/`
        or `scripts/midtermpanel/` in the list means `high_risk=False`, no
        HIGH_RISK_WORKFLOW_CHANGE marker, and a human merge gate never told
        that the pull request edits the lane reviewing it. That marker is the
        sole mitigation this workflow's own header names for the accepted
        repository-scoped-secret residual.

        This asked for `per_page=300`. GitHub caps `per_page` at 100 and
        clamps silently, so the request was never honoured: a pull request
        touching more than 100 files returned a path-sorted first hundred and
        nothing said so. A diff of 120 files under `app/` plus one under
        `scripts/midtermpanel/` therefore reported `high_risk=False`.

        Bounded, and the bound is a refusal. Concluding "no risky path" from a
        list that did not end is the failure this exists to prevent."""
        collected: list = []
        for page in range(1, MAX_CHANGED_FILE_PAGES + 1):
            got = self._get(
                f"/pulls/{int(pr_number)}/files"
                f"?per_page={CHANGED_FILE_PAGE_SIZE}&page={page}",
                where="pull-files")
            if not isinstance(got, list):
                refuse("category=pull_files_response_not_a_list")
            collected.extend(str(entry.get("filename") or "") for entry in got
                             if isinstance(entry, dict))
            if len(got) < CHANGED_FILE_PAGE_SIZE:
                return collected
        refuse(f"category=changed_files_did_not_end "
               f"pages={MAX_CHANGED_FILE_PAGES} collected={len(collected)} — "
               "the changed-file list did not terminate within the page "
               "bound, so `high_risk=False` cannot be concluded from it; a "
               "diff this large needs a human source review rather than a "
               "risk classification drawn from its first pages")

    def workflow_run(self, run_id: int) -> dict:
        """The triggering run itself, by exact id.

        Read because the `workflow_run` event payload says what completed but
        the RUN says what it tested. A pull-request CI run checks out a
        generated `refs/pull/N/merge` commit, so its green is evidence about a
        merge of the candidate with a specific base — and that base is a fact
        about the past, recoverable only from the run."""
        got = self._get(f"/actions/runs/{int(run_id)}", where="workflow-run")
        if not isinstance(got, dict):
            refuse("category=workflow_run_response_not_an_object")
        return got

    def workflow_run_jobs(self, run_id: int) -> list:
        """That run's own jobs, so 'CI was green' is about THIS run.

        The exact-head check-run query stays as a second control, but it
        answers a different question: it says some run reported green on this
        commit, not that the run which triggered this panel did."""
        got = self._get(f"/actions/runs/{int(run_id)}/jobs?per_page=100",
                        where="workflow-run-jobs")
        jobs = (got or {}).get("jobs")
        if not isinstance(jobs, list):
            refuse("category=workflow_run_jobs_response_malformed")
        return jobs

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
