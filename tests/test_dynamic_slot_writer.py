from __future__ import annotations

import re

from app.content_registry import DYNAMIC_SLOTS


def test_placeholders_still_satisfy_their_own_contract():
    """What is SERVED today must already be legal — the
    placeholder is what a reader sees until generation lands."""
    for s in DYNAMIC_SLOTS:
        assert len(s.placeholder) <= s.max_len, s.slug
        assert re.match(s.regex, s.placeholder), (s.slug, s.placeholder)
