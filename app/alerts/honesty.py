"""The honesty lint: vocabulary an operator message must never contain.

The score is not a probability, the service gives no advice, and nothing
here is certain. The lint reads every language the phrase set carries
(v3.5 is German and English), and it runs twice: on every fragment of
every language when a phrase set is validated - so a translation the
operator has not yet switched to cannot be promoted with a word in it
that would make the renderer refuse every message using it (#119 round
7, SOTA-A) - and on the rendered body before anything reaches a wire.

It is its own module because the renderer and the phrase registry both
need it and the renderer imports the registry.
"""
from __future__ import annotations

import re

_FORBIDDEN = re.compile(
    r"(?i)\b(wahrscheinlich\w*|sicher\b|garantiert\w*|kaufen|verkaufen|empfehl\w*|"
    r"crash\w*|prognos\w*|"
    r"probab\w*|certain\w*|guarantee\w*|buy\b|buying|sell\b|sells|selling|"
    r"recommend\w*|forecast\w*|predict\w*)"
)

#: The one honest use of the banned noun: denying it. The disclaimer the
#: mandate itself requires - the caveat NOT_A_PROBABILITY every score-
#: bearing rule carries reads "Wert ist keine Wahrscheinlichkeit." - names
#: the noun to say the score is not one. That idiom, in either language, is
#: removed before the stems are sought; the same stem anywhere else still
#: offends. (Found by linting the shipped sets at validation: the caveat
#: had tripped the render-time lint since v3.2, on six disabled rules.)
_DISCLAIMER = re.compile(r"(?i)\b(?:keine\s+wahrscheinlichkeit|not\s+a\s+probability|no\s+probability)\b")


def honesty_lint(body: str) -> str | None:
    """The forbidden phrase found, or None."""
    match = _FORBIDDEN.search(_DISCLAIMER.sub(" ", body))
    return match.group(0) if match else None
