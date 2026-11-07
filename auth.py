"""auth.py - simple email + password login for the Pharma Study Analyser (prototype).

What it gives you
    POST /auth/signup   create a user (email + password)
    POST /auth/login    returns a JWT access token
    GET  /auth/me       shows who the token belongs to
    get_current_user    FastAPI dependency: returns the user_id from the token.
                        Use it in every endpoint (upload, search, compare) so the
                        user id always comes from the token and never from the browser.

Needs in .env
    JWT_SECRET=<long random string>         # generate: python -c "import secrets; print(secrets.token_hex(32))"
    TOKEN_MINUTES=60                        # optional
    AZURE_SQL_SERVER / AZURE_SQL_DATABASE / AZURE_SQL_USERNAME / AZURE_SQL_PASSWORD / AZURE_SQL_DRIVER
    (If AZURE_SQL_SERVER is empty, a local SQLite file auth_dev.db is used for testing.)

Run alone for testing:
    uvicorn auth:app --reload      then open http://127.0.0.1:8000/docs
"""
import datetime as dt
import os
import re
import uuid
from urllib.parse import quote_plus

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine, insert, select

load_dotenv(override=True)

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGO = "HS256"
TOKEN_MINUTES = int(os.getenv("TOKEN_MINUTES", "60"))
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is missing in .env (see the note at the top of auth.py)")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------- database ----------
def _make_engine():
    server = os.getenv("AZURE_SQL_SERVER")
    if not server:
        return create_engine("sqlite:///auth_dev.db")  # local testing only
    odbc = (
        f"DRIVER={{{os.getenv('AZURE_SQL_DRIVER', 'ODBC Driver 18 for SQL Server')}}};"
        f"SERVER={server};DATABASE={os.getenv('AZURE_SQL_DATABASE')};"
        f"UID={os.getenv('AZURE_SQL_USERNAME')};PWD={os.getenv('AZURE_SQL_PASSWORD')};"
        "Encrypt=yes;TrustServerCertificate=no"
    )
    return create_engine("mssql+pyodbc:///?odbc_connect=" + quote_plus(odbc))


engine = _make_engine()
metadata = MetaData()
users = Table(
    "users",
    metadata,
    Column("user_id", String(36), primary_key=True),
    Column("email", String(255), unique=True, nullable=False),
    Column("password_hash", String(100), nullable=False),
    Column("created_at", DateTime, nullable=False),
)
metadata.create_all(engine)  # creates the users table if it does not exist


# ---------- helpers ----------
def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _check_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def _make_token(user_id: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {"sub": user_id, "iat": now, "exp": now + dt.timedelta(minutes=TOKEN_MINUTES)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


# ---------- API ----------
router = APIRouter(prefix="/auth", tags=["auth"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


class SignupBody(BaseModel):
    email: str
    password: str


@router.post("/signup", status_code=201)
def signup(body: SignupBody):
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email.")
    if not (8 <= len(body.password.encode("utf-8")) <= 72):
        raise HTTPException(status_code=400, detail="Password must be 8 to 72 characters.")

    with engine.begin() as conn:
        exists = conn.execute(select(users.c.user_id).where(users.c.email == email)).first()
        if exists:
            raise HTTPException(status_code=409, detail="This email is already registered.")
        user_id = str(uuid.uuid4())
        conn.execute(
            insert(users).values(
                user_id=user_id,
                email=email,
                password_hash=_hash_password(body.password),
                created_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
            )
        )
    return {"user_id": user_id, "email": email}


@router.post("/login")
def login(form: OAuth2PasswordRequestForm = Depends()):
    # the form field is called "username" but we use it for the email
    email = form.username.strip().lower()
    with engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.email == email)).first()
    if row is None or not _check_password(form.password, row.password_hash):
        raise HTTPException(status_code=401, detail="Wrong email or password.")
    return {"access_token": _make_token(row.user_id), "token_type": "bearer"}


def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    """Return the user_id from the token. Use as: user_id: str = Depends(get_current_user)"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired. Log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token.")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token.")
    return user_id


@router.get("/me")
def me(user_id: str = Depends(get_current_user)):
    with engine.connect() as conn:
        row = conn.execute(select(users.c.email).where(users.c.user_id == user_id)).first()
    if row is None:
        raise HTTPException(status_code=401, detail="User not found.")
    return {"user_id": user_id, "email": row.email}


# standalone app for testing; in the real project api.py will include `router`
app = FastAPI(title="Pharma Study Analyser - auth")
app.include_router(router)
