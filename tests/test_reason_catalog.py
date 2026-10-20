"""Customer-approved reasons remain available to the live classifier."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_customer_approved_reasons_are_active(session: AsyncSession) -> None:
    rows = await session.execute(
        text("select name from wb.negative_reasons where is_active and is_approved")
    )
    names = {str(row.name) for row in rows.all()}

    assert {
        "ухудшение качества товара",
        "нет заявленного эффекта",
        "плохая стирка",
        "не оправдались ожидания",
        "мало пены",
        "аромат не соответствует заявленному",
        "слабый аромат",
        "высокая цена",
        "большой расход средства",
        "нечитаемый отзыв",
        "не соответствует заявленному",
        "неудобная резинка",
        "холод после намокания",
        "большемерят",
        "ошибочно низкая оценка",
        "дискомфорт",
        "слишком жидкое средство",
        "работа ПВЗ",
    } <= names
