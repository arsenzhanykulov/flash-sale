"""Чтение распродаж."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.api.deps import SessionDep
from app.api.schemas.sales import SaleResponse
from app.services.sales import get_sale, list_sales

router = APIRouter()


@router.get("", response_model=list[SaleResponse], summary="Список распродаж")
async def get_sales(session: SessionDep) -> list[SaleResponse]:
    sales = await list_sales(session)
    return [SaleResponse.model_validate(sale) for sale in sales]


@router.get(
    "/{sale_id}",
    response_model=SaleResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "Распродажа не найдена"}},
    summary="Одна распродажа",
)
async def get_sale_by_id(sale_id: UUID, session: SessionDep) -> SaleResponse:
    sale = await get_sale(session, sale_id)
    if sale is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Распродажа не найдена",
        )
    return SaleResponse.model_validate(sale)
