"""Outbound notification channels: iMessage (via imessage-proxy) and sipgate SMS.

The daily digest picks exactly one, via `Settings.daily_digest_transport`.
Both senders share a shape on purpose — never raise, return a small result
dataclass — because a failed digest must not take the scheduler down with it.
"""

import httpx

#: The httpx failures of a request that was never written: no connection was
#: made, or none was taken from the pool, so nothing reached the provider. Any
#: other failure of a send leaves the delivery unknown: the provider may have
#: read the request, and its answer was lost or late. A class this tuple does
#: not name reads as unknown too, whatever it was: unknown claims least, and
#: for the failure alarm it errs towards an all-clear nobody needed, the
#: smaller mistake by its own rule. The same four classes the alert sender
#: calls not accepted (app/alerts/sender.py:_classify_exception; held together
#: by tests/test_alert_imessage_sender.py).
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.PoolTimeout)
