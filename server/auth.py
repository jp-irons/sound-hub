"""Authentication and authorisation for Sound Hub.

Three FastAPI dependencies
--------------------------
get_current_user
    Validates a Bearer JWT from the Authorization header.
    Sets request.state.auth_user so the audit middleware can log it.
    Raises 401 if the token is missing or invalid.

require_admin
    Calls get_current_user and checks role == 'admin'.
    Raises 403 if the user is a viewer.

require_node
    Checks the request source IP falls within NODE_LAN_SUBNET.
    Raises 403 if the request arrives from outside the LAN.
    Used on audio/ack and audio/push — these are node-to-hub calls that
    must never be reachable from the internet.

JWT secret
----------
Loaded from the file named by config.AUTH_SECRET_FILE (next to the DB).
Created automatically on first use with 32 bytes of cryptographic randomness.
Add AUTH_SECRET_FILE to .gitignore.
"""
import ipaddress
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from . import config, db

log = logging.getLogger("sound_hub.auth")

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Secret key — loaded or created once at import time
# ---------------------------------------------------------------------------

def _load_or_create_secret() -> str:
    path = Path(config.AUTH_SECRET_FILE)
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_hex(32)
    path.write_text(secret)
    log.info("Generated new JWT secret → %s", path)
    return secret


_SECRET_KEY: str = _load_or_create_secret()
_LAN_NETWORK = ipaddress.ip_network(config.NODE_LAN_SUBNET)


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_token(username: str, role: str) -> str:
    """Return a signed JWT valid for AUTH_TOKEN_EXPIRE_HOURS."""
    expire = datetime.now(timezone.utc) + timedelta(hours=config.AUTH_TOKEN_EXPIRE_HOURS)
    payload = {"sub": username, "role": role, "exp": expire}
    return jwt.encode(payload, _SECRET_KEY, algorithm=config.AUTH_ALGORITHM)


def _decode_token(token: str) -> dict:
    """Decode and verify a JWT.  Raises JWTError on any failure."""
    return jwt.decode(token, _SECRET_KEY, algorithms=[config.AUTH_ALGORITHM])


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Validate Bearer token and return the user record.

    Sets request.state.auth_user for the audit middleware.
    Raises 401 if the token is absent or invalid.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = _decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username: str = payload.get("sub", "")
    user = await db.get_user(username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    request.state.auth_user = username
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Require the caller to be an admin.  Raises 403 for viewers."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user


async def require_node(request: Request) -> None:
    """Restrict an endpoint to LAN source IPs only.

    Nodes push audio from within the LAN; this check ensures those endpoints
    are unreachable even if a port is accidentally forwarded externally.
    """
    try:
        client_ip = ipaddress.ip_address(request.client.host)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unresolvable source IP")

    if client_ip not in _LAN_NETWORK:
        log.warning("require_node: blocked request from %s", client_ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Node endpoints are LAN-only",
        )
