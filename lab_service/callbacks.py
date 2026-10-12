"""POST the job state to Django's callback URL (Bearer callback_token). Failures are logged: the job file stays the
truth and Django re-polls GET /internal/jobs/{id} every 60 s."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


def post_callback(url: str | None, token: str | None, body: dict, retries: int = 3) -> bool:
    if not url:
        return False
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for i in range(retries):
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=20)
            if r.status_code < 300:
                return True
            log.warning("callback %s -> %s %s", url, r.status_code, r.text[:200])
            if r.status_code < 500 and r.status_code != 409:
                return False
        except httpx.HTTPError as e:
            log.warning("callback %s failed: %s", url, e)
        time.sleep(2 * (i + 1))
    return False
