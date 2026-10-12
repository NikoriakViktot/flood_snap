"""Service JWT minted by Django (geohydro/adapters/tokens_jwt.py): HS256, aud=geoprocessing, iss=geohydro-platform."""
from __future__ import annotations

import jwt
from fastapi import HTTPException, Request

from lab_service import settings


def verify_service_jwt(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing service token")
    if not settings.SERVICE_JWT_KEY:
        raise HTTPException(503, "SERVICE_JWT_KEY is not configured")
    try:
        return jwt.decode(auth[7:].strip(), settings.SERVICE_JWT_KEY, algorithms=[settings.SERVICE_JWT_ALG],
                          audience=settings.SERVICE_JWT_AUD, issuer=settings.SERVICE_JWT_ISS)
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"invalid service token: {e}")
