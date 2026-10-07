"""PostgreSQL transactional outbox와 이벤트 적재 워커.

업무 변경과 함께 남겨야 하는 이벤트는 호출자가 가진 같은 connection으로
``enqueue``를 부른다. 검색 노출처럼 읽기 요청에서 생기는 이벤트는
``record_impressions``가 짧은 독립 트랜잭션으로 남긴다.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

import config


def _uuid(value: str | uuid.UUID | None = None) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value) if value else uuid.uuid4()


def enqueue(connection: psycopg.Connection, event_type: str, aggregate_type: str,
            aggregate_id: str, payload: dict[str, Any], *,
            idempotency_key: str | None = None,
            event_id: str | uuid.UUID | None = None) -> uuid.UUID:
    """현재 트랜잭션에 outbox 이벤트를 넣는다. commit은 호출자의 책임이다."""
    if not event_type or not aggregate_type or not aggregate_id:
        raise ValueError("event_type, aggregate_type, aggregate_id는 필수입니다.")
    eid = _uuid(event_id)
    key = idempotency_key or f"{event_type}:{eid}"
    connection.execute("""
        INSERT INTO outbox_events
          (event_id,event_type,aggregate_type,aggregate_id,idempotency_key,payload)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (idempotency_key) DO NOTHING
    """, (eid, event_type, aggregate_type, aggregate_id, key, Jsonb(payload)))
    return eid


def record_impressions(dsn: str, *, user_id: str, results: Iterable[dict[str, Any]],
                       source: str, model_name: str, model_version: str,
                       request_id: str | uuid.UUID | None = None,
                       recommendation_id: str | uuid.UUID | None = None,
                       experiment_id: str | None = None) -> dict[str, str]:
    """한 검색 응답의 노출 후보와 순위를 학습 가능한 계보로 남긴다."""
    request = _uuid(request_id)
    recommendation = _uuid(recommendation_id)
    rows = list(results)
    with psycopg.connect(dsn) as connection:
        for rank, row in enumerate(rows, 1):
            product_id = str(row["product_id"])
            payload = {
                "user_id": user_id,
                "request_id": str(request),
                "recommendation_id": str(recommendation),
                "experiment_id": experiment_id,
                "model_name": model_name,
                "model_version": model_version,
                "product_id": product_id,
                "rank": rank,
                "source": source,
                "score": float(row["score"]) if row.get("score") is not None else None,
            }
            enqueue(
                connection, "product_impression", "recommendation",
                str(recommendation), payload,
                idempotency_key=f"impression:{recommendation}:{product_id}:{rank}",
            )
        connection.commit()
    return {"request_id": str(request), "recommendation_id": str(recommendation)}


def record_interaction(dsn: str, *, event_type: str, user_id: str,
                       product_id: str | None = None,
                       recommendation_id: str | uuid.UUID | None = None,
                       source: str = "runtime",
                       context: dict[str, Any] | None = None) -> str:
    """상세 조회·클릭처럼 한 건짜리 행동을 outbox에 남긴다."""
    recommendation = _uuid(recommendation_id) if recommendation_id else None
    event_id = uuid.uuid4()
    payload = {
        "user_id": user_id,
        "recommendation_id": str(recommendation) if recommendation else None,
        "product_id": product_id,
        "source": source,
        **(context or {}),
    }
    aggregate_id = product_id or user_id
    with psycopg.connect(dsn) as connection:
        enqueue(connection, event_type, "product" if product_id else "user",
                aggregate_id, payload, event_id=event_id)
        connection.commit()
    return str(event_id)


def _month_bounds(moment: datetime) -> tuple[datetime, datetime, str]:
    utc = moment.astimezone(timezone.utc)
    start = datetime(utc.year, utc.month, 1, tzinfo=timezone.utc)
    if utc.month == 12:
        end = datetime(utc.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(utc.year, utc.month + 1, 1, tzinfo=timezone.utc)
    name = f"events_{utc.year:04d}_{utc.month:02d}"
    return start, end, name


def ensure_month_partition(connection: psycopg.Connection, moment: datetime) -> str:
    """이벤트 시각의 UTC 월 파티션을 이벤트 적재 전에 만든다.

    이미 있으면 아무것도 하지 않는다 — 평소에는 잠그지 않으므로 워커끼리 막히지 않는다.
    없을 때만 권고 잠금으로 한 줄로 세운다. 워커 여러 개가 같은 달 파티션을 동시에
    만들면 IF NOT EXISTS 여도 시스템 카탈로그의 유일성 경합으로 한쪽이 실패할 수 있다.
    잠금은 트랜잭션이 끝날 때 풀리고, 기다린 쪽은 IF NOT EXISTS 로 그냥 지나간다.
    """
    start, end, name = _month_bounds(moment)
    exists = connection.execute("SELECT to_regclass(%s)", (name,)).fetchone()
    if exists and exists[0] is not None:
        return name
    connection.execute("SELECT pg_advisory_xact_lock(hashtext('events_partition'))")
    # 파티션 bound는 PostgreSQL DDL 문법상 bind parameter를 받을 수 없다.
    # sql.Literal이 날짜를 안전하게 quote하고 Identifier가 테이블명을 quote한다.
    connection.execute(sql.SQL(
        "CREATE TABLE IF NOT EXISTS {} PARTITION OF events "
        "FOR VALUES FROM ({}) TO ({})"
    ).format(sql.Identifier(name), sql.Literal(start), sql.Literal(end)))
    return name


def process_batch(dsn: str = config.SHOP_DSN, batch_size: int = 200) -> int:
    """SKIP LOCKED로 여러 워커가 겹치지 않게 한 묶음을 events로 옮긴다."""
    if not 1 <= batch_size <= 5000:
        raise ValueError("batch_size는 1~5000이어야 합니다.")
    with psycopg.connect(dsn) as connection:
        rows = connection.execute("""
            SELECT event_id,event_type,payload,occurred_at
            FROM outbox_events
            WHERE processed_at IS NULL AND failed_at IS NULL AND available_at <= now()
            ORDER BY occurred_at
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        """, (batch_size,)).fetchall()
        # 이벤트 시각마다가 아니라 **달마다** 한 번. 시각이 전부 다른 200건이면 예전에는
        # 같은 달 파티션 생성을 200번 시도했다.
        for occurred_at in {_month_bounds(row[3])[2]: row[3] for row in rows}.values():
            ensure_month_partition(connection, occurred_at)
        processed = 0
        for event_id, event_type, payload, occurred_at in rows:
            try:
                # 한 이벤트가 깨져도 묶음 전체가 rollback되지 않게 savepoint를 둔다.
                with connection.transaction():
                    data = payload if isinstance(payload, dict) else json.loads(payload)
                    connection.execute("""
                        INSERT INTO events
                          (event_id,occurred_at,event_type,user_id,session_id,request_id,
                           recommendation_id,experiment_id,model_name,model_version,
                           product_id,rank,source,payload)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (event_id,occurred_at) DO NOTHING
                    """, (
                        event_id, occurred_at, event_type, data.get("user_id"),
                        data.get("session_id"), data.get("request_id"),
                        data.get("recommendation_id"), data.get("experiment_id"),
                        data.get("model_name"), data.get("model_version"),
                        data.get("product_id"), data.get("rank"), data.get("source"),
                        Jsonb(data),
                    ))
                    connection.execute("""
                        UPDATE outbox_events
                        SET processed_at=now(),attempts=attempts+1,last_error=NULL
                        WHERE event_id=%s
                    """, (event_id,))
                processed += 1
            except Exception as error:
                connection.execute("""
                    UPDATE outbox_events
                    SET attempts=attempts+1,last_error=%s,
                        available_at=now()+LEAST(300,power(2,attempts)) * interval '1 second',
                        failed_at=CASE WHEN attempts+1>=8 THEN now() ELSE NULL END
                    WHERE event_id=%s
                """, (f"{type(error).__name__}: {str(error)[:500]}", event_id))
        connection.commit()
        return processed


def run_worker(dsn: str, batch_size: int, poll_seconds: float, once: bool) -> int:
    total = 0
    while True:
        count = process_batch(dsn, batch_size)
        total += count
        if once:
            return total
        if count == 0:
            time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=config.SHOP_DSN)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds는 0보다 커야 합니다.")
    count = run_worker(args.dsn, args.batch_size, args.poll_seconds, args.once)
    if args.once:
        print(f"이벤트 {count:,}개 적재")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
