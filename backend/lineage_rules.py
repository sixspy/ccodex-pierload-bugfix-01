"""轴重血缘同源规则：续磅识别、父票定位、缺边盘点。

五页（地磅、快照、对账、过磅、血缘）一律经由本模块取数，禁止各自推导。
修复册第 16 号采用甲路径：正式票提交须同事务写入父子边，缺边即回滚拒收。
"""
from __future__ import annotations

from fastapi import HTTPException

CONTINUATION_MARK = "续"
EDGE_TYPE_CONTINUATION = "续磅"


def is_continuation_plate(plate: str | None) -> bool:
    """车牌含「续」标记即为续磅票，必须存在正式入边。"""
    return CONTINUATION_MARK in (plate or "")


def base_plate(plate: str | None) -> str:
    """去掉续磅标记后的原始车牌，用于定位父票。"""
    return (plate or "").replace(CONTINUATION_MARK, "").strip()


def find_parent_ticket(conn, plate: str | None):
    """按原始车牌定位最近一张正式父票；找不到返回 None。"""
    base = base_plate(plate)
    if not base:
        return None
    return conn.execute(
        """SELECT id, ticket_code, vehicle_plate, status, bl_ref
           FROM weigh_tickets
           WHERE vehicle_plate=? AND status <> '作废'
           ORDER BY id DESC LIMIT 1""",
        (base,),
    ).fetchone()


def has_inbound_edge(conn, ticket_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM axle_lineage WHERE to_ticket_id=? LIMIT 1",
            (ticket_id,),
        ).fetchone()
        is not None
    )


def ticket_lineage_ok(conn, ticket) -> bool:
    """单票血统是否齐全：非续磅票无需入边；续磅票必须有正式入边。"""
    if not is_continuation_plate(ticket["vehicle_plate"]):
        return True
    return has_inbound_edge(conn, int(ticket["id"]))


def missing_lineage_tickets(
    conn,
    bl_code: str | None = None,
    bridge_id: int | None = None,
) -> list[dict]:
    """有票无边：续磅票存在而轴重血缘边表无父子衔接行。"""
    sql = """
        SELECT t.id, t.ticket_code, t.vehicle_plate, t.bl_ref, t.lane_id,
               t.status, l.lane_code, b.bridge_code
        FROM weigh_tickets t
        JOIN weigh_lanes l ON l.id = t.lane_id
        JOIN weigh_bridges b ON b.id = l.bridge_id
        WHERE t.status <> '作废'
          AND instr(t.vehicle_plate, ?) > 0
          AND NOT EXISTS (
              SELECT 1 FROM axle_lineage e WHERE e.to_ticket_id = t.id
          )
    """
    args: list = [CONTINUATION_MARK]
    if bl_code is not None:
        sql += " AND t.bl_ref = ?"
        args.append(bl_code)
    if bridge_id is not None:
        sql += " AND b.id = ?"
        args.append(bridge_id)
    sql += " ORDER BY t.id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def lineage_health(
    conn,
    bl_code: str | None = None,
    bridge_id: int | None = None,
) -> dict:
    missing = missing_lineage_tickets(conn, bl_code=bl_code, bridge_id=bridge_id)
    return {
        "complete": not missing,
        "missing_count": len(missing),
        "missing": missing,
    }


def ensure_ticket_lineage_complete(conn, ticket) -> None:
    """快照/封闸前闸门：续磅票缺父子边即拒收，整笔回滚。"""
    if not ticket_lineage_ok(conn, ticket):
        raise HTTPException(409, "续磅票缺父子血缘边，拒绝封闸")
