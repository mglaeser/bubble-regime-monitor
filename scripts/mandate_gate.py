#!/usr/bin/env python3
"""The mandate gate (§9.10 of governance/mandate/part1.md) — the machine that
keeps the due-diligence mandate true after every engagement ends.

Runs in CI on every change (blocking) and on the weekly cadence. Subcommands:

  status     Validate audit/03-findings.json against audit/00-check-catalogue.json,
             recompute audit/engagement-status.json, fail closed on drift, on any
             PASS with a null standing_control, on any open blocker-band finding
             not in the accepted-residuals register, and on governance hash
             mismatches. `--write` regenerates the status file (repair lane).
  ratchet    Enforce audit/ratchet-baselines.json: floors may not fall, ceilings
             may not rise. `--measure` prints current values without enforcing.
  calibrate  S12 seeded-defect calibration: re-prove, with scratch files, that
             the gates still catch each class in the calibration corpus, and
             that the two NOT-APPLICABLE classes are still structurally absent.
  surface    Regenerate audit/00-audit-surface.json (the audit denominator).
  all        status + ratchet + calibrate (the CI entry point).

Design rules: stdlib only (subprocess for the real gate tools), deterministic,
fail-closed — a check that cannot run is a failed check, not a skipped one.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "audit"
GOV = ROOT / "governance"

BLOCKER_BANDS = ("STOP-SHIP", "BLOCKER-1", "BLOCKER-2")
ALL_BANDS = BLOCKER_BANDS + ("MUST-FIX", "SHOULD-FIX", "PLAN", "ASSESS")
OPEN_VERDICTS = ("FAIL", "PARTIAL", "NO-EVIDENCE")
# §5: the ONLY legal verdicts. Anything else fails the build (adversarial
# audit 2026-07-25, critical: there was no whitelist, so "Fail"/null/"WAIVED"
# fell out of the open-blocker loop, the PASS-control check and the N/A check
# simultaneously — a one-character edit made a STOP-SHIP invisible).
CANONICAL_VERDICTS = ("PASS", "FAIL", "PARTIAL", "NO-EVIDENCE", "NOT-APPLICABLE")


def effective_band(finding: dict) -> str:
    """Escalations applied, fail-closed (2026-07-25 audit finding: the
    free-text escalated_band 'STOP-SHIP (A-01+A-39 both fail)' matched no
    band constant, so an open FAIL silently escaped the blocker gate). A
    band value must START WITH a known band token; anything unparseable is
    a gate failure, never a silently ignored record."""
    raw = finding.get("escalated_band") or finding["band"]
    for band in ALL_BANDS:
        if str(raw).startswith(band):
            return band
    _fail(f"{finding['id']} has unparseable band {raw!r} — a record the "
          "gate cannot band is a record it cannot gate")
    raise AssertionError  # unreachable; _fail exits


def _fail(msg: str) -> None:
    print(f"MANDATE-GATE FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        _fail(f"required artifact missing: {path.relative_to(ROOT)}")
    except json.JSONDecodeError as exc:
        _fail(f"unparseable artifact {path.relative_to(ROOT)}: {exc}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- status ----


def compute_status() -> dict:
    catalogue = _load(AUDIT / "00-check-catalogue.json")
    findings = _load(AUDIT / "03-findings.json")
    accepted = _load(GOV / "accepted-residuals.json")
    manifest = _load(GOV / "mandate" / "manifest.json")

    cat_ids = [c["id"] for c in catalogue["checks"]]
    if len(cat_ids) != len(set(cat_ids)):
        _fail("check catalogue contains duplicate ids")
    if catalogue["registered_check_count"] != len(cat_ids):
        _fail("catalogue registered_check_count does not match its own checks")

    # The audit denominator is pinned by the manifest, not by the catalogue's
    # own self-report (adversarial audit 2026-07-25: catalogue+findings could
    # be shrunk as a matched pair to drop checks from the universe).
    required = manifest.get("required_check_ids")
    if not isinstance(required, list) or not required:
        _fail("governance/mandate/manifest.json has no required_check_ids — "
              "the audit denominator must be pinned, not inferred")
    if set(required) != set(cat_ids):
        missing = sorted(set(required) - set(cat_ids))
        extra = sorted(set(cat_ids) - set(required))
        _fail(f"catalogue does not match the manifest's pinned check universe "
              f"— missing: {missing} extra: {extra} (founding 119 are "
              "immutable; additions are Article XIII amendments)")

    f_ids = [f["id"] for f in findings]
    if len(f_ids) != len(set(f_ids)):
        _fail("findings contain duplicate ids")
    if set(f_ids) != set(cat_ids):
        missing = sorted(set(cat_ids) - set(f_ids))
        extra = sorted(set(f_ids) - set(cat_ids))
        _fail(f"findings/catalogue mismatch — missing: {missing} extra: {extra}")

    for f in findings:
        if f.get("verdict") not in CANONICAL_VERDICTS:
            _fail(f"{f.get('id', '<no id>')} has non-canonical verdict "
                  f"{f.get('verdict')!r} — legal values are "
                  f"{CANONICAL_VERDICTS}. A verdict the gate cannot read is a "
                  "finding it cannot gate (adversarial audit 2026-07-25).")
        if not isinstance(f.get("band"), str):
            _fail(f"{f.get('id', '<no id>')} has no string `band`")

    # §3/§5: a PASS with no standing control is PARTIAL, and per §5 the
    # control must be structured with a non-null `demonstrated` — a control
    # nobody has watched block something is a control being hoped about.
    for f in findings:
        if f["verdict"] == "PASS":
            sc = f.get("standing_control")

            def _substantive(v) -> bool:
                # bools/ints/lists are not descriptions (adversarial audit
                # 2026-07-25: {"mechanism": true} satisfied a truthiness test)
                return isinstance(v, str) and len(v.strip()) >= 12

            if not isinstance(sc, dict) or not _substantive(sc.get("mechanism")) \
                    or not _substantive(sc.get("demonstrated")):
                _fail(f"{f['id']} is PASS without a structured "
                      "standing_control carrying non-null mechanism and "
                      "demonstrated — per the mandate that verdict is "
                      "PARTIAL, always (§5)")
        if f["verdict"] == "NOT-APPLICABLE" and not f.get("na_justification"):
            _fail(f"{f['id']} is NOT-APPLICABLE without na_justification (§5)")

    open_blockers: list[str] = []
    band_counts = {b: 0 for b in BLOCKER_BANDS}
    for f in findings:
        band = effective_band(f)
        if f["verdict"] in OPEN_VERDICTS and band in BLOCKER_BANDS:
            open_blockers.append(f["id"])
            band_counts[band] += 1

    accepted_ids = set(accepted["accepted_open_findings"])
    unaccepted = sorted(set(open_blockers) - accepted_ids)
    if unaccepted:
        _fail(f"open blocker-band findings NOT in the accepted-residuals "
              f"register: {unaccepted} — a NEW blocker cannot be waved "
              "through; fix it or the operator must accept it by decision "
              "record in governance/accepted-residuals.json")
    stale = sorted(accepted_ids - set(open_blockers))
    if stale:
        _fail(f"accepted-residuals register lists findings that are no longer "
              f"open blockers: {stale} — prune the register (a closed "
              "acceptance left in place is a future bypass)")

    # Governance hash attestation (Article XI / B-35). The attested set
    # includes the gate's own authority files — the ratchet baselines and the
    # accepted-residuals register — because a register the measured thing can
    # hand-edit is a diary, not a gate (§9.8; tamper-test finding 2026-07-25).
    const_path = GOV / "constitution.md"
    const_hash = _sha256(const_path)
    if manifest["constitution_sha256"] != const_hash:
        _fail("constitution.md hash does not match governance/mandate/"
              "manifest.json — amendments must go through the gate and "
              "bump the attested hash (Article XIII)")
    attested = {
        "findings_sha256": AUDIT / "03-findings.json",
        "check_catalogue_sha256": AUDIT / "00-check-catalogue.json",
        "part1_sha256": GOV / "mandate" / "part1.md",
        "combined_mandate_sha256": GOV.parent / "governance" / "mandate.md",
        "ratchet_baselines_sha256": AUDIT / "ratchet-baselines.json",
        "accepted_residuals_sha256": GOV / "accepted-residuals.json",
    }
    for key, path in attested.items():
        if manifest.get(key) != _sha256(path):
            _fail(f"{path.relative_to(ROOT)} hash mismatch vs manifest — "
                  "changing this file is an Article XIII amendment: update "
                  f"{key} in governance/mandate/manifest.json in the same "
                  "change (loosening anything is automatically a finding)")

    evidenced = sum(1 for f in findings if f["verdict"] != "NO-EVIDENCE")
    pending = sorted(f["id"] for f in findings if f["verdict"] == "NO-EVIDENCE")

    def _volume_complete(tracks: tuple[str, ...]) -> str:
        # Computed, never asserted (§8): a volume is COMPLETE when every one
        # of its checks carries an evidence-backed verdict. Open accepted
        # residuals affect production_eligible, not phase completion — the §8
        # eligibility chain requires BOTH separately, so the states are
        # deliberately distinct.
        vol = [f for f in findings if f["id"].split("-")[0] in tracks]
        return ("COMPLETE" if all(f["verdict"] != "NO-EVIDENCE" for f in vol)
                else "IN_PROGRESS")

    production_eligible = (
        not open_blockers
        and evidenced == len(cat_ids)
        and accepted.get("constitution_state") == "RATIFIED"
    )
    return {
        "catalogue_version": catalogue["catalogue_version"],
        "registered_check_count": len(cat_ids),
        "active_check_count": len(cat_ids),
        "present_check_count": len(f_ids),
        "evidenced_check_count": evidenced,
        "pending_check_ids": pending,
        "part1_status": _volume_complete(("A", "B")),
        "part2_status": _volume_complete(("C",)),
        "phase": "STANDING_REGIME",
        "highest_open_band": next((b for b in BLOCKER_BANDS
                                   if band_counts[b]), "MUST-FIX-OR-BELOW"),
        "open_stop_ship_count": band_counts["STOP-SHIP"],
        "open_blocker_1_count": band_counts["BLOCKER-1"],
        "open_blocker_2_count": band_counts["BLOCKER-2"],
        "open_blocker_ids_accepted_by_operator": sorted(open_blockers),
        "security_scope_audited": True,
        "constitution_state": accepted.get("constitution_state"),
        "production_eligible": production_eligible,
        "production_note": accepted.get("production_note", ""),
        "mandate_manifest_hash": _sha256(GOV / "mandate" / "manifest.json"),
        "constitution_hash": const_hash,
    }


def cmd_status(write: bool) -> None:
    status = compute_status()
    out = AUDIT / "engagement-status.json"
    rendered = json.dumps(status, indent=2, sort_keys=True) + "\n"
    if write:
        out.write_text(rendered)
        print(f"wrote {out.relative_to(ROOT)}")
        return
    if not out.exists():
        _fail("audit/engagement-status.json missing — run "
              "`mandate_gate.py status --write` and commit it")
    if out.read_text() != rendered:
        _fail("audit/engagement-status.json drifted from the computed state — "
              "regenerate with `status --write` and commit (the file is a "
              "computed property, never hand-edited)")
    print("status: OK — engagement state consistent; "
          f"production_eligible={status['production_eligible']} "
          f"(open blockers, operator-accepted: "
          f"{len(status['open_blocker_ids_accepted_by_operator'])})")


# --------------------------------------------------------------- ratchet ----


def _count_grep(pattern: str, globs: list[str]) -> int:
    n = 0
    rx = re.compile(pattern)
    for g in globs:
        for p in ROOT.glob(g):
            if p.is_file():
                for line in p.read_text(errors="replace").splitlines():
                    if rx.search(line):
                        n += 1
    return n


_EMOJI_RX = re.compile("[\\U0001f300-\\U0001faff\\u2600-\\u27bf]")


def _emoji_count() -> int:
    # Article XIV alarm dilution: emojis are reserved for constitutional
    # alerts; any emoji in source dilutes the only unmissable signal.
    n = 0
    for g in ("app/**/*.py", "scripts/*.py", "tests/**/*.py"):
        for p in ROOT.glob(g):
            n += len(_EMOJI_RX.findall(p.read_text(errors="replace")))
    return n


def measure_ratchets() -> dict[str, int]:
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True)
    # Panel finding (PR #23, first run that could actually SEE this file): the
    # return code was ignored, so a suite with collection/import errors still
    # printed "N tests collected" and greened the ratchet whenever N happened
    # to reach the floor — a broken suite reading as a healthy one.
    if collected.returncode != 0:
        _fail("pytest collection FAILED (rc="
              f"{collected.returncode}) — a suite that cannot be collected "
              "cannot measure a floor: "
              + (collected.stdout or collected.stderr or "")[-600:])
    if re.search(r"error(s)? during collection|^ERROR ", collected.stdout,
                 re.MULTILINE | re.IGNORECASE):
        _fail("pytest reported collection errors — refusing to measure a "
              "ratchet from a partially-collected suite")
    m = re.search(r"(\d+) tests? collected", collected.stdout)
    if not m:
        _fail("could not measure collected test count (pytest --collect-only)")
    return {
        "test_count_floor": int(m.group(1)),
        "noqa_ceiling": _count_grep(r"#\s*noqa", ["app/**/*.py", "scripts/*.py",
                                                  "tests/**/*.py"]),
        "type_ignore_ceiling": _count_grep(r"#\s*type:\s*ignore",
                                           ["app/**/*.py", "scripts/*.py"]),
        "emoji_in_source_ceiling": _emoji_count(),
    }


def cmd_ratchet(measure_only: bool) -> None:
    current = measure_ratchets()
    if measure_only:
        print(json.dumps(current, indent=2))
        return
    # The baseline file is attested (tamper-test finding 2026-07-25: a
    # hand-loosened floor passed silently). Verify even when ratchet runs
    # standalone, not only via status.
    manifest = _load(GOV / "mandate" / "manifest.json")
    if manifest.get("ratchet_baselines_sha256") != _sha256(
            AUDIT / "ratchet-baselines.json"):
        _fail("audit/ratchet-baselines.json hash mismatch vs manifest — "
              "baseline changes are Article XIII amendments (update "
              "ratchet_baselines_sha256 in the same change; loosening is "
              "automatically a finding)")
    baselines = _load(AUDIT / "ratchet-baselines.json")
    errors = []
    for name, base in baselines["ratchets"].items():
        cur = current.get(name)
        if cur is None:
            errors.append(f"ratchet {name} has a baseline but no measurement")
        elif name.endswith("_floor") and cur < base:
            errors.append(f"{name}: {cur} < floor {base} (may not fall)")
        elif name.endswith("_ceiling") and cur > base:
            errors.append(f"{name}: {cur} > ceiling {base} (may not rise)")
    if errors:
        _fail("ratchet regression — " + "; ".join(errors) +
              ". Tightening is a normal change; loosening requires a decision "
              "record in the baseline file AND is automatically a finding "
              "(§9.1). Floors that improved should be re-baselined upward.")
    print(f"ratchet: OK — {len(baselines['ratchets'])} ratchets hold "
          f"(current: {current})")


# ------------------------------------------------------------- calibrate ----

_UUID_LIT = re.compile(
    r"""=\s*["'][0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"""
    r"""[0-9a-fA-F]{4}-[0-9a-fA-F]{12}["']""")
# Name denylist widened after the 2026-07-25 adversarial audit (auth/bearer/
# pat/cookie/session/credential were invisible); value shapes now also cover
# hyphen-less 32-hex and long base64-ish literals in any assignment.
_CRED_ASSIGN = re.compile(
    r"""\b\w*(token|api[-_]?key|secret|passwd|password|auth|bearer|"""
    r"""credential|session|cookie)\w*\s*=\s*["'][^"']{8,}["']""",
    re.IGNORECASE)  # NB: no "pat" alternative — it matched path_note etc;
# real PATs (ghp_…) are caught by the entropy check below instead.
# Value-shape detection only on an ASSIGNMENT right-hand side, and only for
# literals with real credential entropy — a long CamelCase XBRL tag or an SQL
# fragment is not a secret (false positives found while hardening, 2026-07-25).
_ASSIGN_RHS = re.compile(r"""=\s*["']([^"']{16,})["']""")


def _looks_random(v: str) -> bool:
    if " " in v or "/" in v.strip("/") and "." in v:
        return False
    hexish = re.fullmatch(r"[0-9a-fA-F]{32,}", v)
    if hexish:
        return True
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]{24,}", v):
        return False
    # require mixed case AND digits: entropy, not an identifier
    return (any(c.isdigit() for c in v) and any(c.islower() for c in v)
            and any(c.isupper() for c in v))


def scan_credential_shapes(paths) -> list[str]:
    """The gate detect-secrets misses (calibration finding, 2026-07-25):
    `token` is not in its keyword denylist and hyphenated UUIDs defeat both
    entropy detectors — and UUID tokens are exactly this app's real credential
    format. Flags exact-UUID string literals and credential-named assignments;
    `pragma: allowlist secret` is honoured for reviewed false positives."""
    hits = []
    for p in paths:
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if "allowlist secret" in line:
                continue
            rhs = _ASSIGN_RHS.search(line)
            if (_UUID_LIT.search(line) or _CRED_ASSIGN.search(line)
                    or (rhs and _looks_random(rhs.group(1)))):
                hits.append(f"{p}:{i}: {line.strip()[:80]}")
    return hits


def find_vacuous_test_asserts(paths) -> list[str]:
    """`assert True`-style assertions in tests: pass unconditionally, so the
    test tests nothing (seeded-defect class 4; ruff's S101 exempts tests/)."""
    rx = re.compile(r"^\s*assert\s+(True|1|\"[^\"]+\"|'[^']+')\s*(#.*)?$")
    hits = []
    for p in paths:
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if rx.match(line):
                hits.append(f"{p}:{i}")
    return hits


def collect_imports(paths) -> set[str]:
    """Top-level module names imported by real import statements (AST, not
    regex: docstring prose like 'from Stooq' was being read as an import)."""
    import ast
    names: set[str] = set()
    for p in paths:
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names - {"app", "tests"}


def declared_dependencies() -> set[str]:
    """Distribution names declared in pyproject (normalised)."""
    text = (ROOT / "pyproject.toml").read_text(errors="replace")
    out = set()
    for m in re.finditer(r'"([A-Za-z][A-Za-z0-9._-]*)\s*(?:[<>=!~\[]|")', text):
        out.add(m.group(1).lower().replace("_", "-"))
    return out


def unresolvable_imports(module_names) -> list[str]:
    import importlib.util
    return [m for m in module_names
            if importlib.util.find_spec(m) is None]


def cmd_calibrate() -> None:
    """Six-class corpus from audit/02-calibration.md, §9.3: prove the gates
    still catch what they exist to catch. A gate that cannot run fails."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)

        # 1 — hard-coded credential, in this app's REAL credential shape
        cred = tdir / "seeded_cred.py"
        cred.write_text(
            'sipgate_token = "4ed251b5-8a9c-4b3e'  # pragma: allowlist secret -- seeded fixture
            '-9f21-7c6d5e4a3b21"\n')
        if not scan_credential_shapes([cred]):
            failures.append("class 1 (uuid credential) NOT caught by the "
                            "credential-shape scanner")
        # and the scanner must be clean on the actual repo
        repo_py = [p for p in ROOT.glob("app/**/*.py")] + \
                  [p for p in ROOT.glob("scripts/*.py")]
        live = scan_credential_shapes(repo_py)
        if live:
            failures.append("credential-shape scanner fired on live source: "
                            + "; ".join(live[:5]))

        # 2 — swallowed exception -> ruff S110 (the CI lint gate)
        swallow = tdir / "seeded_swallow.py"
        swallow.write_text("def f():\n    try:\n        g()\n"
                           "    except Exception:\n        pass\n")
        r = subprocess.run([sys.executable, "-m", "ruff", "check",
                            "--select", "S110", "--isolated", str(swallow)],
                           capture_output=True, text=True)
        # Fail-closed BOTH ways (self-audit finding): exit 0 means the defect
        # slipped through, but a nonzero exit with no S110 in the output means
        # ruff itself failed to run — which must read as a failed gate, never
        # as a catch.
        if r.returncode == 0 or "S110" not in (r.stdout + r.stderr):
            failures.append("class 2 (swallowed exception) NOT caught by ruff "
                            f"S110 (exit={r.returncode})")

        # 3 — non-existent dependency -> import-resolution check
        if unresolvable_imports(["reqwests_http"]) != ["reqwests_http"]:
            failures.append("class 3 (hallucinated package) NOT caught by the "
                            "import-resolution check")
        # and every real top-level app import must resolve (the standing gate)
        top = collect_imports(ROOT.glob("app/**/*.py"))
        declared = declared_dependencies()
        # A module that is DECLARED in pyproject but absent here is an
        # environment gap (optional/heavy deps), not a hallucination; an
        # UNDECLARED unresolvable import is the seeded-class-3 signal.
        missing = [m for m in unresolvable_imports(sorted(top))
                   if m.lower().replace("_", "-") not in declared]
        if missing:
            failures.append("live app imports neither resolvable nor declared "
                            f"in pyproject: {missing}")

        # 4 — vacuous assertion in a test
        vac = tdir / "test_seeded_vac.py"
        vac.write_text("def test_something():\n    assert True\n")
        if not find_vacuous_test_asserts([vac]):
            failures.append("class 4 (vacuous assertion) NOT caught")
        live_vac = find_vacuous_test_asserts(list(ROOT.glob("tests/**/*.py")))
        if live_vac:
            failures.append("vacuous assertions in the live suite: "
                            + "; ".join(live_vac[:5]))

    # 5 — untrusted text -> tool call: N/A by structure; re-validate (§9.9):
    # the LLM judgment path must pass NO tools to the API.
    llm_src = "".join(p.read_text(errors="replace")
                      for p in ROOT.glob("app/**/*.py"))
    # Panel finding (PR #23): matching only `tools=` let
    # `create(..., **{"tools": [...]})` enable tool use while calibration
    # still reported the class N/A. Match the kwarg in every spelling.
    if re.search(r"""["']?\btools["']?\s*[=:]""", llm_src):
        failures.append("class 5 N/A no longer holds: a `tools=` parameter "
                        "appeared in app/ — the no-tool-call architecture "
                        "assumption is void; re-run A-10/C-06/C-07")

    # 6 — cross-tenant ownership: N/A by structure; re-validate: still no
    # per-user tables (single-tenant).
    models_src = (ROOT / "app" / "models.py").read_text(errors="replace")
    # Panel finding (PR #23): a three-name denylist left `account_id`,
    # `customer_id`, an `organisation` FK or a users/accounts relationship
    # free to introduce real multi-tenancy while calibration reported N/A.
    _TENANCY = (r"\b(user|tenant|owner|account|customer|org|organisation|"
                r"organization|workspace|member|subject|principal)_id\b"
                r"|ForeignKey\(\s*[\"']"
                r"(users|accounts|tenants|orgs|organisations|organizations|"
                r"customers|members|workspaces)\.")
    if re.search(_TENANCY, models_src, re.IGNORECASE):
        failures.append("class 6 N/A no longer holds: per-user/tenant columns "
                        "appeared in app/models.py — re-run C-01")

    if failures:
        _fail("seeded-defect calibration (S12) — " + " | ".join(failures) +
              " — a gate that stopped catching its seeded defect is a FAILED "
              "gate and freezes releases (§9.3)")
    print("calibrate: OK — 4 seedable classes caught; 2 N/A classes "
          "structurally re-validated; live source clean")


# --------------------------------------------------------------- surface ----


def cmd_surface() -> None:
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    routes = []
    for p in ROOT.glob("app/routers/*.py"):
        for m in re.finditer(r"@router\.(get|post|put|delete)\(\s*[\"']([^\"']+)",
                             p.read_text(errors="replace")):
            routes.append({"method": m.group(1).upper(), "path": m.group(2),
                           "module": str(p.relative_to(ROOT))})
    surface = {
        "generated_by": "scripts/mandate_gate.py surface",
        "files_tracked": len(tracked),
        "python_modules": sorted(f for f in tracked if f.endswith(".py")),
        "routes": sorted(routes, key=lambda r: (r["path"], r["method"])),
        "scheduled_jobs": ["4-hourly recompute (APScheduler, app/main.py)",
                           "deploy watchdog (host systemd, docs/AUTO_DEPLOY.md)"],
        "data_stores": ["SQLite (DB_URL; snapshots, indicator_readings, "
                        "falsification_outcomes append-only, dashboard_feed, "
                        "daily_close, breadth caches)"],
        "model_providers": ["Anthropic (judgment note; generator vendor)",
                            "OpenAI (independent verifier panel; second vendor)"],
        "egress": ["FRED/ALFRED", "SEC EDGAR", "Shiller xls", "Stooq/price",
                   "CNN Fear&Greed", "sipgate SMS", "Anthropic API",
                   "OpenAI API (CI panel)"],
        "identities": ["operator (mglaeser) — human-in-command",
                       "GitHub Actions runner (CI gates)",
                       "container app user (runtime)"],
        "policy_bundle": [".github/workflows/ci.yml",
                          ".github/workflows/independent-verify.yml",
                          "scripts/independent_verify.py",
                          "scripts/mandate_gate.py",
                          "governance/ (CODEOWNERS-protected)"],
        "workflows": sorted(f for f in tracked if f.startswith(".github/")),
        "prompts": ["app/llm.py (judgment note; numbers-only invariant)",
                    "app/notify/ (SMS text)"],
    }
    out = AUDIT / "00-audit-surface.json"
    out.write_text(json.dumps(surface, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out.relative_to(ROOT)} ({len(tracked)} tracked files, "
          f"{len(routes)} routes)")


# ------------------------------------------------------------------ main ----


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "status":
        cmd_status(write="--write" in args)
    elif cmd == "ratchet":
        cmd_ratchet(measure_only="--measure" in args)
    elif cmd == "calibrate":
        cmd_calibrate()
    elif cmd == "surface":
        cmd_surface()
    elif cmd == "all":
        cmd_status(write=False)
        cmd_ratchet(measure_only=False)
        cmd_calibrate()
        print("mandate-gate: ALL OK")
    else:
        print(f"unknown subcommand {cmd!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
