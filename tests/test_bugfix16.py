"""漏边修复第16号：过磅提交须同事务写正式父子血缘边（甲路径）。

三红：
  甲  test_16a_formal_ticket_edge_missing_is_red
  乙  test_16b_shadow_without_promoted_edge_is_not_formal
  争用 test_16c_concurrent_same_parent_continued_weighing_is_serialized

夹具：本机种子库（init_db）+ 有票无边假绿夹具。
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import db
import tickets_svc
from app import app
from db import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin", password: str = "admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _lane(c: TestClient, h: dict, code: str) -> dict:
    lanes = c.get("/api/lanes", headers=h).json()["items"]
    return next(x for x in lanes if x["lane_code"] == code)


def _ticket_by_code(c: TestClient, h: dict, code: str) -> dict:
    tickets = c.get("/api/tickets", headers=h).json()["items"]
    return next(t for t in tickets if t["ticket_code"] == code)


# ---------------------------------------------------------------------------
# 甲：正式票提交成功，但边表没有父子衔接行 —— 现网假绿，必须见红。
# ---------------------------------------------------------------------------
def test_16a_formal_ticket_edge_missing_is_red():
    c = TestClient(app)
    h = _auth(c)
    lane = _lane(c, h, "LN-N-01-IN")
    parent = _ticket_by_code(c, h, "WT-LN-N-01-IN-001")

    resp = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": lane["id"],
            "bl_ref": "BL-2026-0101",
            "vehicle_plate": "沪A·集8821续",
            "axle_weights": [{"axle": 1, "w": 8.0}, {"axle": 2, "w": 7.0}],
            "ticket_code": "WT-C16-A",
        },
    )
    assert resp.status_code == 200, resp.text
    child_id = resp.json()["id"]

    with db.connect() as conn:
        edge = conn.execute(
            "SELECT * FROM axle_lineage WHERE to_ticket_id=?", (child_id,)
        ).fetchone()
    assert edge is not None, "正式票已提交成功，轴重血缘边表却为空（漏边）"
    assert edge["from_ticket_id"] == parent["id"]
    assert edge["edge_type"] == "续磅"

    out = c.get(f"/api/lineage/ticket/{parent['id']}", headers=h).json()
    assert any(e["to_ticket_id"] == child_id for e in out["outbound"]), "血缘页断链"


# ---------------------------------------------------------------------------
# 乙：影子边尚未晋升，血缘页/对账页不得把它标成正式、血统齐全。
# 夹具：有票无边（票已正式，边表零行）。
# ---------------------------------------------------------------------------
def test_16b_shadow_without_promoted_edge_is_not_formal():
    c = TestClient(app)
    h = _auth(c)
    lane = _lane(c, h, "LN-N-01-IN")
    shadow_code = "WT-C16-SHADOW"
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
               axle_weights,gross_t,generation,status,adjudicated_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                shadow_code,
                lane["id"],
                "BL-2026-0101",
                "影A·续16",
                json.dumps([{"axle": 1, "w": 9.0}, {"axle": 2, "w": 9.0}]),
                18.0,
                1,
                "已裁定",
                db.now_iso(),
                db.now_iso(),
            ),
        )
        conn.commit()
        shadow_id = cur.lastrowid
        assert conn.execute(
            "SELECT id FROM axle_lineage WHERE to_ticket_id=?", (shadow_id,)
        ).fetchone() is None, "夹具须为有票无边"

    recon = c.get("/api/reconcile/BL-2026-0101", headers=h).json()
    assert recon["lineage_complete"] is False, "影子边未晋升却被对账当作血统齐全"
    assert recon["proof_ready"] is False
    assert shadow_code in [x["ticket_code"] for x in recon["lineage_missing"]]

    lg = c.get("/api/lineage", headers=h).json()
    assert lg["complete"] is False, "影子边未晋升却被血缘页误标正式"
    assert shadow_code in [x["ticket_code"] for x in lg["missing"]]


# ---------------------------------------------------------------------------
# 争用：两路并发同父票抢续磅，必须占票锁；败方只记争用痕，不得双成。
# 打桩点：胜方父票锁提交之后、业务事务之前暂停，此时锁已对败方可见。
# ---------------------------------------------------------------------------
def test_16c_concurrent_same_parent_continued_weighing_is_serialized(
    monkeypatch,
):
    c = TestClient(app)
    h_admin = _auth(c, "admin", "admin123")
    h_writer = _auth(c, "writer", "writer123")
    parent = _ticket_by_code(c, h_admin, "WT-LN-N-01-IN-001")
    lane = _lane(c, h_admin, "LN-N-01-IN")
    lock_key = f"ticket:{parent['id']}"

    orig_connect = tickets_svc.connect
    winner_holding = threading.Event()
    release_winner = threading.Event()
    lock = threading.Lock()
    pause_state = {"paused": False}

    class BlockingConn:
        """仅在胜方父票锁提交后暂停；上下文管理连接（业务事务）不拦。"""

        def __init__(self, inner):
            self._inner = inner
            self._entered = False

        def __enter__(self):
            self._entered = True
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def commit(self):
            self._inner.commit()
            if self._entered:
                return
            held = self._inner.execute(
                "SELECT holder FROM pier_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            if not held:
                return
            with lock:
                if pause_state["paused"]:
                    return
                pause_state["paused"] = True
            winner_holding.set()
            release_winner.wait(10)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __setattr__(self, name, value):
            if name in ("_inner", "_entered"):
                object.__setattr__(self, name, value)
            else:
                setattr(self._inner, name, value)

    def blocking_connect():
        return BlockingConn(orig_connect())

    monkeypatch.setattr(tickets_svc, "connect", blocking_connect)

    results: dict[str, tuple[int, dict]] = {}

    def worker(name: str, headers: dict, plate: str) -> None:
        client = TestClient(app)
        r = client.post(
            "/api/tickets",
            headers=headers,
            json={
                "lane_id": lane["id"],
                "bl_ref": "BL-2026-0101",
                "vehicle_plate": plate,
                "axle_weights": [{"axle": 1, "w": 8.0}, {"axle": 2, "w": 7.0}],
                "ticket_code": f"WT-C16-{name.upper()}",
                "parent_ticket_id": parent["id"],
            },
        )
        results[name] = (r.status_code, r.json())

    t_admin = threading.Thread(target=worker, args=("admin", h_admin, "竞A·续16"))
    t_writer = threading.Thread(target=worker, args=("writer", h_writer, "竞B·续16"))
    t_admin.start()
    assert winner_holding.wait(5), "胜方未取得父票锁"
    t_writer.start()
    try:
        deadline = time.time() + 5
        while len(results) < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert len(results) == 1, "败方未被父票锁即时拦下"
        loser_name, (loser_status, loser_body) = next(iter(results.items()))
        assert loser_status == 409, f"败方未被票锁拦下，出现抢建双成：{loser_body}"
    finally:
        release_winner.set()
        t_admin.join(10)
        t_writer.join(10)

    assert sorted(s for s, _ in results.values()) == [200, 409], results
    assert len(results) == 2

    winner_name = next(n for n in results if n != loser_name)
    with db.connect() as conn:
        children = conn.execute(
            "SELECT id, ticket_code FROM weigh_tickets WHERE ticket_code IN (?,?)",
            ("WT-C16-ADMIN", "WT-C16-WRITER"),
        ).fetchall()
        assert len(children) == 1, "同父票并发续磅不得双成"
        assert children[0]["ticket_code"] == f"WT-C16-{winner_name.upper()}"
        assert (
            conn.execute(
                "SELECT id FROM weigh_tickets WHERE ticket_code=?",
                (f"WT-C16-{loser_name.upper()}",),
            ).fetchone()
            is None
        ), "败方不得写业务票行"

        edges = conn.execute(
            "SELECT * FROM axle_lineage WHERE to_ticket_id=?", (children[0]["id"],)
        ).fetchall()
        assert len(edges) == 1, "胜方须有且仅有一条正式父子边"
        assert edges[0]["from_ticket_id"] == parent["id"]

        contention = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=?", (lock_key,)
        ).fetchall()
    assert contention, "败方未记争用痕"
    assert any(row["actor"] == loser_name for row in contention)
    assert any(row["action"] == "争用" for row in contention)
