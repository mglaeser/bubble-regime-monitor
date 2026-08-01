"""Third-party Actions must be pinned to immutable commits.

`uses: actions/checkout@v4` is not a version. It is a request for whatever the
tag points at when the runner resolves it, and the person who can move that tag
is not the person who owns this repository. In a lane whose entire job is
deciding what may run with a credential, a moving tag is the supply chain
deciding instead.

So every `uses:` in a trusted-lane workflow must be a 40-hex commit SHA, and
that SHA must appear below with the release it was verified against. The
mapping is not decoration: `assert_pinned` refuses a SHA it does not know, which
means adding a new action is a deliberate edit here rather than a silent
addition in a workflow file.

How the mapping was established (reproducible by anyone, no credential):

    git ls-remote https://github.com/actions/checkout     refs/tags/v4
    git ls-remote https://github.com/actions/checkout     refs/tags/v4.4.0
    git ls-remote https://github.com/actions/setup-python refs/tags/v5
    git ls-remote https://github.com/actions/setup-python refs/tags/v5.6.0

Each moving tag currently resolves to the same commit as a concrete release
tag, so the SHA is tied to a named release rather than to a snapshot of a
branch. That is the check RELEASE_EXECUTION_CONTRACT_v1 §12 asks for, and it is
weaker than it sounds: it proves the SHA carried that release tag when it was
resolved, not that the release was reviewed. Reviewing upstream source remains
an operator act.
"""

from __future__ import annotations

import re

from .errors import refuse

_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")

#: action name -> {sha: release}. Additions are deliberate edits.
#:
#: The allowlist pragmas are the same call as for a commit SHA elsewhere in this
#: repository: a 40-hex upstream commit id is a public git identity, and the
#: secret gate flags every 40-hex literal on principle. Saying which it is here
#: is the point of the pragma.
APPROVED_ACTION_PINS = {
    "actions/checkout": {
        "11d5960a326750d5838078e36cf38b85af677262": "v4.4.0",  # pragma: allowlist secret
    },
    "actions/setup-python": {
        "a26af69be951a213d495a4c3e4e4022e16d87065": "v5.6.0",  # pragma: allowlist secret
    },
    # R06. D1 retains its two signed documents as private artifacts and D2
    # downloads them BY RUN ID. Adding two actions to a credential-bearing lane
    # is a deliberate decision, which is why it is an edit here: both SHAs were
    # resolved with `git ls-remote` and each carried a concrete release tag as
    # well as its moving one, per PIN_VERIFICATION_METHOD below.
    "actions/upload-artifact": {
        "ea165f8d65b6e75b540449e92b4886f43607fa02": "v4.6.2",  # pragma: allowlist secret
    },
    "actions/download-artifact": {
        "634f93cb2916e3fdff6788551b99b062d0335ce0": "v5.0.0",  # pragma: allowlist secret
    },
}

#: How the mapping above was verified, recorded so the claim is checkable.
PIN_VERIFICATION_METHOD = "git ls-remote upstream refs/tags/<moving> and <release>"
PIN_VERIFICATION_SCOPE = (
    "the SHA carried both the moving tag and a concrete release tag when "
    "resolved; upstream source review is an operator act, not this check"
)


def parse_uses(value: str) -> tuple[str, str]:
    """Split `owner/repo@ref` into (name, ref). Local/docker uses are refused.

    A `./path` composite action is a local file, and in a lane that must not
    execute candidate content that is exactly the wrong kind of indirection —
    the file could be added by the same change that references it."""
    if not isinstance(value, str) or not value:
        refuse("category=action_uses_empty")
    if value.startswith("./") or value.startswith("docker://"):
        refuse(f"category=action_uses_not_a_pinned_repository value={value!r} "
               "— a local composite or docker action is not pinnable to an "
               "upstream commit this policy can verify")
    if "@" not in value:
        refuse(f"category=action_uses_unpinned value={value!r}")
    name, _, ref = value.partition("@")
    return name, ref


def assert_pinned(value: str, *, where: str = "") -> dict:
    """Refuse anything that is not an approved immutable pin."""
    name, ref = parse_uses(value)
    if not _SHA1.match(ref):
        refuse(f"category=action_not_pinned_to_sha where={where} "
               f"action={name} ref={ref!r} — a tag is whatever its owner points "
               "it at; pin the commit")
    known = APPROVED_ACTION_PINS.get(name)
    if known is None:
        refuse(f"category=action_not_in_policy where={where} action={name} — "
               "adding an action to a credential-bearing lane is a deliberate "
               "decision, so it is an edit to actionpolicy.py")
    if ref not in known:
        refuse(f"category=action_sha_not_approved where={where} action={name} "
               f"sha={ref[:12]}… approved={[s[:12] for s in known]}")
    return {"action": name, "sha": ref, "release": known[ref]}


def pin_record() -> dict:
    """The mapping, as evidence, with its own honest scope."""
    return {
        "pins": {name: dict(shas) for name, shas in APPROVED_ACTION_PINS.items()},
        "verification_method": PIN_VERIFICATION_METHOD,
        "honest_scope": PIN_VERIFICATION_SCOPE,
    }
