from fastapi import FastAPI

from app.routes import orders, users

app = FastAPI(title="shop")
app.include_router(users.router, prefix="/users")
app.include_router(orders.router, prefix="/orders")
