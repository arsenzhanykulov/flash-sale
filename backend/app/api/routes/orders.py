"""Заказы покупателя."""

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep
from app.api.schemas.orders import MyOrdersResponse
from app.services.orders import list_my_orders

router = APIRouter()


@router.get(
    "/me",
    response_model=MyOrdersResponse,
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Требуется вход"}},
    summary="Мои заказы, свежие сверху",
)
async def my_orders(current_user: CurrentUser, session: SessionDep) -> MyOrdersResponse:
    view = await list_my_orders(session, user_id=current_user.id)
    return MyOrdersResponse.model_validate(view)
