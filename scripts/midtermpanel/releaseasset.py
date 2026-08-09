"""Materialise the exact `engine-identity.json` asset from the approved release.

## Why this file exists

`engine.load_release_engine_identity` refuses a provider-backed run without
`MIDTERM_APPROVED_ENGINE_IDENTITY_PATH`, and nothing produced that path. The
consumer was correct and unreachable: the first provider-backed count job
would have stopped at `approved_engine_identity_not_configured` before
counting anything. Fail-closed, and also not operational.

## What it is allowed to hold

`GITHUB_TOKEN` and nothing else. This step runs BEFORE the step that receives
`TRUSTED_VERIFIER_OPENAI_KEY`, and the ordering is the point: a download that
could reach the provider is a download whose compromise costs money, and the
release asset is fetched from a service that has no business being near a
provider credential.

## What it refuses

The asset is the operator's approval made readable, so every way of getting a
*different* document has to be a named refusal rather than a fallback:

  * no release for the configured tag, or a draft one
  * a release belonging to a different repository
  * zero assets named `engine-identity.json`, or more than one
  * a response larger than an identity document can legitimately be
  * a redirect away from the approved hosts
  * a destination that is not a fresh regular file
  * a document whose sha256 is not the approved one

The digest check is what makes the rest of it advisory: even if every step
above were subverted, a document that does not hash to the approved value is
refused. It is checked here AND the document is strict-loaded here, so a
malformed approval fails in the cheap step rather than inside the job holding
the key.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .errors import refuse

#: The one asset name this lane reads from a release.
IDENTITY_ASSET_NAME = "engine-identity.json"

#: Hosts a release-asset download may legitimately touch. A redirect anywhere
#: else is refused rather than followed: the whole point of the fetch is that
#: the bytes came from the operator's release.
PERMITTED_HOSTS = ("api.github.com", "objects.githubusercontent.com",
                   "release-assets.githubusercontent.com",
                   "github.com", "codeload.github.com")

#: An identity record is tens of kilobytes. Bounded so a pathological response
#: is a named refusal rather than a memory event on a runner.
MAX_ASSET_BYTES = 1 << 20

API_ROOT = "https://api.github.com"


def _request(url: str, *, token: str, accept: str) -> bytes:
    """One bounded GET, with the token and nothing else."""
    request = urllib.request.Request(url, method="GET")  # noqa: S310
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", accept)
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            final = urllib.parse.urlparse(response.geturl()).hostname or ""
            if final not in PERMITTED_HOSTS:
                refuse(f"category=release_asset_redirected_off_host "
                       f"host={final!r} permitted={list(PERMITTED_HOSTS)}")
            payload = response.read(MAX_ASSET_BYTES + 1)
    except urllib.error.HTTPError as exc:
        refuse(f"category=release_api_http_error status={exc.code} — the "
               "release the operator approved could not be read; the URL is "
               "not reported because it can carry a token in a query string")
    except (urllib.error.URLError, OSError) as exc:
        refuse(f"category=release_api_unreachable "
               f"exception_class={type(exc).__name__}")
    if len(payload) > MAX_ASSET_BYTES:
        refuse(f"category=release_asset_too_large limit_bytes={MAX_ASSET_BYTES}")
    return payload


def resolve_identity_asset(*, owner: str, repo: str, tag: str, token: str,
                           repository_numeric_id: int,
                           permit_draft: bool = False) -> dict:
    """Find exactly one `engine-identity.json` on the tagged release."""
    if not tag:
        refuse("category=release_tag_not_configured — there is no approved "
               "release to read an identity document from")
    if not token:
        refuse("category=release_api_token_missing — reading a release needs "
               "GITHUB_TOKEN; it must never be the provider key")
    body = _request(f"{API_ROOT}/repos/{owner}/{repo}/releases/tags/{tag}",
                    token=token, accept="application/vnd.github+json")
    try:
        release = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        refuse(f"category=release_api_response_not_json "
               f"exception_class={type(exc).__name__}")
    if not isinstance(release, dict):
        refuse("category=release_api_response_not_an_object")
    if release.get("draft") and not permit_draft:
        refuse(f"category=release_is_a_draft tag={tag!r} — a draft can be "
               "edited or deleted without changing its tag, so it is not "
               "something an operator approval can name")
    observed_repo = ((release.get("repository") or {}).get("id")
                     if isinstance(release.get("repository"), dict) else None)
    if observed_repo is not None and observed_repo != repository_numeric_id:
        refuse(f"category=release_belongs_to_a_different_repository "
               f"observed={observed_repo} expected={repository_numeric_id}")
    assets = release.get("assets")
    if not isinstance(assets, list):
        refuse("category=release_assets_not_a_list")
    matching = [a for a in assets if isinstance(a, dict)
                and a.get("name") == IDENTITY_ASSET_NAME]
    if not matching:
        refuse(f"category=release_identity_asset_missing "
               f"tag={tag!r} expected_name={IDENTITY_ASSET_NAME!r}")
    if len(matching) > 1:
        refuse(f"category=release_identity_asset_ambiguous count={len(matching)}"
               f" — {IDENTITY_ASSET_NAME!r} appears more than once, so "
               "'the' approved document does not identify one file")
    asset = matching[0]
    url = asset.get("url")
    if not isinstance(url, str) or not url.startswith(f"{API_ROOT}/"):
        refuse("category=release_identity_asset_url_unexpected")
    return {"asset_url": url, "asset_id": asset.get("id"),
            "release_id": release.get("id"), "tag": tag}


def materialise_release_identity(*, owner: str, repo: str, tag: str,
                                 token: str, repository_numeric_id: int,
                                 expected_sha256: str, destination: str,
                                 permit_draft: bool = False) -> dict:
    """Download, verify and strict-load the approved identity document.

    Returns the destination path and the digest actually observed. The caller
    exports the path; nothing writes an absolute runner path into governance,
    because a path is where a file happens to be on one machine and the
    approval is about the bytes."""
    from trustedlane import enginebuild

    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        refuse("category=release_identity_expected_digest_malformed — the "
               "operator must have approved an exact identity document "
               "digest before a run may fetch one")
    located = resolve_identity_asset(
        owner=owner, repo=repo, tag=tag, token=token,
        repository_numeric_id=repository_numeric_id, permit_draft=permit_draft)
    payload = _request(located["asset_url"], token=token,
                       accept="application/octet-stream")

    observed = hashlib.sha256(payload).hexdigest()
    if observed != expected_sha256:
        refuse(f"category=release_identity_digest_mismatch "
               f"expected={expected_sha256[:16]} observed={observed[:16]} — "
               "the release asset is not the document the operator approved")

    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.lexists(destination):
        # Never write through whatever is already there: a symlink at the
        # destination would place approved-looking content somewhere else.
        refuse("category=release_identity_destination_exists — refusing to "
               "overwrite; the destination must be a fresh path in the "
               "runner temp directory")
    with open(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
              "wb") as handle:
        handle.write(payload)

    # Strict-load immediately, so a malformed or relabelled approval fails
    # here rather than inside the job that holds the provider key.
    record = enginebuild.strict_load_built_identity(destination)
    return {"path": destination,
            "engine_identity_sha256": observed,
            "state": record["state"],
            "release_tag": located["tag"],
            "asset_id": located["asset_id"],
            "honest_scope": (
                "the document hashes to the digest the operator approved and "
                "is internally sealed. Whether that approval was the right "
                "one is not a question this step can answer")}


def main() -> None:
    """Append the materialised path to `$GITHUB_ENV`. No provider secret."""
    import sys

    from trustedlane import enginebuild as _enginebuild

    from . import REPOSITORY_NUMERIC_ID
    from .engine import ENGINE_IDENTITY_DIGEST_ENV, ENGINE_RELEASE_TAG_ENV

    # The ordering guarantee, checked rather than documented: this step runs
    # before the provider key is introduced, and refuses if one is already in
    # scope. Reuses the trusted lane's own check so there is one list of
    # capability names rather than a second that can drift.
    _enginebuild.assert_build_holds_no_provider_secret(os.environ)

    temp = os.environ.get("RUNNER_TEMP") or refuse(
        "category=runner_temp_not_set")
    destination = os.path.join(temp, "midterm", "release",
                               IDENTITY_ASSET_NAME)
    result = materialise_release_identity(
        owner="mglaeser", repo="bubble-regime-monitor",
        tag=os.environ.get(ENGINE_RELEASE_TAG_ENV, ""),
        token=os.environ.get("GITHUB_TOKEN", ""),
        repository_numeric_id=REPOSITORY_NUMERIC_ID,
        expected_sha256=os.environ.get(ENGINE_IDENTITY_DIGEST_ENV, ""),
        destination=destination)
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        refuse("category=github_env_not_set")
    with open(github_env, "a", encoding="utf-8") as handle:
        handle.write(f"MIDTERM_APPROVED_ENGINE_IDENTITY_PATH={result['path']}\n")
    json.dump({k: v for k, v in result.items() if k != "path"}, sys.stdout,
              indent=2, sort_keys=True)
    sys.stdout.write("\n")
