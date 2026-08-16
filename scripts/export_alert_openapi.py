"""Generate `docs/openapi-alerts.json` from the RUNNING app schema.

The application's own `/openapi.json` stays the single source of truth. This
extracts the alert subset — paths, the components they reference, and the tags
— so the alert contract can be reviewed in a diff instead of by starting the
service. CI regenerates it and fails on drift, which is what stops the
committed artifact from quietly describing an API that no longer exists.

    python -m scripts.export_alert_openapi          # write
    python -m scripts.export_alert_openapi --check  # fail on drift
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ARTIFACT = Path(__file__).resolve().parents[1] / "docs" / "openapi-alerts.json"
ALERT_PREFIXES = ("/api/v1/alerts", "/api/v1/admin/alerts")


def _referenced_schemas(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _referenced_schemas(value, found)
    elif isinstance(node, list):
        for value in node:
            _referenced_schemas(value, found)


def extract_alert_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The alert paths plus the transitive closure of the schemas they use."""
    paths = {
        path: spec for path, spec in schema.get("paths", {}).items()
        if path.startswith(ALERT_PREFIXES)
    }
    wanted: set[str] = set()
    _referenced_schemas(paths, wanted)

    all_schemas = schema.get("components", {}).get("schemas", {})
    # Close over nested references so the artifact validates standalone.
    while True:
        nested: set[str] = set()
        for name in wanted:
            _referenced_schemas(all_schemas.get(name, {}), nested)
        if nested <= wanted:
            break
        wanted |= nested

    return {
        "openapi": schema.get("openapi"),
        "info": {
            "title": f"{schema.get('info', {}).get('title', 'bubblegauge')} — alerts",
            "version": schema.get("info", {}).get("version"),
            "description": (
                "Alert-system subset of the bubblegauge OpenAPI document, generated "
                "from the running application. Reads use ALERTS_READ_API_KEY, writes "
                "use ALERTS_WRITE_API_KEY; neither falls back to the admin key. "
                "Research, not advice."
            ),
        },
        "paths": dict(sorted(paths.items())),
        "components": {
            "schemas": {name: all_schemas[name] for name in sorted(wanted)
                        if name in all_schemas}
        },
    }


def build() -> dict[str, Any]:
    from app.main import create_app

    return extract_alert_schema(create_app().openapi())


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    generated = build()
    rendered = json.dumps(generated, indent=2, sort_keys=True) + "\n"

    if "--check" in argv:
        if not ARTIFACT.exists():
            print(f"missing artifact {ARTIFACT}", file=sys.stderr)
            return 1
        if ARTIFACT.read_text(encoding="utf-8") != rendered:
            print("docs/openapi-alerts.json is stale; regenerate it", file=sys.stderr)
            return 1
        print("openapi artifact is current")
        return 0

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(rendered, encoding="utf-8")
    print(f"wrote {ARTIFACT} ({len(generated['paths'])} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
