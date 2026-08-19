"""Outbound notification channels: iMessage (via imessage-proxy) and sipgate SMS.

The daily digest picks exactly one, via `Settings.daily_digest_transport`.
Both senders share a shape on purpose — never raise, return a small result
dataclass — because a failed digest must not take the scheduler down with it.
"""
