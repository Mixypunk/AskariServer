from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
import bcrypt
import jwt
import secrets
import time
from collections import defaultdict

from ..database import get_db, User
from ..config import settings

router = APIRouter()
security = HTTPBearer(auto_error=False)


# ── Rate limiting simple en memoire ────────────────────────────────────────────
# { ip: [(timestamp, ...), ...] }
_login_attempts: dict = defaultdict(list)

def _check_rate_limit(ip: str) -> None:
    now = time.time()
    window = settings.LOGIN_RATE_WINDOW
    max_attempts = settings.LOGIN_RATE_LIMIT
    # Nettoyer les tentatives hors fenetre
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < window]
    if len(_login_attempts[ip]) >= max_attempts:
        raise HTTPException(
            status_code=429,
            detail=f"Trop de tentatives. Reessayez dans {window}s.",
            headers={"Retry-After": str(window)},
        )
    _login_attempts[ip].append(now)


# ── Schemas ────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str   # peut contenir un username OU une adresse email
    password: str

class TokenResponse(BaseModel):
    accesstoken: str
    refreshtoken: str
    user: dict

class RefreshRequest(BaseModel):
    token: str


# ── JWT helpers ────────────────────────────────────────────────────────────────
def create_token(user_id: int, token_type: str = "access") -> str:
    expire = datetime.utcnow() + (
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        if token_type == "access"
        else timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )
    return jwt.encode(
        {"sub": str(user_id), "type": token_type, "exp": expire},
        settings.SECRET_KEY, algorithm="HS256"
    )


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expire")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token invalide")


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    token: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db)
) -> User:
    raw_token = None
    if credentials:
        raw_token = credentials.credentials
    elif token:
        raw_token = token
    else:
        raise HTTPException(status_code=401, detail="Non authentifie")

    payload = decode_token(raw_token)
    if payload.get("type") not in ("access", "stream"):
        raise HTTPException(status_code=401, detail="Token invalide")
    user = await db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Utilisateur introuvable")
    user.last_seen = datetime.utcnow()
    await db.commit()
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Acces admin requis")
    return user


# ── Routes ─────────────────────────────────────────────────────────────────────
@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    # Rate limiting par IP
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    identifier = req.username.strip()

    from sqlalchemy import or_

    if settings.REQUIRE_EMAIL_LOGIN:
        # Mode final : uniquement par email
        if '@' not in identifier:
            raise HTTPException(
                status_code=400,
                detail="Veuillez utiliser votre adresse email pour vous connecter."
            )
        result = await db.execute(select(User).where(User.email == identifier))
    else:
        # Mode transition : email OU username
        result = await db.execute(
            select(User).where(
                or_(User.username == identifier, User.email == identifier)
            )
        )

    user = result.scalar_one_or_none()
    if not user or not bcrypt.checkpw(req.password.encode(), user.password.encode()):
        raise HTTPException(status_code=401, detail="Identifiants incorrects")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Compte desactive")

    # Login reussi : reinitialiser le compteur
    _login_attempts.pop(client_ip, None)

    return TokenResponse(
        accesstoken=create_token(user.id, "access"),
        refreshtoken=create_token(user.id, "refresh"),
        user={"id": user.id, "username": user.username, "role": user.role, "can_download": user.can_download}
    )


@router.post("/refresh")
async def refresh_token(req: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = decode_token(req.token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Token de refresh invalide")
    user = await db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Utilisateur introuvable")
    return {
        "accesstoken":  create_token(user.id, "access"),
        "refreshtoken": create_token(user.id, "refresh"),
    }


@router.get("/user")
async def get_user_info(user: User = Depends(get_current_user)):
    return {"id": user.id, "username": user.username, "role": user.role, "can_download": user.can_download}


@router.get("/users")
async def list_users_public(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.is_active == True))
    users = result.scalars().all()
    return {"users": [{"username": u.username} for u in users]}


@router.get("/users/no-email")
async def list_users_without_email(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """[Admin] Liste les utilisateurs sans email — suivi de la migration."""
    result = await db.execute(
        select(User).where(User.email == None, User.is_active == True)  # noqa: E711
    )
    users = result.scalars().all()
    return {
        "count": len(users),
        "ready_for_migration": len(users) == 0,
        "users": [
            {"id": u.id, "username": u.username, "created_at": u.created_at}
            for u in users
        ],
    }


@router.post("/logout")
async def logout(user: User = Depends(get_current_user)):
    return {"message": "Deconnecte"}


class SetupRequest(BaseModel):
    username: str
    password: str

@router.post("/setup")
async def setup(req: SetupRequest, db: AsyncSession = Depends(get_db)):
    count = await db.execute(select(User))
    if count.scalars().first():
        raise HTTPException(status_code=400, detail="Setup deja effectue")
    hashed = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    user = User(username=req.username, password=hashed, role="admin")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {
        "message":      f"Admin '{req.username}' cree",
        "accesstoken":  create_token(user.id, "access"),
        "refreshtoken": create_token(user.id, "refresh"),
    }


# ── QR Code pairing ────────────────────────────────────────────────────────────
_pair_codes: dict = {}

@router.get("/pair/code")
async def generate_pair_code(user: User = Depends(get_current_user)):
    code = secrets.token_urlsafe(16)
    import datetime as dt
    _pair_codes[code] = {
        "user_id": user.id,
        "expires": dt.datetime.utcnow() + dt.timedelta(minutes=5),
    }
    # Nettoyer les codes expires
    now = dt.datetime.utcnow()
    expired = [k for k, v in _pair_codes.items() if v["expires"] < now]
    for k in expired:
        del _pair_codes[k]

    server_url = settings.ALLOWED_ORIGINS.split(",")[0].strip() if settings.ALLOWED_ORIGINS else ""
    return {
        "code": code,
        "expires_in": 300,
        "qr_data": f"{server_url} {code}",
    }


@router.get("/stream-token")
async def get_stream_token(user: User = Depends(get_current_user)):
    """
    Retourne un token de courte duree (15min) uniquement pour le streaming audio.
    Utilise dans l'URL ?token= pour les requetes <audio> qui ne peuvent pas
    envoyer de header Authorization.
    Separe du access token principal pour limiter l'exposition.
    """
    import datetime as dt
    token = jwt.encode(
        {
            "sub": str(user.id),
            "type": "stream",
            "exp": dt.datetime.utcnow() + dt.timedelta(minutes=15),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    return {"stream_token": token, "expires_in": 900}


@router.get("/pair")
async def pair_with_code(code: str, db: AsyncSession = Depends(get_db)):
    import datetime as dt
    entry = _pair_codes.get(code)
    if not entry:
        raise HTTPException(status_code=400, detail="Code invalide")
    if dt.datetime.utcnow() > entry["expires"]:
        del _pair_codes[code]
        raise HTTPException(status_code=400, detail="Code expire")
    user = await db.get(User, entry["user_id"])
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="Utilisateur introuvable")
    del _pair_codes[code]
    return {
        "accesstoken":  create_token(user.id, "access"),
        "refreshtoken": create_token(user.id, "refresh"),
        "user": {"id": user.id, "username": user.username, "role": user.role, "can_download": user.can_download},
    }


# ── TV Pairing (code 6 chiffres, usage unique, stocké en DB) ──────────────────
#
# ⚠️  Le dict en mémoire ne fonctionne PAS avec --workers > 1 (uvicorn multi-process).
#     Les codes sont donc stockés en PostgreSQL, partagé entre tous les workers.
#
# Flux :
#   1. TV (non auth)  → POST /auth/tv/code    → génère et stocke un code en DB
#   2. TV             → GET  /auth/tv/poll    → poll toutes les ~2.5s
#   3. Mobile (auth)  → POST /auth/tv/confirm → marque le code "approved" en DB
#   4. TV             → poll retourne les tokens → connectée
#
# Sécurité :
#   • Code généré via secrets (CSPRNG), usage unique
#   • Expire après 5 minutes (nettoyage automatique à chaque POST)
#   • Rate-limiting 5 codes/min/IP (en mémoire, par worker — suffisant)

_tv_code_rate: dict = defaultdict(list)   # ip → [timestamps]


def _check_tv_rate(ip: str) -> None:
    """5 générations de code max par minute et par IP."""
    now = time.time()
    _tv_code_rate[ip] = [t for t in _tv_code_rate[ip] if now - t < 60]
    if len(_tv_code_rate[ip]) >= 5:
        raise HTTPException(
            status_code=429,
            detail="Trop de demandes de code. Attendez 60s.",
            headers={"Retry-After": "60"},
        )
    _tv_code_rate[ip].append(now)


class TvConfirmRequest(BaseModel):
    code: str


@router.post("/tv/code")
async def create_tv_pair_code(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    [TV — sans auth] Génère un code à 6 chiffres stocké en DB.
    Nettoie les codes expirés à chaque appel.
    """
    from ..database import TvPairCode

    client_ip = request.client.host if request.client else "unknown"
    _check_tv_rate(client_ip)

    now = datetime.utcnow()

    # Nettoyer les codes expirés
    expired = await db.execute(
        select(TvPairCode).where(TvPairCode.expires_at < now)
    )
    for old in expired.scalars().all():
        await db.delete(old)

    # Générer un code unique
    for _ in range(20):
        code = f"{secrets.randbelow(1_000_000):06d}"
        existing = await db.execute(
            select(TvPairCode).where(TvPairCode.code == code)
        )
        if existing.scalar_one_or_none() is None:
            break
    else:
        raise HTTPException(status_code=503, detail="Impossible de générer un code unique.")

    entry = TvPairCode(
        code=code,
        status="pending",
        user_id=None,
        client_ip=client_ip,
        expires_at=now + timedelta(minutes=5),
    )
    db.add(entry)
    await db.commit()

    return {"code": code, "expires_in": 300}


@router.get("/tv/poll")
async def poll_tv_pair(code: str, db: AsyncSession = Depends(get_db)):
    """
    [TV — sans auth] Poll l'état du code.
    Retourne {status: "pending"} ou {status: "approved", accesstoken, ...}
    """
    from ..database import TvPairCode

    now = datetime.utcnow()
    result = await db.execute(
        select(TvPairCode).where(TvPairCode.code == code)
    )
    entry = result.scalar_one_or_none()

    if entry is None:
        raise HTTPException(status_code=410, detail="Code invalide ou expiré.")

    if now > entry.expires_at:
        await db.delete(entry)
        await db.commit()
        raise HTTPException(status_code=410, detail="Code expiré. Générez-en un nouveau.")

    if entry.status == "approved":
        user_id = entry.user_id
        await db.delete(entry)   # usage unique
        await db.commit()
        user = await db.get(User, user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=400, detail="Utilisateur introuvable.")
        return {
            "status":       "approved",
            "accesstoken":  create_token(user.id, "access"),
            "refreshtoken": create_token(user.id, "refresh"),
            "user": {
                "id":           user.id,
                "username":     user.username,
                "role":         user.role,
                "can_download": user.can_download,
            },
        }

    return {"status": "pending"}


@router.post("/tv/confirm")
async def confirm_tv_pair(
    req: TvConfirmRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    [Mobile — auth requise] Valide un code TV depuis le mobile connecté.
    """
    from ..database import TvPairCode

    code = req.code.strip().replace(" ", "")   # accepte "482 619" ou "482619"
    now  = datetime.utcnow()

    result = await db.execute(
        select(TvPairCode).where(TvPairCode.code == code)
    )
    entry = result.scalar_one_or_none()

    if entry is None:
        raise HTTPException(status_code=404, detail="Code introuvable. Vérifiez le code affiché sur la TV.")
    if now > entry.expires_at:
        await db.delete(entry)
        await db.commit()
        raise HTTPException(status_code=410, detail="Code expiré. La TV doit en générer un nouveau.")
    if entry.status == "approved":
        raise HTTPException(status_code=409, detail="Ce code a déjà été utilisé.")

    entry.status  = "approved"
    entry.user_id = user.id
    await db.commit()

    return {
        "message":  f"TV connectée avec le compte '{user.username}' ✓",
        "username": user.username,
    }

