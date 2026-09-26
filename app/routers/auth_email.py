"""
Email + password sign-in, added next to -- not in place of -- the existing
username + password POST /auth/login (app/routers/auth.py), which is left
unchanged. The dashboard's login field accepts either: anything containing
"@" is sent here, anything else to /auth/login.

Returns exactly the same token /auth/login does, so every protected route
works the same whichever way the user signed in.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.auth import create_access_token, verify_password
from app.database import get_db
from app.schemas import Token

router = APIRouter(prefix="/auth", tags=["auth"])


class EmailLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


@router.post("/login/email", response_model=Token, summary="Log in with email and password")
def login_with_email(credentials: EmailLogin, db: Session = Depends(get_db)):
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )
    # Case-insensitive, like the Auth0 account matching (app/auth0.py).
    # Two accounts differing only by case can't be told apart by email, so
    # that is refused rather than guessed -- those users can still use
    # their username.
    matches = db.query(models.User).filter(func.lower(models.User.email) == credentials.email.lower()).all()
    if len(matches) != 1 or not verify_password(credentials.password, matches[0].password_hash):
        raise invalid
    return Token(access_token=create_access_token(data={"sub": str(matches[0].id)}), token_type="bearer")
