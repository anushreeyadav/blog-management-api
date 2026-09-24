import math

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.schemas import AiSupportHistoryResponse, AiSupportRequest, AiSupportResponse
from app.services import support_chat as support_chat_service

router = APIRouter(prefix="/api/ai-support", tags=["ai-support"])


@router.post(
    "/",
    response_model=AiSupportResponse,
    summary="Ask AI support a question",
    description="Returns a support answer for the authenticated user's message, and when it was saved -- the minimal "
    "request/response form of POST /support-chat/ask, backed by the same service "
    "(app/services/support_chat.py): Claude when AI_CHAT_ENABLED is on, otherwise the predefined FAQ "
    "answer from app/services/support_faq.py. The exchange is saved to the caller's support chat history "
    "(GET /support-chat/history) either way.",
    responses={
        200: {
            "description": "The support answer and the time the exchange was saved.",
            "content": {
                "application/json": {
                    "example": {
                        "response": "To create a post, log in and send POST /posts with a title and content. ...",
                        "timestamp": "2026-09-24T12:18:09.424722+05:30",
                    }
                }
            },
        },
        401: {"description": "Missing or invalid access token."},
        422: {"description": "The message is missing, blank, or longer than 2000 characters."},
    },
)
def ask_ai_support(
    request: AiSupportRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    exchange = support_chat_service.answer_question(db, current_user, request.message)
    return {"response": exchange.response, "timestamp": exchange.created_at}


@router.get(
    "/history/",
    response_model=AiSupportHistoryResponse,
    summary="View your AI support chat history",
    description="Returns the authenticated user's own AI support exchanges, newest first, paginated the same "
    "way as GET /posts (page/limit, plus total and total_pages). There is no user_id parameter anywhere in "
    "this path -- the caller is always resolved from the access token via get_current_user, so another "
    "user's messages can never be requested. Includes exchanges from both POST /api/ai-support/ and the "
    "chat widget (POST /support-chat/ask), which share one history.",
    responses={
        200: {"description": "One page of the caller's own exchanges, newest first."},
        401: {"description": "Missing or invalid access token."},
        422: {"description": "page is below 1, or limit is outside 1-100."},
    },
)
def read_my_ai_support_history(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(10, ge=1, le=100, description="Messages per page (max 100)"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.SupportChatMessage).filter(models.SupportChatMessage.user_id == current_user.id)

    total = query.count()
    offset = (page - 1) * limit
    records = (
        query.order_by(models.SupportChatMessage.created_at.desc(), models.SupportChatMessage.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    total_pages = math.ceil(total / limit) if total else 0

    return {
        "messages": [
            {
                "id": record.id,
                "question": record.question,
                "ai_response": record.response,
                "created_at": record.created_at,
            }
            for record in records
        ],
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
    }
