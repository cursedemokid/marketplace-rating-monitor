"""Synthetic fixtures; deliberately independent of production records and keys."""

import asyncio
import datetime as dt
import os
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import load_settings
from app.db.engine import build_engine
from app.db.session import build_session_factory
from app.services.auth import create_user
from sqlalchemy import text

from e2e.runtime import database_url


async def main() -> None:
    engine = build_engine(database_url())
    today = dt.datetime.now(ZoneInfo("Europe/Moscow")).date()
    end = today - dt.timedelta(days=1)
    baseline = end - dt.timedelta(days=14)
    previous_end = end - dt.timedelta(days=7)
    try:
        async with build_session_factory(engine)() as session:
            for username, role, variable in (
                ("e2eadmin", "admin", "WB_E2E_ADMIN_PASSWORD"),
                ("e2eviewer", "viewer", "WB_E2E_VIEWER_PASSWORD"),
            ):
                await create_user(
                    session,
                    username=username,
                    REDACTED=os.environ[variable],
                    role=role,
                    policy=load_settings().auth,
                )
            await session.execute(
                text("""
                insert into wb.products
                    (nm_id,vendor_code,title,account_key,is_monitored,updated_at)
                values
                    (999921001,'E2E-MAIN','Тестовые подгузники','default',true,now()),
                    (999921002,'E2E-OTHER','Тестовые салфетки','DemoSeller',true,now()),
                    (999921003,'E2E-ADD','Товар для добавления','DemoSeller',false,now()),
                    (999921004,'E2E-NO-TITLE',null,'default',true,now())
                """)
            )
            for day, rating, stars in (
                (baseline, Decimal("4.70"), (83, 3, 3, 2, 1)),
                (previous_end, Decimal("4.80"), (90, 4, 3, 2, 1)),
                (end - dt.timedelta(days=6), Decimal("4.81"), (91, 4, 3, 2, 1)),
                (end - dt.timedelta(days=5), Decimal("4.81"), (93, 4, 3, 2, 2)),
                (end - dt.timedelta(days=3), Decimal("4.80"), (95, 5, 3, 2, 2)),
                (end - dt.timedelta(days=2), Decimal("4.78"), (95, 5, 3, 2, 3)),
                (end, Decimal("4.79"), (97, 5, 3, 2, 3)),
            ):
                await session.execute(
                    text("""
                        insert into wb.daily_snapshots
                            (nm_id,snapshot_date,rating,ratings_total,
                             ratings_5,ratings_4,ratings_3,ratings_2,ratings_1,
                             buyout_percent,orders_count,buyouts_count,collected_at)
                        values (999921001,:day,:rating,:total,:s5,:s4,:s3,:s2,:s1,
                                90,100,90,now())
                        """),
                    dict(
                        day=day,
                        rating=rating,
                        total=sum(stars) + int(day == end - dt.timedelta(days=2)),
                        **{
                            f"s{star}": count
                            for star, count in zip(range(5, 0, -1), stars, strict=True)
                        },
                    ),
                )
                for nm_id in (999921002, 999921004):
                    await session.execute(
                        text("""
                            insert into wb.daily_snapshots
                                (nm_id,snapshot_date,rating,ratings_total,
                                 ratings_5,ratings_4,ratings_3,ratings_2,ratings_1,
                                 buyout_percent,collected_at)
                            values (:nm_id,:day,4.50,10,7,1,1,0,1,:buyout,now())
                            """),
                        {
                            "nm_id": nm_id,
                            "day": day,
                            "buyout": 90 if nm_id == 999921004 and day == end else None,
                        },
                    )
            await session.execute(
                text("""
                insert into wb.nomenclature
                    (account_key,vendor_code,pack,line,category,imported_at)
                values ('default','E2E-MAIN','1','Test','Детская гигиена',now()),
                       ('DemoSeller','E2E-OTHER','2','Test','Гигиена',now())
                """)
            )
            await session.execute(
                text("""
                    insert into wb.feedbacks
                        (id,nm_id,valuation,text,created_at,created_date,imported_at)
                    values ('e2e-negative',999921001,2,'Протекают',:created,:day,now()),
                           ('e2e-other-negative',999921002,1,'Брак',:created,:day,now())
                    """),
                {"day": end, "created": dt.datetime.combine(end, dt.time(12), dt.UTC)},
            )
            await session.execute(
                text("""
                insert into wb.feedback_reasons(feedback_id,reason_id,model,classified_at)
                select 'e2e-negative',id,'e2e-fixture',now()
                from wb.negative_reasons where name='протекают'
                """)
            )
            await session.execute(
                text("""
                insert into wb.feedback_reasons(feedback_id,reason_id,model,classified_at)
                select 'e2e-other-negative',id,'e2e-fixture',now()
                from wb.negative_reasons where name='брак'
                """)
            )
            await session.execute(
                text("""
                    insert into wb.signals
                        (nm_id,signal_type,period_start,period_end,delta,ai_summary,created_at)
                    values (999921001,'negative_spike',:day,:day,12.5,'протекают',now())
                    """),
                {"day": end},
            )
            await session.execute(
                text("""
                    insert into wb.ai_cost_requests
                        (model,month_start,status,estimated_cost_usd,actual_cost_usd,
                         input_tokens,output_tokens,reserved_at,settled_at)
                    values ('e2e-model',:month,'settled',0.04,0.000316,30146,999,now(),now())
                    """),
                {"month": today.replace(day=1)},
            )
            await session.commit()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
