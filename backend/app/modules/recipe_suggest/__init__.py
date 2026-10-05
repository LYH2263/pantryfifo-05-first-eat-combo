"""先吃组合: multi-item eat-first combo planning.

Pure planning layer — no DB access, no mutation. The API layer loads
on-shelf lots, calls plan_combo for preview (no writes), and calls it
again inside a BEGIN IMMEDIATE transaction for confirm (all-or-nothing).

Invariants enforced here:
- Only clean lots with positive remaining qty may enter a combo
  (dirty rows like the seeded 冻饺 never land in deductions).
- The verdict is all-or-nothing: ok only when every demand line is
  fully covered, so a confirm either writes the whole group or nothing.
"""
from app.engines.fefo import consume_fefo


def combinable(lots: list[dict]) -> list[dict]:
    """Lots allowed into a combo: clean data and qty_remain > 0."""
    return [
        l for l in lots
        if l.get("data_quality", "clean") == "clean" and float(l.get("qty_remain", 0)) > 0
    ]


def plan_combo(lots_by_item: dict, demands: list[dict]) -> dict:
    """Plan FEFO deductions for each demand line against combinable lots.

    demands: [{"item_id": int, "qty": float}] (already merged/validated).
    Returns {"ok": bool, "items": [{item_id, qty, ok, reason, short, deductions}]}.
    ok is True only if every line is fully covered — the group is atomic.
    """
    items = []
    for d in demands:
        r = consume_fefo(combinable(lots_by_item.get(d["item_id"], [])), float(d["qty"]))
        items.append({"item_id": d["item_id"], "qty": float(d["qty"]), **r})
    return {"ok": all(i["ok"] for i in items), "items": items}
