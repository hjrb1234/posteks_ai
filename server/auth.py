"""Opaque, expiring database sessions; no credentials are stored in browser storage."""
from datetime import timedelta
import hashlib
import hmac
import secrets
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import settings
from .db import get_session, utcnow
from .models import AuthSession, GuestSession, User

AUTH_COOKIE = 'posteks_session'
GUEST_COOKIE = 'posteks_guest'


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    rounds = 600000
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), rounds).hex()
    return f'pbkdf2_sha256${rounds}${salt}${digest}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        method, rounds, salt, expected = encoded.split('$')
        if method != 'pbkdf2_sha256' or not 100000 <= int(rounds) <= 2000000:
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def set_cookie(response: Response, name: str, token: str, days: int):
    response.set_cookie(name, token, max_age=days * 86400, httponly=True,
                        secure=settings.cookie_secure, samesite='lax', path='/')


def optional_user(request: Request, session: Session) -> User | None:
    token = request.cookies.get(AUTH_COOKIE, '')
    if not token or len(token) > 200:
        return None
    auth = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(token), AuthSession.expires_at > utcnow()))
    if not auth:
        return None
    user = session.get(User, auth.user_id)
    return user if user and user.active else None


def require_staff(request: Request, session: Session = Depends(get_session)) -> User:
    user = optional_user(request, session)
    if not user or user.role not in {'admin', 'operator', 'engineer'}:
        raise HTTPException(401, 'Требуется вход сотрудника')
    return user


def require_admin(user: User = Depends(require_staff)) -> User:
    if user.role != 'admin':
        raise HTTPException(403, 'Требуются права администратора')
    return user


def create_session(user: User, session: Session, response: Response) -> None:
    token = secrets.token_urlsafe(48)
    session.add(AuthSession(token_hash=token_hash(token), user_id=user.id, expires_at=utcnow() + timedelta(days=settings.session_days)))
    session.commit()
    set_cookie(response, AUTH_COOKIE, token, settings.session_days)


def get_actor_scope(request: Request, response: Response, session: Session) -> str:
    user = optional_user(request, session)
    if user:
        return f'user:{user.id}'
    raw = request.cookies.get(GUEST_COOKIE, '')
    guest = None
    if raw and len(raw) < 200:
        guest = session.scalar(select(GuestSession).where(GuestSession.token_hash == token_hash(raw), GuestSession.expires_at > utcnow()))
    if guest is None:
        raw = secrets.token_urlsafe(48)
        guest = GuestSession(token_hash=token_hash(raw), expires_at=utcnow() + timedelta(days=30))
        session.add(guest)
        session.commit()
        set_cookie(response, GUEST_COOKIE, raw, 30)
    return f'guest:{guest.id}'


def user_dict(user: User):
    return {'id': user.id, 'username': user.username, 'role': user.role}
