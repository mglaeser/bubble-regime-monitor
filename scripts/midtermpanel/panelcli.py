"""`python -m midtermpanel.panelcli` — execute the counted plan, publish the review.

Consumes the artifact the count job uploaded IN THIS RUN. It does not rebuild the
plan: the plan carries the provider's counts, and a rebuild without calling the
provider has nothing to compare them against.
"""

from __future__ import annotations

import os
import sys

from . import (
    COUNT_EVIDENCE_CLASS,
    PANEL_EVIDENCE_CLASS,
    REPOSITORY_NUMERIC_ID,
    REVIEW_STATUS,
)
from .clibase import require_env, run, self_test_report, self_test_requested
from .errors import PanelRefusal, refuse
from .evidence import (
    panel_evidence,
    strict_load,
    strict_load_plan,
    write_atomic,
)
from .panel import (
    aggregate,
    anti_copy_tripwire,
    assert_synthesis_cannot_clear_a_refutation,
    verify_handoff,
)
from .status import pending, publish, status_request

REQUIRED = ("GITHUB_TOKEN", "CANDIDATE_HEAD_SHA", "CANDIDATE_BASE_SHA",
            "MIDTERM_ENGINE_DIGEST", "MIDTERM_POLICY_DIGEST",
            "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")

#: Where the same-run download-artifact step places the count job's output.
INPUT_DIRNAME = "count-input"



def _runner_temp(environ: dict) -> str:
    """The runner's private temp directory. Refused when absent.

    Deliberately not defaulted to `/tmp`. The files written here are the private
    plan and the private verdicts — the candidate's code and the reviewer's
    questions — and a world-readable fallback is the wrong place for them. A
    missing `RUNNER_TEMP` means this is not running where it thinks it is, which
    is worth a refusal rather than a guess."""
    temp = str(environ.get("RUNNER_TEMP") or "").strip()
    if not temp:
        raise PanelRefusal(
            "MIDTERM_PANEL_REFUSED",
            "category=runner_temp_absent — private evidence must be written to "
            "the runner's own temp directory; falling back to /tmp would put "
            "the candidate's code and the reviewer's questions somewhere "
            "world-readable")
    return temp

def input_paths(temp: str) -> dict:
    base = os.path.join(temp, "midterm", INPUT_DIRNAME)
    return {"evidence": os.path.join(base, "count-evidence.json"),
            "plan": os.path.join(base, "executable-plan.json")}


def review_paths(temp: str) -> dict:
    """Where the sanitized human-readable review is retained.

    Beside `panel-evidence.json` and never instead of it. The evidence file is
    the cryptographic record — full per-model verdicts, proofs, digests, all
    private. These two are the SANITIZED reading of it: exactly what was
    published (`.md`) and the structured object it was rendered from (`.json`),
    so a reader can check what the panel showed a human against what it
    actually decided."""
    base = os.path.join(temp, "midterm")
    return {"markdown": os.path.join(base, "panel-review.md"),
            "json": os.path.join(base, "panel-review.json")}


def retain_review(environ: dict, *, review: dict, body: str,
                  publication: dict) -> dict:
    """Write both artifacts. Atomic for the JSON, and plain for the Markdown.

    The Markdown is written with the same temp-and-rename discipline by hand
    rather than through `write_atomic`, which is typed for evidence records and
    would refuse a string."""
    from .evidence import write_atomic
    paths = review_paths(_runner_temp(environ))
    os.makedirs(os.path.dirname(paths["markdown"]), exist_ok=True)
    temporary = f"{paths['markdown']}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(temporary, paths["markdown"])
    write_atomic({"schema_version": 1,
                  "artifact": "midterm-panel-readable-review",
                  "publication_class": "private_sanitized",
                  "review": review,
                  "publication": publication,
                  "published_body_sha256": _sha256(body),
                  "published_body_chars": len(body)},
                 paths["json"])
    return paths


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def perform(environ: dict, *, execute_fn, opener,
            panel_identity: dict | None = None,
            require_identity: bool = False,
            scan=None, publish_review_fn=None) -> dict:
    """Verify the handoff, execute, aggregate, publish. Injected seams only.

    The challenge comes from the PLAN, not from the environment. It is what the
    count job actually counted, and `require_approvals` uses it to check that
    each verdict was written for this run — so reading it from a variable that
    the panel job sets independently would let the two disagree while every
    check still passed."""

    env = require_env(environ, REQUIRED, where="panelcli")
    head, base = env["CANDIDATE_HEAD_SHA"], env["CANDIDATE_BASE_SHA"]
    run_id, attempt = int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"])
    target = environ.get("MIDTERM_RUN_URL") or (
        "https://github.com/mglaeser/bubble-regime-monitor/actions/runs/"
        f"{run_id}")

    paths = input_paths(_runner_temp(environ))
    count_record = strict_load(
        paths["evidence"], expected_class=COUNT_EVIDENCE_CLASS,
        expected_head=head, expected_base=base,
        expected_engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        expected_policy_digest=env["MIDTERM_POLICY_DIGEST"])
    # F-08: the plan was read with a plain `json.loads`, which accepts
    # duplicate keys and keeps the last. Every field here is security-relevant
    # — which requests are sent, under whose challenge, against which pins — so
    # a document declaring one of them twice must refuse rather than parse.
    plan = strict_load_plan(
        paths["plan"], expected_head=head, expected_base=base,
        expected_engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        expected_policy_digest=env["MIDTERM_POLICY_DIGEST"])
    handoff = verify_handoff(
        count_record=count_record, plan=plan, expected_head=head,
        expected_base=base,
        expected_engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        expected_policy_digest=env["MIDTERM_POLICY_DIGEST"],
        panel_identity=panel_identity,
        require_identity=require_identity)
    # The count record's OWN published self-digest, not a fresh hash of it.
    # `digest_of(count_record)` can never equal `evidence_sha256`, because the
    # record contains that field: re-hashing it produces a number nobody else
    # can reproduce, so a field named `count_evidence_sha256` would have
    # identified nothing. This is the digest the status marker and the merge
    # gate already use.
    count_evidence_sha256 = count_record.get("evidence_sha256")

    published = [publish(
        pending(candidate_head_sha=head, context=REVIEW_STATUS,
                target_url=target, run_id=run_id, run_attempt=attempt),
        opener=opener, token=env["GITHUB_TOKEN"])]

    executed = execute_fn(plan=plan)
    verdict = aggregate(votes=executed["votes"],
                        synthesis=executed["synthesis"],
                        challenge=plan["execution_challenge"])
    tripwire = anti_copy_tripwire(executed["votes"])

    state = "success" if verdict["decision"] == "approved" else "failure"
    assert_synthesis_cannot_clear_a_refutation(verdict, proposed_state=state)

    record = panel_evidence(
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        candidate_base_sha=base, engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        policy_digest=env["MIDTERM_POLICY_DIGEST"], run_id=run_id,
        run_attempt=attempt,
        body={"decision": verdict["decision"],
              "models_voting": verdict["models_voting"],
              "required_approver": verdict["required_approver"],
              "strict_any_refutation": verdict["strict_any_refutation"],
              "votes": verdict["votes"],
              "generation_calls": executed["generation_calls"],
              "coverage": executed["coverage"],
              "output_privacy": executed["output_privacy"],
              "generation_ledger_sha256": executed["generation_ledger"].get(
                  "generation_ledger_sha256"),
              # Raw reply -> internal envelope, per model, as digests. This is
              # what makes "the engine saw what the provider sent" checkable
              # rather than asserted.
              "normalization": executed["normalization"],
              "anti_copy": tripwire,
              "plan_sha256": plan["plan_sha256"],
              # The two exact inputs this verdict was produced from, so the
              # final record identifies them rather than describing them.
              "count_evidence_sha256": count_evidence_sha256,
              "identity_compared": handoff["identity_compared"],
              # The same builder identity the count job recorded and this job
              # proved it shares, repeated verbatim.
              **(panel_identity or {})})
    write_atomic(record, os.path.join(_runner_temp(environ),
                                      "midterm", "panel-evidence.json"))

    # The readable review goes out BEFORE the terminal status, and the ordering
    # was corrected by adversarial review. `status.publish` refuses on any HTTP
    # fault, and that refusal propagates — so with the review published after
    # it, a single GitHub hiccup on the status POST would have aborted the run
    # with the finding never rendered, retained or shown. Requirement 9 is that
    # a finding stays readable after it blocks the job; it must also stay
    # readable when the machine channel is the thing that failed.
    #
    # Publishing the human-readable comment a moment before the authoritative
    # status is not a correctness problem: the comment says in its own footer
    # that the machine decision is authoritative, and `finalize` resolves any
    # status this run leaves pending.
    published_review = publish_readable_review(
        environ, plan=plan, executed=executed, verdict=verdict, record=record,
        count_evidence_sha256=count_evidence_sha256, head=head, base=base,
        target=target, run_id=run_id, token=env["GITHUB_TOKEN"], scan=scan,
        publish_review_fn=publish_review_fn)

    published.append(publish(status_request(
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        context=REVIEW_STATUS, state=state,
        description=(f"panel {verdict['decision']}: "
                     f"{len(verdict['models_voting'])} models "
                     "(mid-term, not write-separated)"),
        target_url=target, run_id=run_id, run_attempt=attempt,
        evidence_sha256=record["evidence_sha256"] if state == "success" else None
        ) if state == "success" else status_request(
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        context=REVIEW_STATUS, state=state,
        description=(f"panel {verdict['decision']} — see run summary "
                     "(mid-term, not write-separated)"),
        target_url=target, run_id=run_id, run_attempt=attempt),
        opener=opener, token=env["GITHUB_TOKEN"]))

    # After the status is published and the evidence is on disk, and only then.
    # A blocked review that returns normally leaves the panel JOB green while
    # the commit status is red — and the job is what a person glances at, what
    # a branch rule can be pointed at, and what `finalize` reports. The order
    # is the whole point: refusing first would lose both the published refusal
    # and the evidence that explains it, which is the failure mode the engine's
    # own `decide_unit_or_block` was rewritten to avoid.
    if verdict["decision"] != "approved":
        refuse(f"category=panel_blocked decision={verdict['decision']} "
               f"engine_gate={verdict['engine_gate']['reason']!r} "
               f"strict_gate={verdict['strict_gate']['reason']!r} "
               f"evidence_sha256={record['evidence_sha256'][:16]} — the "
               "readable findings were published to the pull request before "
               "this refusal and are retained in `panel-review.md`; the "
               "evidence carries per-model DIGESTS and counts, not the full "
               "verdict text. This refusal is the process exit, not the "
               "finding")
    return {"published": published, "verdict": verdict, "evidence": record,
            "review": published_review}


def publish_readable_review(environ: dict, *, plan: dict, executed: dict,
                            verdict: dict, record: dict,
                            count_evidence_sha256, head: str, base: str,
                            target: str, run_id: int, token: str, scan,
                            publish_review_fn) -> dict:
    """Render, publish and retain the human-readable review.

    Separated from `perform` so the ordering above stays legible and so a test
    can drive this alone. It returns a record in every case — including the case
    where it could not run at all — because the caller writes that record into
    the private artifact, and "the publisher was not wired up" and "the
    publisher ran and GitHub refused" must not look the same.
    """
    from . import reviewrender

    if not callable(scan) or not callable(publish_review_fn):
        # Reported, never raised. A missing seam is a wiring defect worth
        # seeing, and it is not a reason to discard a completed review.
        return {"published": False, "outcome": "publisher_not_wired",
                "refusal": ("no engine scanner or no publish function was "
                            "supplied; every rendered field must be scanned "
                            "with the engine's own scanner")}
    challenge = plan.get("execution_challenge")
    try:
        review = reviewrender.build_review(
            decision=verdict["decision"], candidate_head_sha=head,
            candidate_base_sha=base, votes=executed["votes"], plan=plan,
            aggregate_record=verdict, scan=scan, run_url=target, run_id=run_id,
            evidence_sha256=record["evidence_sha256"],
            count_evidence_sha256=count_evidence_sha256, challenge=challenge)
        body = reviewrender.render(review, challenge=challenge)
    except Exception as exc:
        # A rendering fault must NEVER become the process's answer. The vertical
        # found this the hard way: a body-level refusal escaped, replaced
        # `category=panel_blocked` as the exit reason, and a run that had
        # correctly refuted three units reported a publication problem instead.
        # The decision is already made and already published; this is a reading
        # of it, and a reading that fails is a reading that is missing.
        reason = getattr(exc, "reason", None)
        print("::warning::midterm panel review could not be rendered: "
              + (reason if isinstance(reason, str)
                 else f"exception_class={type(exc).__name__}"))
        return {"published": False, "outcome": "render_refused",
                "refusal": (reason if isinstance(reason, str)
                            else f"exception_class={type(exc).__name__}")}
    publication = publish_review_fn(
        body=body, pr_number=environ.get("CANDIDATE_PR_NUMBER"),
        candidate_head_sha=head, token=token, challenge=challenge)
    try:
        paths = retain_review(environ, review=review, body=body,
                              publication=publication)
    except OSError as exc:
        # Same rule as above, one layer out: a full disk is not a verdict.
        paths = {"retained": False,
                 "refusal": f"exception_class={type(exc).__name__}"}
    # The panel job's log is the one place an operator looks when a comment did
    # not appear, so the outcome is stated there rather than only on disk.
    if not publication.get("published"):
        print(f"::warning::midterm panel review not published "
              f"(outcome={publication.get('outcome')}): "
              f"{publication.get('refusal')}")
    return {**publication, "render_version": reviewrender.RENDER_VERSION,
            "finding_count": review["finding_count"],
            "retained": paths,
            "decision_unchanged": review["decision"] == verdict["decision"]}


def selected_generation_attempt_cap(environ: dict) -> int:
    """The panel's attempt cap, out of the profile preflight's class selected.

    Read here rather than passed as a workflow literal so that it cannot
    disagree with `VERIFIER_MAX_GENERATION_CALLS`; `inputs.select_profile`
    refuses a profile whose cap is below its own approved call count."""
    from . import inputs
    loaded = inputs.load_pin_values(environ)
    return int(loaded["profile"]["generation_attempt_cap"])


def panel_execution(environ: dict, *, mode: str, transport_factory) -> dict:
    """Load the approved engine, rebuild Stage 1, and return the executor.

    The skeleton is REBUILT here rather than carried over from the count job.
    That is `execute_review_plan`'s own requirement — it refuses when the
    rebuilt skeleton's digest differs from the plan's — and it is the check
    that catches a candidate that moved between the two jobs."""
    from trustedlane import enginebridge

    from . import inputs
    from .engine import MODE_PROVIDER, load_engine_for_mode, resolve_release_config
    from .panel import execute

    release = resolve_release_config(environ)
    repository_path = inputs.load_repository_path(environ)
    temp = _runner_temp(environ)

    opened = load_engine_for_mode(
        mode=mode, release=release,
        reviewed_candidate_head_sha=environ["CANDIDATE_HEAD_SHA"],
        artifact_path=os.path.join(temp, "midterm", "engine-panel.tar.gz"),
        # Job-specific. `artifactload.extract` refuses a non-empty destination,
        # and it is right to: a mixture of an approved engine and whatever a
        # previous job left there is not one. Two jobs sharing a directory
        # would have worked on separate runners and failed the moment anything
        # ran them in one place — which is exactly what the vertical does.
        extract_to=os.path.join(temp, "midterm", "engine-panel"),
        repository_numeric_id=REPOSITORY_NUMERIC_ID,
        candidate_paths=[repository_path],
        cwd=environ.get("GITHUB_WORKSPACE", "."))
    engine = opened["engine"]
    skeleton = enginebridge.build_skeleton(
        engine, target_base_sha=environ["CANDIDATE_BASE_SHA"],
        candidate_head_sha=environ["CANDIDATE_HEAD_SHA"],
        repository_path=repository_path)
    authorizations, _ = inputs.load_authorizations(
        environ, engine=engine, skeleton=skeleton)
    # The adapter that reads the provider's answers is identified by the engine
    # the operator approved, so the digest comes from the VERIFIED identity
    # rather than from anything this job could compute about itself.
    transport = transport_factory(
        engine, engine_source_sha256=opened["engine_source_digest"])

    def execute_fn(*, plan):
        return execute(engine=engine, plan=plan, skeleton=skeleton,
                       transport=transport, repository_path=repository_path,
                       authorizations=authorizations)

    return {"execute_fn": execute_fn, "engine_identity": opened,
            "identity_binding": identity_binding(opened),
            # The ENGINE's scanner, for the readable review. Every field the
            # publisher renders is scanned with the operator-approved artifact's
            # own copy rather than with one this package imported for itself.
            "scan": enginebridge.secret_scanner(engine),
            # Only a provider-backed run has an identity to compare. A dry run
            # has none, and saying so is different from silently comparing two
            # absences and calling it verified.
            "require_identity": mode == MODE_PROVIDER,
            "transport": transport}


def identity_binding(opened: dict) -> dict:
    """The builder identity the panel actually loaded, in count's own shape.

    Built from the same keys `countcli` writes into count evidence, so the
    comparison in `verify_handoff` is field-for-field rather than two
    near-identical vocabularies that agree until one of them is edited."""
    return {
        "engine_identity_sha256": opened.get("engine_identity_sha256"),
        "engine_identity_state": opened.get("engine_identity_state"),
        "engine_native_branch_protection":
            opened.get("native_branch_protection"),
        "engine_control_class": opened.get("control_class"),
        "engine_build_run_id": opened.get("engine_build_run_id"),
        "engine_build_run_attempt": opened.get("engine_build_run_attempt"),
        "engine_provenance_sha256": opened.get("engine_provenance_sha256"),
        "engine_artifact_sha256": opened.get("engine_artifact_sha256"),
        "engine_release_binding_sha256":
            opened.get("engine_release_binding_sha256"),
    }


def main() -> None:
    import json

    from . import dryrun
    from .engine import MODE_DRY_RUN, MODE_PROVIDER
    from .transport import (
        live_generation_transport,
        provider_http_opener,
        read_provider_key,
    )

    environ = dict(os.environ)

    if dryrun.is_dry_run(environ):
        dryrun.assert_no_credential_is_present(environ)
        sink = dryrun.status_sink(environ)
        prepared = panel_execution(
            environ, mode=MODE_DRY_RUN,
            transport_factory=dryrun.generation_transport_factory(environ))
        outcome = perform(environ, execute_fn=prepared["execute_fn"],
                          opener=sink,
                          panel_identity=prepared["identity_binding"],
                          require_identity=prepared["require_identity"],
                          scan=prepared["scan"],
                          # RENDERED and retained, never sent. See
                          # `reviewpublish.not_published_in_a_dry_run` for why
                          # this is not a sink: the comment publisher reads the
                          # pull request's head and its comment list, and a sink
                          # answering those would be a model of GitHub rather
                          # than a record of what was constructed.
                          publish_review_fn=_dry_run_review_publisher())
        proof = dryrun.record(environ, sink=sink,
                              transport=prepared["transport"])
        print(json.dumps({"dry_run": proof, "verdict": outcome["verdict"],
                          "review": outcome["review"]},
                         indent=2, sort_keys=True, default=str))
        return

    key = read_provider_key(environ)
    # F-06: from the selected operator profile, never a workflow literal.
    # `MIDTERM_GENERATION_ATTEMPT_CAP: '60'` contradicted the approved cap of
    # 80, so a plan priced for 80 attempts would have stopped at 60 under a
    # limit nobody wrote, reported as an exhausted budget.
    cap = selected_generation_attempt_cap(environ)

    # Two protocols, two names — see `countcli.main`. The provider one takes
    # `(method, url, headers, body)`; `urlopen` does not, and passing it here
    # was the same defect that stopped the count job at
    # TOKEN_COUNT_RETRY_EXHAUSTED. Generation carries the larger response bound.
    #
    # NOT the bare `urlopen`. It follows redirects and re-sends the headers on
    # the Request — including `Authorization` — so a 302 moves the GitHub token
    # wherever the response says. `releaseasset` already refuses that on its own
    # reads; the status publisher and the review publisher were still using the
    # default, and adversarial review pointed out that fixing one of three call
    # sites for the same defect is worse than knowing about none of them.
    #
    # Wired HERE, once, so both publishers get it from the same place.
    github_status_opener = github_api_opener()
    prepared = panel_execution(
        environ, mode=MODE_PROVIDER,
        transport_factory=lambda engine, *, engine_source_sha256:
            live_generation_transport(
                engine, opener=provider_http_opener(
                    capability="GENERATION_TRANSPORT"), key=key,
                generation_attempt_cap=cap,
                engine_source_sha256=engine_source_sha256))
    perform(environ, execute_fn=prepared["execute_fn"],
            opener=github_status_opener,
            panel_identity=prepared["identity_binding"],
            require_identity=prepared["require_identity"],
            scan=prepared["scan"],
            publish_review_fn=_live_review_publisher(github_status_opener))


def github_api_opener():
    """An opener that refuses to carry the token off api.github.com.

    A redirect is a request to send the bearer token somewhere else. Every URL
    this package builds is pinned to `api.github.com` and a numeric repository
    id, and that pinning means nothing if the response can move the request.
    There is no legitimate redirect on these endpoints, so this refuses rather
    than allowlisting hosts — an allowlist is a thing that has to be kept
    correct, and a refusal is not."""
    import urllib.request

    class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            refuse(f"category=github_api_redirect_refused http_status={code} "
                   "— the response asked this request to go somewhere else and "
                   "it carries a bearer token. Checking the final URL after the "
                   "fact is too late: urllib has already re-sent the header")

    return urllib.request.build_opener(_RefuseRedirects).open


def _live_review_publisher(opener):
    """The real comment publisher, bound to the GitHub opener.

    A separate function so that the two `perform` call sites differ in exactly
    one visible way, and so a test can assert that the provider path is wired to
    `reviewpublish.publish_review` and the dry-run path is not."""
    from . import reviewpublish
    return lambda *, body, pr_number, candidate_head_sha, token, challenge: (
        reviewpublish.publish_review(
            body=body, pr_number=pr_number,
            candidate_head_sha=candidate_head_sha, token=token, opener=opener,
            challenge=challenge))


def _dry_run_review_publisher():
    from . import reviewpublish
    return lambda *, body, pr_number, candidate_head_sha, token, challenge: (
        reviewpublish.not_published_in_a_dry_run(
            body=body, candidate_head_sha=candidate_head_sha))


def _self_test() -> int:
    from . import reviewpublish, reviewrender
    return self_test_report("panelcli", {
        "module imports": True,
        "perform is callable": callable(perform),
        "reads the same-run artifact directory":
            INPUT_DIRNAME in input_paths("/x")["plan"],
        "panel evidence class is the mid-term one":
            PANEL_EVIDENCE_CLASS.startswith("MIDTERM_"),
        "retains the readable review beside the evidence":
            review_paths("/x")["markdown"].endswith("panel-review.md"),
        "the review publisher reaches only the pinned repository":
            reviewpublish.comments_url(1).startswith(
                f"https://api.github.com/repositories/{REPOSITORY_NUMERIC_ID}/"),
        "the renderer has one sticky marker":
            reviewrender.MARKER.startswith("<!--"),
    })


if __name__ == "__main__":
    if self_test_requested(sys.argv[1:]):
        raise SystemExit(_self_test())
    raise SystemExit(run(main, name="panelcli"))
