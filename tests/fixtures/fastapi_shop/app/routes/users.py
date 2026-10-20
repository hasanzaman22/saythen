from fastapi import APIRouter

from app.db import USERS

router = APIRouter()


@router.get("/{user_id}")
def get_user(user_id: int):
    return USERS[user_id]
