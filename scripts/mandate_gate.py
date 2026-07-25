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
OPEN_VERDICTS = ("FAIL", "PARTIAL", "NO-EVIDENCE")


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

    f_ids = [f["id"] for f in findings]
    if len(f_ids) != len(set(f_ids)):
        _fail("findings contain duplicate ids")
    if set(f_ids) != set(cat_ids):
        missing = sorted(set(cat_ids) - set(f_ids))
        extra = sorted(set(f_ids) - set(cat_ids))
        _fail(f"findings/catalogue mismatch — missing: {missing} extra: {extra}")

    # §3 conditional escalation: a PASS with no standing control is PARTIAL.
    for f in findings:
        if f["verdict"] == "PASS" and not f.get("standing_control"):
            _fail(f"{f['id']} is PASS with a null standing_control — "
                  "per the mandate that verdict is PARTIAL, always")

    open_blockers: list[str] = []
    band_counts = {b: 0 for b in BLOCKER_BANDS}
    for f in findings:
        band = f.get("escalated_band") or f["band"]
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

    # Governance hash attestation (Article XI / B-35).
    const_path = GOV / "constitution.md"
    const_hash = _sha256(const_path)
    if manifest["constitution_sha256"] != const_hash:
        _fail("constitution.md hash does not match governance/mandate/"
              "manifest.json — amendments must go through the gate and "
              "bump the attested hash (Article XIII)")
    p1 = GOV / "mandate" / "part1.md"
    if manifest["part1_sha256"] != _sha256(p1):
        _fail("governance/mandate/part1.md hash mismatch vs manifest — "
              "the mandate text is immutable (§9.9)")

    evidenced = sum(1 for f in findings if f["verdict"] != "NO-EVIDENCE")
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
        "part1_status": "COMPLETE",
        "part2_status": "COMPLETE",
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
_CRED_ASSIGN = re.compile(
    r"""\b\w*(token|api_key|secret|passwd|password)\w*\s*=\s*["'][^"']{8,}["']""",
    re.IGNORECASE)


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
            if _UUID_LIT.search(line) or _CRED_ASSIGN.search(line):
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
        top = set()
        for p in ROOT.glob("app/**/*.py"):
            for line in p.read_text(errors="replace").splitlines():
                m = re.match(r"^(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
                if m and not m.group(1).startswith(("app", "tests")):
                    top.add(m.group(1))
        top -= {"lppls"}  # optional engine: suite is hermetic without it (A-02)
        missing = unresolvable_imports(sorted(top))
        if missing:
            failures.append(f"live app imports do not resolve: {missing}")

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
    if re.search(r"\btools\s*=", llm_src):
        failures.append("class 5 N/A no longer holds: a `tools=` parameter "
                        "appeared in app/ — the no-tool-call architecture "
                        "assumption is void; re-run A-10/C-06/C-07")

    # 6 — cross-tenant ownership: N/A by structure; re-validate: still no
    # per-user tables (single-tenant).
    models_src = (ROOT / "app" / "models.py").read_text(errors="replace")
    if re.search(r"user_id|tenant_id|owner_id", models_src):
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
