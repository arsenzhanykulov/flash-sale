"""Дашборд магазина."""

from fastapi import APIRouter, status

from app.api.deps import CurrentShop, SessionDep
from app.api.schemas.shop import ShopSalesResponse
from app.services.shop import list_shop_sales

router = APIRouter()


@router.get(
    "/sales",
    response_model=ShopSalesResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Требуется вход"},
        status.HTTP_403_FORBIDDEN: {"description": "Раздел доступен только магазинам"},
    },
    summary="Мои распродажи со счётчиками и выручкой",
)
async def shop_sales(current_shop: CurrentShop, session: SessionDep) -> ShopSalesResponse:
    view = await list_shop_sales(session, shop_id=current_shop.id)
    return ShopSalesResponse.model_validate(view)
