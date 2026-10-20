from fastapi import APIRouter

from app.db import ORDERS

router = APIRouter()


@router.get("/{order_id}")
def get_order(order_id: int):
    order = ORDERS[order_id]
    return {"id": order["id"], "total": order["total"]}
