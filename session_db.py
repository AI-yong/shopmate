"""세션·에이전트 상태 저장 계층 (업무 DB 와 분리).

왜 분리했나

  매 요청마다 쓰이고, 날아가도 되고, 백업이 필요 없고, 수명이 분~시간이다.
  주문·재고와 잠금·백업·마이그레이션을 공유할 이유가 없다.

어떻게 담나

  pending         ->  열로 뺀다. 블롭 하나에 넣으면 "지금 승인 대기 중인 사용자 수"
                      조차 셀 수 없다. 나머지는 rest 에.
  선호            ->  여기 없다. 사용자가 저장해 달라고 한 것이라 업무 DB 의
                      user_preferences 에 둔다 (server.Session).
  만료 판단        ->  expires_at 열. 코드가 읽을 때 판단하는 것 외에, 행으로도
                      지울 수 있다 (purge_expired). 안 지우면 죽은 행이 계속 쌓인다.

agent.snapshot() 이 주는 dict 를 그대로 받고 그대로 돌려준다.
"""

from __future__ import annotations

import json
import uuid

import psycopg
from psycopg.types.json import Jsonb

import config

SESSION_DSN = config.APP_SESSION_DSN   # 웹 서버 전용 최소 권한 계정 (config.py)

# snapshot() 의 키 중 열로 뺀 것. 나머지는 rest 에 담는다.
_COLUMNS = ("pending",)
# rest 에 넣지 않는 키. turn 은 정수 열로 따로 저장하고, preferences 는 세션 DB 에
# 두지 않는다 — snapshot() 에 들어 있어도 버린다 (원본은 업무 DB 의 user_preferences).
_NOT_HERE = ("turn", "preferences")


def connect(dsn=None):
    conn = psycopg.connect(dsn or SESSION_DSN)
    conn.execute("SET lock_timeout = '5s'")
    conn.commit()
    return conn


def save_agent_state(conn, user_id, state):
    """agent.snapshot() 이 준 dict 를 저장한다.

    pending 의 만료 시각을 expires_at 으로 끌어올립니다. 정책(몇 초인가, 무엇이
    만료인가)은 여전히 agent.py 가 판단하고, 여기는 그 결과를 열에 적을 뿐입니다
    — 판단하는 곳이 둘이 되면 둘은 반드시 어긋납니다.
    """
    state = json.loads(json.dumps(state, ensure_ascii=False, default=str))
    pending = state.get("pending")
    rest = {k: v for k, v in state.items() if k not in _COLUMNS and k not in _NOT_HERE}
    expires = (pending or {}).get("expires_at")

    conn.execute(
        "INSERT INTO agent_state (user_id, pending, rest,"
        " turn, expires_at, updated_at)"
        " VALUES (%s, %s, %s, %s, to_timestamp(%s), now())"
        " ON CONFLICT (user_id) DO UPDATE SET"
        " pending = excluded.pending,"
        " rest = excluded.rest,"
        " turn = excluded.turn, expires_at = excluded.expires_at,"
        " updated_at = now()",
        (user_id,
         Jsonb(pending) if pending is not None else None,
         Jsonb(rest),
         int(state.get("turn") or 0),
         float(expires) if isinstance(expires, (int, float)) else None),
    )
    conn.commit()


def load_agent_state(conn, user_id):
    """저장된 상태 dict. 없으면 None. 모양은 agent.snapshot() 과 같다 (선호는 빠진다)."""
    row = conn.execute(
        "SELECT pending, rest, turn FROM agent_state"
        " WHERE user_id = %s", (user_id,)).fetchone()
    if row is None:
        return None
    pending, rest, turn = row
    state = dict(rest or {})
    state["pending"] = pending
    state["turn"] = turn
    return state


def delete_agent_state(conn, user_id):
    conn.execute("DELETE FROM agent_state WHERE user_id = %s", (user_id,))
    conn.commit()


def purge_expired(conn):
    """만료된 승인 대기를 비우고, 오래 안 쓴 대화 상태를 지운다. 기동 시 한 번, 또는 pg_cron 으로.

    만료를 읽을 때만 판단하면 죽은 행이 계속 쌓인다. 사용자가 늘면 그대로 비용이 된다.

    예전에는 승인이 만료된 **행을 통째로** 지워서, 같은 행에 있던 대화 내역과 선호까지
    사라졌다. 이제 만료된 승인과 그 뒤에 이어질 예정이던 작업(postponed)만 비운다 —
    agent.restore() 가 만료된 승인을 되살리지 않으면서 postponed 도 버리는 것과 같다.
    대화 문맥은 따로, 마지막 사용 뒤 config.SESSION_STATE_IDLE_HOURS 가 지나면 지운다.
    반환: (비운 승인 수, 지운 행 수)
    """
    idle_hours = config.SESSION_STATE_IDLE_HOURS
    cleared = conn.execute(
        "UPDATE agent_state SET pending = NULL, expires_at = NULL,"
        " rest = rest - 'postponed'"
        " WHERE expires_at IS NOT NULL AND expires_at < now()").rowcount
    deleted = conn.execute(
        "DELETE FROM agent_state WHERE expires_at IS NULL"
        " AND updated_at < now() - %s * interval '1 hour'", (idle_hours,)).rowcount
    conn.commit()
    return cleared, deleted


def save_image_query(conn, query_id, user_id, bucket, object_key, sha256,
                     width, height, ttl_seconds):
    conn.execute("""INSERT INTO image_search_queries
        (query_id,user_id,object_bucket,object_key,content_sha256,width,height,expires_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,now()+(%s * interval '1 second'))""",
        (query_id, user_id, bucket, object_key, sha256, width, height, ttl_seconds))
    conn.commit()


def load_image_query(conn, query_id, user_id):
    """소유자가 맞고 아직 만료되지 않은 이미지 질의만 반환한다."""
    row = conn.execute("""SELECT query_id::text,object_bucket,object_key,
                          content_sha256,width,height,expires_at
        FROM image_search_queries
        WHERE query_id=%s AND user_id=%s AND expires_at>now()""",
        (query_id, user_id)).fetchone()
    if row is None:
        return None
    keys = ("query_id", "object_bucket", "object_key", "content_sha256",
            "width", "height", "expires_at")
    return dict(zip(keys, row))


def save_image_analysis(conn, query_id, user_id, items, model_name):
    """원본 이미지의 만료 시각까지만 VLM bbox 결과를 보관한다."""
    analysis_id = str(uuid.uuid4())
    cursor = conn.execute("""INSERT INTO image_analyses
        (analysis_id,query_id,user_id,items,model_name,expires_at)
        SELECT %s,query_id,user_id,%s,%s,expires_at
        FROM image_search_queries
        WHERE query_id=%s AND user_id=%s AND expires_at>now()""",
        (analysis_id, Jsonb(items), model_name, query_id, user_id))
    if cursor.rowcount != 1:
        conn.rollback()
        return None
    conn.commit()
    return analysis_id


def load_image_analysis_item(conn, analysis_id, item_id, user_id):
    """소유권·TTL을 통과한 분석에서 서버가 저장한 item_id만 반환한다."""
    row = conn.execute("""SELECT query_id::text,items,model_name
        FROM image_analyses
        WHERE analysis_id=%s AND user_id=%s AND expires_at>now()""",
        (analysis_id, user_id)).fetchone()
    if row is None:
        return None
    for item in row[1] or []:
        if item.get("item_id") == item_id:
            return {"query_image_id": row[0], "item": item, "model_name": row[2]}
    return None


def load_image_analysis(conn, analysis_id, user_id):
    """소유권·TTL을 통과한 분석 전체(아이템 목록)를 반환한다."""
    row = conn.execute("""SELECT query_id::text,items,model_name
        FROM image_analyses
        WHERE analysis_id=%s AND user_id=%s AND expires_at>now()""",
        (analysis_id, user_id)).fetchone()
    if row is None:
        return None
    return {"analysis_id": analysis_id, "query_image_id": row[0],
            "items": row[1] or [], "model_name": row[2]}


def load_image_analysis_by_query(conn, query_id, user_id, model_name=None):
    """같은 사진을 같은 모델로 이미 분석했으면 그 결과를 재사용한다.

    사용자가 같은 사진으로 대화를 이어가면 턴마다 VLM을 다시 부르게 된다.
    분석은 원본 이미지와 같은 TTL이므로 살아 있는 최신 것 하나면 충분하다.
    """
    row = conn.execute("""SELECT analysis_id::text,items,model_name
        FROM image_analyses
        WHERE query_id=%s AND user_id=%s AND expires_at>now()
          AND (%s::text IS NULL OR model_name=%s)
        ORDER BY created_at DESC LIMIT 1""",
        (query_id, user_id, model_name, model_name)).fetchone()
    if row is None:
        return None
    return {"analysis_id": row[0], "query_image_id": query_id,
            "items": row[1] or [], "model_name": row[2]}


def delete_expired_image_queries(conn):
    """MinIO 삭제에 필요한 위치를 돌려준 뒤 만료 행을 지운다."""
    rows = conn.execute("""DELETE FROM image_search_queries WHERE expires_at<=now()
        RETURNING object_bucket,object_key""").fetchall()
    conn.commit()
    return rows


def delete_user_image_queries(conn, user_id):
    rows = conn.execute("""DELETE FROM image_search_queries WHERE user_id=%s
        RETURNING object_bucket,object_key""", (user_id,)).fetchall()
    conn.commit()
    return rows
