"""API generation bound to the existing server-owned Streamlit login session.

Not a bearer token, cross-process session registry or browser-supplied claim.
"""
from __future__ import annotations

import secrets

STATE_KEY = "__ssai_external_api_generation"


def invalidate(state):
    state[STATE_KEY] = {"epoch": secrets.randbits(128) + 2, "binding": None}
    for key in list(state):
        if str(key).startswith("__external_api_"):
            state.pop(key, None)


def marker(state):
    value = state.get(STATE_KEY)
    if not isinstance(value, dict) or type(value.get("epoch")) is not int or value["epoch"] <= 1:
        invalidate(state)
    return state[STATE_KEY]["epoch"]


def bind_verified(state, expected_marker, identity, permissions):
    """Bind only after fresh central authentication; reject a concurrent switch."""
    if marker(state) != expected_marker:
        return None
    binding = (identity, tuple(sorted(permissions)))
    previous = state[STATE_KEY].get("binding")
    if previous is not None and previous != binding:
        invalidate(state)
    state[STATE_KEY]["binding"] = binding
    return marker(state)
