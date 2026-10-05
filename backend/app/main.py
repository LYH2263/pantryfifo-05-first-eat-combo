import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect, write_tx
from app.engines.fefo import consume_fefo, consume_group_fefo, expire_lots

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    with write_tx(c):
        cur = c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
            (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
        lid = cur.lastrowid
    c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

def _eligible_lots(c, item_ids: list[int]) -> dict:
    """Live on-shelf, positive, non-dirty lots grouped by item — the only stock any eat-path may deduct."""
    if not item_ids:
        return {}
    marks = ",".join("?" for _ in item_ids)
    q = f"""SELECT * FROM lots WHERE status='on_shelf' AND qty_remain>0
            AND (data_quality IS NULL OR data_quality!='dirty') AND item_id IN ({marks})"""
    out = {}
    for r in c.execute(q, item_ids):
        out.setdefault(r["item_id"], []).append(dict(r))
    return out

def _apply_deductions(c, deductions: list[dict]):
    for d in deductions:
        c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))

@app.post("/api/consume")
def consume(body: ConsumeIn):
    if body.qty <= 0:
        raise HTTPException(400, "qty_non_positive")
    c = connect()
    try:
        with write_tx(c):
            # Recompute against live rows under the write lock so a concurrent
            # consume/combo/sweep can never make this response overstate stock.
            lots = [dict(r) for r in c.execute(
                "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0 "
                "AND (data_quality IS NULL OR data_quality!='dirty')", (body.item_id,))]
            result = consume_fefo(lots, body.qty)
            if not result["ok"]:
                raise HTTPException(409, result)
            _apply_deductions(c, result["deductions"])
            c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                      (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        return result
    finally:
        c.close()

class ComboItemIn(BaseModel):
    item_id: int
    qty: float

class ComboIn(BaseModel):
    items: list[ComboItemIn]
    allow_partial: bool = False
    note: str = ""

def _validate_requests(c, reqs: list[ComboItemIn]):
    cleaned = []
    for r in reqs:
        if r.qty <= 0:
            raise HTTPException(400, f"qty_non_positive: item {r.item_id}")
        cleaned.append({"item_id": r.item_id, "qty": r.qty})
    if not cleaned:
        raise HTTPException(400, "empty_combo")
    ids = [r["item_id"] for r in cleaned]
    known = {row["id"] for row in c.execute(
        f"SELECT id FROM items WHERE id IN ({','.join('?' for _ in ids)})", ids)}
    missing = [i for i in ids if i not in known]
    if missing:
        raise HTTPException(404, f"unknown item: {missing}")
    return cleaned

def _names(c, item_ids) -> dict:
    if not item_ids: return {}
    return {r["id"]: r["name"] for r in c.execute(
        f"SELECT id,name FROM items WHERE id IN ({','.join('?' for _ in item_ids)})", list(item_ids))}

def _enrich(c, result: dict, requests: list[dict]) -> dict:
    names = _names(c, [r["item_id"] for r in requests])
    for it in result["items"]:
        it["name"] = names.get(it["item_id"])
    for s in result["short_items"]:
        s["name"] = names.get(s["item_id"])
    return result

@app.post("/api/combo/preview")
def combo_preview(body: ComboIn):
    """Plan multi-item FEFO deductions against current stock. Never writes lots."""
    c = connect()
    try:
        reqs = _validate_requests(c, body.items)
        result = consume_group_fefo(reqs, _eligible_lots(c, [r["item_id"] for r in reqs]),
                                    allow_partial=body.allow_partial)
        result["warn_days"] = int(c.execute(
            "SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
        return _enrich(c, result, reqs)
    finally:
        c.close()

@app.post("/api/combo/confirm")
def combo_confirm(body: ComboIn):
    c = connect()
    try:
        with write_tx(c):
            reqs = _validate_requests(c, body.items)
            # Plan is rebuilt from locked live rows: whatever a bypass consume or
            # off-shelf sweep did first is already visible here.
            result = consume_group_fefo(reqs, _eligible_lots(c, [r["item_id"] for r in reqs]),
                                        allow_partial=body.allow_partial)
            if not result["ok"] or not result["deductions"]:
                # All-or-nothing short, or partial mode where no item is fulfillable:
                # there is no single deduction to persist, so nothing may be written.
                raise HTTPException(409, _enrich(c, result, reqs))
            _apply_deductions(c, result["deductions"])
            result = _enrich(c, result, reqs)
            warn_days = int(c.execute(
                "SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
            created_at = datetime.now(timezone.utc).isoformat()
            # Immutable confirmed snapshot; later warn_days edits never touch it.
            snapshot = {"items": result["items"], "short_items": result["short_items"],
                        "deductions": result["deductions"], "allow_partial": body.allow_partial,
                        "reason": result["reason"], "warn_days": warn_days, "created_at": created_at}
            cur = c.execute("INSERT INTO combos(result_json,warn_days,created_at) VALUES (?,?,?)",
                            (json.dumps(snapshot), warn_days, created_at))
            combo_id = cur.lastrowid
            # Remaining quantities read back inside the same lock: the response
            # and the layer page must agree on this single outcome.
            result["combo_id"] = combo_id
            result["warn_days"] = warn_days
            result["lots_remaining"] = {}
            for d in result["deductions"]:
                row = c.execute("SELECT id,item_id,qty_remain,status FROM lots WHERE id=?",
                                (d["lot_id"],)).fetchone()
                result["lots_remaining"][d["lot_id"]] = dict(row)
        return result
    finally:
        c.close()

@app.get("/api/combos")
def combos():
    """Confirmed combo deduction snapshots (audit trail)."""
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM combos ORDER BY id")]
    for r in rows:
        r["result"] = json.loads(r.pop("result_json"))
    c.close(); return rows

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    try:
        with write_tx(c):
            lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
            ids = expire_lots(lots, date.today().isoformat())
            for i in ids:
                c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
        return {"expired_ids": ids}
    finally:
        c.close()

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

class SettingsIn(BaseModel):
    warn_days: int

@app.patch("/api/settings")
def update_settings(body: SettingsIn):
    if body.warn_days < 0:
        raise HTTPException(400, "warn_days must be >= 0")
    c = connect()
    try:
        with write_tx(c):
            c.execute("UPDATE settings SET value=? WHERE key='warn_days'", (str(body.warn_days),))
        return {"warn_days": body.warn_days}
    finally:
        c.close()
