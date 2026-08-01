"""R06 — the inputs the lane reads, and the steps that actually produce them.

The finding this closes is narrow and total: both workflow templates named
paths like `${{ runner.temp }}/operator-records.json` and NOTHING in either
file wrote one. Every consumer was correct, every path was spelled right, and
on a real runner the first thing `d1cli.read_environment` would have found is a
file that does not exist. The lane would refuse — fail-closed, so nothing
unsafe — but it would refuse on `d1_input_unreadable` forever, and no test
showed it, because every test hands the runtime its documents directly.

**One producer per consumed path, in the same job, strictly before the lane.**
That sentence is the design, and `assert_producer_consumer_graph` checks it.

**Where each input comes from, and why it cannot come from anywhere else.**

`protected-state.json`
    Branch protection and environment records need repository *administration*
    reads. A workflow token cannot have them — `permissions:` has no
    `administration` scope — so this is an installation token's answer, taken
    out of band and supplied as an environment secret. That is not a weakening:
    EX5-R21 binds prerequisite 7 to the digest of the exact document evaluated,
    so a stale or edited observation is one the operator has not approved.

`operator-records.json`, `operator-revocations.json`
    The operator's own documents. They are authenticated by MAC against a trust
    store the candidate never sees, so their transport does not need to be
    trusted — but their PRESENCE does, and "no revocation list" must never read
    as "no revocations".

`engine.tar.gz`, `engine-identity.json`
    Produced by the protected engine build and published as release assets on
    one tag. Downloaded here with the workflow's own `contents: read` token and
    verified by a digest that comes from a repository variable, so a release
    cannot supply both the bytes and the number they are checked against.

**What this module will not do.** Invent a default for any of them. A default
is how one of these quietly stops being checked, and the failure would be
silent in exactly the direction that matters.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from .errors import refuse

#: Inputs written from an environment SECRET, by `write_secret_documents`.
SECRET_DOCUMENTS = {
    "TRUSTED_OBSERVATION_PATH": "TRUSTED_PROTECTED_STATE_OBSERVATION",
    "TRUSTED_OPERATOR_RECORDS_PATH": "TRUSTED_OPERATOR_RECORDS",
    "TRUSTED_REVOCATION_LIST_PATH": "TRUSTED_OPERATOR_REVOCATIONS",
}

#: Release assets the protected engine build publishes, written by
#: `fetch_engine_release`. The artifact and the identity record that describes
#: it travel on ONE tag, because an identity record from one build beside an
#: artifact from another is the exact mismatch
#: `enginebridge.assert_identity_is_this_engine` exists to catch.
RELEASE_ASSETS = {
    "TRUSTED_ENGINE_ARTIFACT_PATH": "engine.tar.gz",
    "TRUSTED_ENGINE_IDENTITY_PATH": "engine-identity.json",
}

#: D1's remaining consumed path, and it is the candidate's own document rather
#: than the operator's — so it is written by `write_candidate_plan`, out of the
#: inert fetch, and never from a secret.
CANDIDATE_DOCUMENTS = {"TRUSTED_CANDIDATE_PLAN_PATH": "candidate-plan.json"}

#: D2's two remaining consumed paths: the documents D1 SIGNED. They are private
#: — the plan carries the exact prompt bytes — so they are retained as a
#: private artifact of the D1 run and downloaded from it by run id, which needs
#: `actions: read` and no new secret. Not an environment secret: a signed
#: document pasted into a secret is a document whose freshness nobody tracks,
#: and D2 compares the run id it came from against the plan's own.
D1_ARTIFACT_DOCUMENTS = {
    "TRUSTED_PLAN_PATH": "trusted-executable-plan",
    "TRUSTED_COUNT_EVIDENCE_PATH": "trusted-count-evidence",
}

#: Which function writes which path variable. The producer-consumer check reads
#: THIS rather than guessing from the shape of a shell script: these are the
#: only functions that write a consumed path, so a step that calls one is a
#: producer and a step that does not is not.
PRODUCER_FUNCTIONS = {
    **{name: "write_secret_documents" for name in SECRET_DOCUMENTS},
    **{name: "fetch_engine_release" for name in RELEASE_ASSETS},
    **{name: "write_candidate_plan" for name in CANDIDATE_DOCUMENTS},
}

#: Paths produced by an ACTION rather than by lane code, named by the action's
#: repository. Pinned SHAs live in `actionpolicy`; this table is about which
#: step writes which file, and matching on the repository part keeps the two
#: facts in the module that owns each.
PRODUCER_ACTIONS = {name: "actions/download-artifact"
                    for name in D1_ARTIFACT_DOCUMENTS}

#: Paths the LANE writes for itself. Requiring a producer step for these would
#: demand that the workflow pre-create a directory the clone must own, or
#: pre-create the engine root before the artifact that fills it is verified.
#:
#: `…_OUTPUT_PATH` is the naming convention for the other direction: D1 WRITES
#: its two signed documents there for the retain steps to upload. They are
#: listed by name rather than matched by suffix, so adding one is a decision
#: rather than a spelling.
LANE_WRITTEN_PATHS = frozenset({"TRUSTED_CANDIDATE_CHECKOUT",
                                "TRUSTED_ENGINE_ROOT",
                                "TRUSTED_WORKFLOW_DIR",
                                "TRUSTED_PLAN_OUTPUT_PATH",
                                "TRUSTED_COUNT_EVIDENCE_OUTPUT_PATH"})

#: The step that consumes everything. `laneentry.main` is the single entry the
#: templates call, so "the consumer" is not a guess either.
CONSUMER_CALL = "laneentry.main"

#: A release tag an operator approves. Restricted rather than escaped: a tag
#: with a slash in it names a location, and escaping it into one literal
#: segment is safer than nothing and still not the tag anyone approved.
#:
#: Restriction rather than `urllib.parse.quote` is also what keeps this module
#: free of any import a network gate would flag. `urllib.parse` opens no
#: socket, but the lane's rule is about NAMES for a reason — a module that
#: imports part of `urllib` is one import away from importing the part that
#: connects, and the gate cannot tell the difference at review time.
_TAG = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

#: `owner/repo`, and nothing that could be a path segment of its own.
_REPOSITORY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
                         r"/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")

#: Generous for an engine tarball and far below what would fill a runner. A
#: download with no bound is a runner a release can exhaust.
MAX_ASSET_BYTES = 64 * 1024 * 1024


def _assert_inside(target: str, *, destination_dir: str, variable: str) -> str:
    real = os.path.realpath(target)
    if os.path.dirname(real) != os.path.realpath(destination_dir):
        refuse(f"category=trusted_input_path_outside_destination "
               f"variable={variable} — the path is not reported; it carries "
               "the runner layout")
    return real


def write_secret_documents(environ, *, destination_dir: str) -> dict:
    """Materialize the three operator documents from environment secrets.

    Validated as JSON here and not beyond that. The observation is checked by
    `protectedstate`, the records by `trustedverifier` against a trust store,
    the revocations by the verifier that reads them — and a second, weaker
    opinion at this boundary is duplicated policy.

    An ABSENT secret refuses, and that is the case that matters most: an empty
    revocation list and a missing one are indistinguishable to a reader and
    opposite in meaning."""
    if not os.path.isdir(destination_dir):
        refuse("category=materialization_destination_absent")
    written = {}
    for path_var, secret_var in sorted(SECRET_DOCUMENTS.items()):
        raw = environ.get(secret_var)
        if not raw:
            refuse(f"category=trusted_input_secret_absent secret={secret_var} "
                   "— an absent document is not an empty one; a lane that "
                   "reads a missing revocation list as no revocations cannot "
                   "be told to stop")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            refuse(f"category=trusted_input_secret_not_json "
                   f"secret={secret_var} "
                   f"exception_class={type(exc).__name__} — the decoder's "
                   "message is not reported, because it quotes the document "
                   "and this one is a secret")
        target = environ.get(path_var)
        if not target:
            refuse(f"category=trusted_input_path_unset variable={path_var}")
        real = _assert_inside(target, destination_dir=destination_dir,
                              variable=path_var)
        with open(real, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
        written[path_var] = {"source_secret": secret_var,
                             "document_sha256": hashlib.sha256(
                                 raw.encode("utf-8")).hexdigest()}
    return {"materialized": written,
            "honest_scope": (
                "these documents were written from environment secrets. "
                "Nothing here says they are true — the operator records are "
                "authenticated by MAC against a trust store, and the "
                "observation is bound by digest to the prerequisite that "
                "approves it")}


def release_url(*, api_url: str, repository: str, tag: str) -> str:
    """The release-by-tag URL, with every part checked before it is joined."""
    if not _TAG.match(tag or ""):
        refuse(f"category=engine_release_tag_not_permitted tag={tag!r} — a tag "
               "with a path separator in it names a location, not a release")
    if not isinstance(api_url, str) or not api_url.startswith("https://"):
        refuse(f"category=engine_release_api_not_https url={api_url!r}")
    if not isinstance(repository, str) or not _REPOSITORY.match(repository):
        refuse("category=engine_release_repository_malformed")
    return f"{api_url}/repos/{repository}/releases/tags/{tag}"


def select_asset(release, *, name: str) -> str:
    """One asset by exact name; a duplicate is a refusal, not a first match.

    Two assets with one name means which bytes arrive depends on API ordering,
    and "pick the first" is a decision nobody made."""
    if not isinstance(release, dict):
        refuse("category=engine_release_not_an_object")
    assets = release.get("assets")
    if not isinstance(assets, list):
        refuse("category=engine_release_has_no_assets")
    matching = [a for a in assets
                if isinstance(a, dict) and a.get("name") == name]
    if not matching:
        refuse(f"category=engine_release_asset_absent asset={name} — the "
               "protected engine build publishes it; a release without it is "
               "not a build this lane can verify")
    if len(matching) > 1:
        refuse(f"category=engine_release_asset_duplicated asset={name}")
    url = matching[0].get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        refuse(f"category=engine_release_asset_url_not_https asset={name}")
    return url


def fetch_engine_release(environ, *, destination_dir: str, fetch) -> dict:
    """Download the approved engine artifact and its identity record.

    `fetch(url, *, accept)` is injected for the same reason every other
    capability in this lane is: the whole path is exercisable against a fake
    server, and the phase gate sits on OBTAINING the capability rather than on
    the code that uses it.

    The artifact digest is verified against `TRUSTED_ENGINE_DIGEST`, which is a
    repository variable the operator sets — so the release supplies the bytes
    and something else supplies the number they are checked against. A release
    that supplied both would be checking itself.

    The identity record is NOT verified by digest here. It is checked by
    `enginebridge.assert_identity_is_this_engine` against the artifact this run
    opened, which is the comparison that matters; a digest of the identity file
    would only prove the file had not changed since someone wrote down its
    digest."""
    if not callable(fetch):
        refuse("category=engine_release_fetch_not_callable")
    if not os.path.isdir(destination_dir):
        refuse("category=materialization_destination_absent")
    tag = environ.get("TRUSTED_ENGINE_RELEASE_TAG")
    expected = environ.get("TRUSTED_ENGINE_DIGEST")
    if not expected or len(expected) != 64:
        refuse("category=engine_artifact_digest_not_configured — the digest is "
               "the only thing separating the approved engine from whatever a "
               "release happens to carry")
    url = release_url(api_url=environ.get("GITHUB_API_URL", ""),
                      repository=environ.get("GITHUB_REPOSITORY", ""),
                      tag=tag)
    body = fetch(url, accept="application/vnd.github+json")
    try:
        release = json.loads(body)
    except json.JSONDecodeError as exc:
        refuse(f"category=engine_release_not_json "
               f"exception_class={type(exc).__name__}")

    written = {}
    for path_var, asset_name in sorted(RELEASE_ASSETS.items()):
        target = environ.get(path_var)
        if not target:
            refuse(f"category=trusted_input_path_unset variable={path_var}")
        real = _assert_inside(target, destination_dir=destination_dir,
                              variable=path_var)
        payload = fetch(select_asset(release, name=asset_name),
                        accept="application/octet-stream")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            refuse(f"category=engine_release_asset_empty asset={asset_name}")
        if len(payload) > MAX_ASSET_BYTES:
            refuse(f"category=engine_release_asset_too_large "
                   f"asset={asset_name} bytes={len(payload)}")
        with open(real, "wb") as handle:
            handle.write(payload)
        written[path_var] = {"asset": asset_name,
                             "sha256": hashlib.sha256(payload).hexdigest()}

    artifact = written["TRUSTED_ENGINE_ARTIFACT_PATH"]["sha256"]
    if artifact != expected:
        refuse(f"category=engine_release_artifact_digest_mismatch "
               f"downloaded={artifact} approved={expected} — the release "
               "carries bytes nobody approved, and the approval is the only "
               "thing that makes this artifact the engine")
    return {"release_tag": tag, "materialized": written,
            "honest_scope": (
                "the artifact matched the digest configured out of band. The "
                "identity record is unverified here on purpose — it is checked "
                "against the artifact this run opens, which is the comparison "
                "that means something")}


#: Where the candidate's declared plan lives in the candidate's own tree. Fixed
#: in trusted policy rather than taken from the environment: a runner-supplied
#: path is a runner choosing which of the candidate's files is "the plan".
CANDIDATE_PLAN_REPO_PATH = "artifacts/verifier/candidate-review-plan.json"


def write_candidate_plan(environ, *, destination_dir: str, clone_dir: str,
                         read_blob) -> dict:
    """Read the candidate's declared plan out of the INERT clone.

    The template previously pointed `TRUSTED_CANDIDATE_PLAN_PATH` inside the
    candidate checkout — a clone made with `--no-checkout`, which by
    construction has no working tree, so the path could never exist. `git
    cat-file blob <sha>:<path>` reads the committed bytes without one, which is
    the whole reason the fetch can stay inert.

    Bound to the candidate HEAD by SHA, not to a ref. A ref is a name that can
    move between the fetch and the read; the head is the commit whose review
    this is."""
    if not callable(read_blob):
        refuse("category=candidate_plan_reader_not_callable")
    head = environ.get("CANDIDATE_HEAD_SHA")
    if not head:
        refuse("category=candidate_head_sha_unset")
    target = environ.get("TRUSTED_CANDIDATE_PLAN_PATH")
    if not target:
        refuse("category=trusted_input_path_unset "
               "variable=TRUSTED_CANDIDATE_PLAN_PATH")
    real = _assert_inside(target, destination_dir=destination_dir,
                          variable="TRUSTED_CANDIDATE_PLAN_PATH")
    payload = read_blob(head, CANDIDATE_PLAN_REPO_PATH, cwd=clone_dir)
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        refuse("category=candidate_plan_blob_empty — the candidate declares a "
               "plan and the trusted lane rebuilds it; an absent declaration "
               "is a review with nothing to compare against")
    try:
        json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        refuse(f"category=candidate_plan_not_json "
               f"exception_class={type(exc).__name__} — the message is not "
               "reported; it quotes candidate content")
    with open(real, "wb") as handle:
        handle.write(payload)
    return {"TRUSTED_CANDIDATE_PLAN_PATH": {
                "candidate_head_sha": head,
                "repository_path": CANDIDATE_PLAN_REPO_PATH,
                "sha256": hashlib.sha256(payload).hexdigest()},
            "honest_scope": (
                "these are the candidate's own bytes, at the candidate's own "
                "commit. They are a CLAIM; the trusted lane rebuilds the plan "
                "from the engine and compares")}


def assert_producer_consumer_graph(document, *, required_env) -> dict:
    """Every consumed path is written by a STRICTLY earlier step in its job.

    Read over the parsed workflow, and over `PRODUCER_FUNCTIONS` rather than
    over the shape of a shell script. That distinction is the point: "does this
    step look like it writes a file" is a proxy question, and the real one —
    "does this step call the function that writes this path" — has an exact
    answer, because these are the only functions that write these paths.

    Order matters and is checked as order. A producer in a later step is a
    consumer reading a file that does not exist yet; a producer in another job
    is a producer whose `runner.temp` is a different machine."""
    consumed = sorted(name for name in required_env
                      if name.endswith("_PATH")
                      and name not in LANE_WRITTEN_PATHS)
    unproducible = [n for n in consumed
                    if n not in PRODUCER_FUNCTIONS
                    and n not in PRODUCER_ACTIONS]
    if unproducible:
        refuse(f"category=consumed_path_has_no_named_producer "
               f"variables={unproducible} — a path the lane reads and no "
               "function writes cannot be checked for, so it would be missing "
               "on a runner and present in every test")

    findings = []
    for job_name, job in sorted((document.get("jobs") or {}).items()):
        steps = job.get("steps") or []
        consumer_index = None
        produced: dict = {}
        for index, step in enumerate(steps):
            script = str(step.get("run") or "")
            uses = str(step.get("uses") or "").split("@")[0]
            declared = set(step.get("env") or {})
            if uses:
                for name, action in PRODUCER_ACTIONS.items():
                    # The action AND the variable naming the file it writes.
                    # The action alone would let any `download-artifact` step
                    # anywhere in the job satisfy every artifact-backed path.
                    if uses == action and name in declared:
                        produced.setdefault(name, index)
            if not script:
                continue
            for name, function in PRODUCER_FUNCTIONS.items():
                if function in script and name not in produced:
                    produced[name] = index
            if CONSUMER_CALL in script:
                consumer_index = index
        if consumer_index is None:
            continue
        for name in consumed:
            at = produced.get(name)
            if at is None:
                findings.append({"job": job_name, "variable": name,
                                 "problem": "no producer step"})
            elif at >= consumer_index:
                findings.append({"job": job_name, "variable": name,
                                 "problem": "producer runs after the lane"})
    return {"consumed_paths": consumed,
            "findings": findings,
            "graph_is_complete": not findings}
