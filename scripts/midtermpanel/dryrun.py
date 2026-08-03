"""The no-key dry run: the same code path, with nothing that can spend or write.

## Why this is a mode and not a test harness

Phase A has to answer two questions that a unit test cannot: does the whole
vertical run, and does it run without a credential. A harness that imports the
pieces and wires them together answers a third question instead — does MY wiring
work — and the wiring under review is the workflow's.

So the dry run is a mode of the real entry points. `python -m
midtermpanel.countcli` with `MIDTERM_PANEL_MODE=DRY_RUN_NO_PROVIDER` runs the
same `count_through_engine` the provider path runs, loads the same engine, calls
the same `prepare_review_plan_core`, and differs in exactly two objects: the
transport is the engine's own labelled stand-in, and the status opener writes to
a file instead of a socket.

## Why the engine can only be loaded once per process

`enginebridge.load_engine` puts the artifact root on `sys.path` and imports
`verifier.*` under their real names, then proves each module's `__file__` lives
under that root. That is the check that keeps a candidate checkout from
supplying the reviewer, and it means a second engine in the same process is
refused — `import verifier.authority` returns the first one, and the origin
check correctly says so.

Which is why the vertical runs count and panel as SEPARATE PROCESSES, the way
the workflow runs them as separate jobs. A vertical that loaded both in one
process would have had to weaken that check to pass, and the check is more
valuable than the convenience.

## The sink is not a mock of GitHub

It records the exact request this lane would have sent — URL, method, and the
body's own fields — and returns a 201. It does not model GitHub's behaviour,
because nothing here depends on GitHub's behaviour beyond the status code. What
it proves is that the status calls were CONSTRUCTED, in order, with the states
the run reached; `status.publish` still validates every request before the sink
ever sees it.
"""

from __future__ import annotations

import json
import os

from .errors import refuse

MODE_ENV = "MIDTERM_PANEL_MODE"
STATUS_SINK_ENV = "MIDTERM_STATUS_SINK_PATH"

#: The one value that selects a dry run. Spelled the same as
#: `engine.MODE_DRY_RUN` so the string in the workflow and the string the gate
#: compares are one string.
DRY_RUN = "DRY_RUN_NO_PROVIDER"


def is_dry_run(environ: dict) -> bool:
    """Explicit opt-in only. An unset variable is a provider-backed run.

    Deliberately not "dry run unless a key is present". That rule would make a
    misconfigured secret silently downgrade a real review into a stand-in one
    that publishes green — the inactive-check defect, reached from the
    direction where it is hardest to notice."""
    value = str(environ.get(MODE_ENV) or "").strip()
    if not value:
        return False
    if value != DRY_RUN:
        refuse(f"category=panel_mode_unknown mode={value!r} "
               f"permitted=['{DRY_RUN}'] — the mode selects whether this run "
               "can spend; an unrecognised one is not a reason to guess")
    return True


def assert_no_credential_is_present(environ: dict) -> dict:
    """A dry run must not even be holding a key.

    The transport gate already refuses a live transport, but a dry run in a job
    where the secret is in scope is a dry run one edit away from spending, and
    the point of the hosted no-key run is to prove the secret was never there.

    Both names are checked: the lane's own variable and the trusted-lane secret
    name, because a workflow that mapped the secret onto the wrong variable
    would still have put it in the process environment."""
    from . import SECRET_NAME
    from .transport import PROVIDER_KEY_ENV
    present = [name for name in (PROVIDER_KEY_ENV, SECRET_NAME)
               if str(environ.get(name) or "").strip()]
    if present:
        refuse(f"category=dry_run_holds_a_credential variables={present} — a "
               "dry run proves this lane works without spending, and a run "
               "holding a key proves it one branch shallower than that")
    return {"credential_variables_checked": [PROVIDER_KEY_ENV, SECRET_NAME],
            "credential_present": False}


class StatusSink:
    """An opener that records what would have been sent. Opens nothing.

    Same shape as `urllib.request.urlopen` as `status.publish` uses it: called
    with a `Request`, used as a context manager, and asked for `.status`."""

    def __init__(self, path: str):
        self.path = path
        self.calls = []
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def __call__(self, request, timeout=None):
        body = request.data or b"{}"
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            refuse("category=status_body_not_json — the sink records what "
                   "would have been sent, and an unreadable body is not a "
                   "record of anything")
        # The Authorization header is deliberately NOT recorded. This file is
        # written for a human to read and may be uploaded as an artifact; a
        # sink that captured headers would capture a bearer token the first
        # time it ran anywhere real.
        self.calls.append({"url": request.full_url,
                           "method": request.get_method(),
                           "state": parsed.get("state"),
                           "context": parsed.get("context"),
                           "description": parsed.get("description")})
        self._flush()
        return _Accepted()

    def _flush(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"status_calls": self.calls,
                       "count": len(self.calls),
                       "honest_scope": (
                           "these statuses were CONSTRUCTED and validated by "
                           "`status.publish`; none was sent. No socket was "
                           "opened and no check appears on any commit")},
                      handle, indent=2, sort_keys=True)
            handle.write("\n")

    def states(self) -> list:
        return [call["state"] for call in self.calls]

    def contexts(self) -> list:
        return sorted({call["context"] for call in self.calls})


class _Accepted:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def status_sink(environ: dict) -> StatusSink:
    """The sink, at the operator-named path. Refuses to guess one."""
    path = str(environ.get(STATUS_SINK_ENV) or "").strip()
    if not path:
        refuse(f"category=dry_run_status_sink_not_configured "
               f"variable={STATUS_SINK_ENV} — a dry run must record what it "
               "would have published; a run that recorded nothing could not be "
               "distinguished from one that published nothing")
    return StatusSink(path)


def count_transport_factory(environ: dict):
    """The engine's own labelled count stand-in, restricted to the count path."""
    from .transport import engine_stand_in_count_transport
    del environ
    return lambda engine: engine_stand_in_count_transport(engine)


def generation_transport_factory(environ: dict):
    """The engine's own labelled generation stand-in.

    `refute` and `identical_reasons` are read from the environment so the
    vertical can drive the veto path and the anti-canned tripwire through the
    REAL entry points rather than by calling `aggregate` directly. Both are
    dry-run only; `is_dry_run` is what gates reaching this function at all."""
    from .transport import engine_stand_in_generation_transport
    refute = {}
    raw = str(environ.get("MIDTERM_DRY_RUN_REFUTE") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            refuse("category=dry_run_refute_not_json — the refutation map is "
                   "{model_id: [unit_sha256, ...]}")
        if not isinstance(parsed, dict):
            refuse("category=dry_run_refute_not_an_object")
        refute = {model: set(units) for model, units in parsed.items()}
    identical = str(
        environ.get("MIDTERM_DRY_RUN_IDENTICAL_REASONS") or "").strip() == "1"
    return lambda engine: engine_stand_in_generation_transport(
        engine, refute=refute, identical_reasons=identical)


def record(environ: dict, *, sink: StatusSink, transport) -> dict:
    """What the dry run proved, in the four honesty fields' spirit."""
    from .transport import SOURCE_MOCK
    # NOT `assert_no_provider_calls`. That function proves a transport made no
    # calls, and the stand-in makes plenty — three counts, one per model. Asking
    # it here reported `provider_calls: 3` for a run in which no provider was
    # reached at all, which is a false statement in the direction that matters.
    #
    # What is actually true, and what is asserted instead: every call went
    # through an object that DECLARES `MOCK_NOT_PROVIDER`, which is the label
    # the engine itself uses to decide that a result may never be represented
    # as provider evidence.
    declared = getattr(transport, "source", None)
    if declared != SOURCE_MOCK:
        refuse(f"category=dry_run_transport_not_declared_mock "
               f"source={declared!r} — a dry run's evidence is only honest "
               "because the engine can see that nothing it received came from "
               "a provider")
    return {
        "mode": DRY_RUN,
        "credential": assert_no_credential_is_present(environ),
        "stand_in_transport": getattr(transport, "kind", None),
        "stand_in_calls": getattr(transport, "call_count", None),
        "stand_in_paths": (transport.paths_called()
                           if hasattr(transport, "paths_called") else None),
        "declared_source": declared,
        "provider_calls": 0,
        "statuses_constructed": sink.states(),
        "status_contexts": sink.contexts(),
        "honest_scope": (
            "the whole vertical ran: the approved engine was built and loaded, "
            "the engine counted every governed model and batch, the plan was "
            "written and re-read, and the panel executed and aggregated. Every "
            "verdict came from the engine's own labelled stand-in and is "
            "MOCK_NOT_PROVIDER. No provider was called, no credential was "
            "present, and no status was published. This proves the LANE works; "
            "it says nothing about what three real models would say"),
    }
