from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.schemas import SupportChatAskRequest, SupportChatHistoryResponse, SupportChatMessageResponse
from app.services import support_chat as support_chat_service

router = APIRouter(prefix="/support-chat", tags=["support-chat"])


@router.post(
    "/ask",
    response_model=SupportChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ask the AI support assistant",
    description="Answers the authenticated user's support question with Claude, using the built-in support "
    "FAQs plus the caller's own plan and usage as context. If Claude is disabled or unavailable, the best "
    "matching predefined FAQ answer is returned instead -- response_source says which one answered. Every "
    "exchange is stored in the caller's chat history.",
    responses={
        201: {"description": "The stored exchange: the question, the answer, and where the answer came from."},
        401: {"description": "Missing or invalid access token."},
        422: {"description": "The question is blank or longer than 2000 characters."},
    },
)
def ask_support_question(
    request: SupportChatAskRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return support_chat_service.answer_question(db, current_user, request.question)


@router.get(
    "/history",
    response_model=SupportChatHistoryResponse,
    summary="View your support chat history",
    description="Returns the authenticated user's own support chat exchanges, oldest first. There is no "
    "user_id parameter anywhere in this path -- the caller is always resolved from the access token via "
    "get_current_user, so another user's chat history can never be requested. Mirrors the same self-scoping "
    "already used by GET /notifications/ and GET /dashboard/me.",
    responses={
        200: {"description": "The caller's own chat exchanges."},
        401: {"description": "Missing or invalid access token."},
    },
)
def read_my_support_history(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    messages = (
        db.query(models.SupportChatMessage)
        .filter(models.SupportChatMessage.user_id == current_user.id)
        .order_by(models.SupportChatMessage.created_at, models.SupportChatMessage.id)
        .all()
    )
    return {"messages": messages}
