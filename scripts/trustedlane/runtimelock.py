"""The D1/D2 runtime dependency lock — hashed, or refused.

EX3-R06. The engine candidate said, out loud:

    pinned_to_versions: false
    pinned_to_hashes:   false

Honest, and not good enough for a credential-bearing trust root. A D1/D2 runner
that resolves `PyYAML` by name at start-up installs whatever the index serves
that morning, and it does so on a machine that is about to hold a provider key.

Two separate corrections, and the first is the one that shrinks the problem.

**The runtime does not need pytest.** The D0/D1/D2 workflows installed `pytest`
and `PyYAML` together because D0 runs the containment suite. D1 and D2 do not
run tests; they count and generate. An AST sweep over `scripts/trustedlane/`
finds exactly one third-party import — `yaml` — and no runtime module imports
pytest. So the credential-bearing runtime installs one package, not two, and the
attack surface of the lock is one line.

**Every requirement must carry a hash.** `parse_lock` refuses a requirement with
no `--hash`, refuses an unpinned version, and refuses an unknown package. It
does not warn: an unhashed dependency in a trust root is the supply chain, and a
warning is a thing people read once.

What this module cannot do from here: resolve the upstream hash. That needs an
index fetch, and the mandate places it correctly — "a no-secret protected build
may resolve/download dependencies before D1/D2 activation". So the lock file is
the artifact, `parse_lock` is the gate, and a lock whose hashes have not been
resolved yet is refused rather than treated as good enough.
"""

from __future__ import annotations

import hashlib
import json
import re

from .errors import refuse

LOCK_PATH = "requirements-trusted-runtime.txt"

#: Packages the credential-bearing runtime may install. Deliberately not the
#: same list as the test environment: pytest is absent because no runtime module
#: imports it, and every extra package is another thing that runs on a machine
#: holding a provider key.
PERMITTED_RUNTIME_PACKAGES = ("PyYAML",)

#: Hash algorithms pip accepts that are worth accepting. `md5` and `sha1` are
#: refused by name rather than merely unlisted, so a lock using one gets an
#: explanation instead of "unknown algorithm".
PERMITTED_HASH_ALGORITHMS = ("sha256", "sha384", "sha512")
REFUSED_HASH_ALGORITHMS = {
    "md5": "collision-broken; two different wheels can share one",
    "sha1": "collision-broken since 2017",
}

_REQUIREMENT = re.compile(r"\A(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;]+)\Z")
_HASH = re.compile(r"\A--hash=(?P<algorithm>[a-z0-9]+):(?P<digest>[0-9a-f]+)\Z")


def _normalise(name: str) -> str:
    """PEP 503 normalisation: `PyYAML`, `pyyaml` and `py_yaml` are one package.

    Without this, a lock could list `PyYAML` while an install resolved `pyyaml`
    from a different index entry and the allowlist would never notice."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str, *, path: str = LOCK_PATH) -> dict:
    """Parse a pip requirements lock, refusing anything not fully pinned.

    Continuation lines (`\\`) are joined first, because pip's own format puts
    the hashes on continuation lines and a parser that reads line-by-line would
    see a pinned requirement with no hashes followed by orphan hash lines — and
    would most likely conclude the requirement was unhashed *or* that the hashes
    belonged to nothing. Either reading is wrong in a different direction."""
    if not isinstance(text, str):
        refuse(f"category=lock_not_text path={path}")
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    requirements = {}
    for lineno, raw in enumerate(joined.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        head, rest = tokens[0], tokens[1:]
        match = _REQUIREMENT.match(head)
        if not match:
            refuse(f"category=lock_requirement_not_pinned path={path} "
                   f"line={lineno} token={head!r} — a requirement without "
                   "`==version` installs whatever the index serves that "
                   "morning, on a machine about to hold a provider key")
        name = _normalise(match.group("name"))
        if name not in {_normalise(p) for p in PERMITTED_RUNTIME_PACKAGES}:
            refuse(f"category=lock_package_not_permitted path={path} "
                   f"package={match.group('name')!r} "
                   f"permitted={list(PERMITTED_RUNTIME_PACKAGES)} — the "
                   "credential-bearing runtime installs only what it imports")
        hashes = []
        for token in rest:
            hash_match = _HASH.match(token)
            if not hash_match:
                refuse(f"category=lock_token_unrecognised path={path} "
                       f"line={lineno} token={token!r}")
            algorithm = hash_match.group("algorithm")
            if algorithm in REFUSED_HASH_ALGORITHMS:
                refuse(f"category=lock_hash_algorithm_broken path={path} "
                       f"algorithm={algorithm} — "
                       f"{REFUSED_HASH_ALGORITHMS[algorithm]}")
            if algorithm not in PERMITTED_HASH_ALGORITHMS:
                refuse(f"category=lock_hash_algorithm_not_permitted "
                       f"path={path} algorithm={algorithm}")
            hashes.append({"algorithm": algorithm,
                           "digest": hash_match.group("digest")})
        if not hashes:
            refuse(f"category=lock_requirement_unhashed path={path} "
                   f"package={match.group('name')} version={match.group('version')} "
                   "— an unhashed dependency IS the supply chain; a pinned "
                   "version still lets a compromised index serve different bytes "
                   "under the same number")
        if name in requirements:
            refuse(f"category=lock_package_listed_twice path={path} "
                   f"package={name} — two entries means one of them is the one "
                   "pip uses and nothing here says which")
        requirements[name] = {
            "name": match.group("name"),
            "version": match.group("version"),
            "hashes": sorted(hashes, key=lambda h: (h["algorithm"], h["digest"])),
        }
    if not requirements:
        refuse(f"category=lock_is_empty path={path} — an empty lock is not a "
               "lock; it permits everything by specifying nothing")
    return requirements


def lock_digest(requirements: dict) -> str:
    payload = {name: {"version": entry["version"], "hashes": entry["hashes"]}
               for name, entry in sorted(requirements.items())}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"trusted-runtime-lock-v1\x00" + blob).hexdigest()


def assert_no_test_dependency(requirements: dict) -> dict:
    """The credential-bearing runtime must not install a test framework.

    Separate from the allowlist because it is a different claim: the allowlist
    says what MAY be installed, this says the runtime was not quietly widened to
    include the tooling D0 needs. A pytest in a D1 runner is a plugin loader, a
    conftest search and an entry-point scan, all on a machine holding a key."""
    banned = {"pytest", "pytest-cov", "tox", "nose"}
    present = sorted(n for n in requirements if _normalise(n) in banned)
    if present:
        refuse(f"category=runtime_lock_contains_test_dependency "
               f"packages={present} — D1/D2 count and generate; they run no "
               "tests. A test framework in a credential-bearing runner is a "
               "plugin loader and an entry-point scan next to a provider key")
    return {"runtime_packages": sorted(requirements), "test_frameworks": 0}


def load_lock(*, root: str = ".") -> dict:
    import os

    path = os.path.join(root, LOCK_PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        refuse(f"category=runtime_lock_missing path={LOCK_PATH} "
               f"exception_class={type(exc).__name__} — the credential-bearing "
               "runtime has no dependency lock, so it resolves by name")
    requirements = parse_lock(text, path=LOCK_PATH)
    assert_no_test_dependency(requirements)
    return {
        "path": LOCK_PATH,
        "requirements": requirements,
        "lock_sha256": lock_digest(requirements),
        "package_count": len(requirements),
        "honest_scope": ("every requirement is version-pinned and carries a "
                         "hash of a permitted algorithm. Whether those hashes "
                         "match what the index actually serves is proved by "
                         "`pip install --require-hashes` at install time, not "
                         "by this parse"),
    }
