# stock_router.py
# Mounted at /qty by gateway.py
# Endpoints:
#   POST /qty/check   -> grouped results by input key
#   POST /qty/list    -> alias, returns same grouped shape
#   POST /qty/one     -> alias, returns first match for a single key

import os
import xmlrpc.client
from functools import lru_cache
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from dotenv import dotenv_values
from pathlib import Path

# ---- Load .env robustly from this folder (handles BOM/whitespace) ----
BASE_DIR = Path(__file__).resolve().parent
_raw = dotenv_values(str(BASE_DIR / ".env")) or {}
env = {}
for k, v in _raw.items():
    nk = (k or "")
    nk = nk.lstrip("\ufeff").strip()  # strip UTF-8 BOM + spaces
    env[nk] = (v.strip() if isinstance(v, str) else v)

def need(k: str) -> str:
    v = (env.get(k) or "").strip()
    if not v:
        raise RuntimeError(f"Missing Odoo creds in .env ({k})")
    return v

ODOO_URL = need("ODOO_URL").rstrip("/")
ODOO_DB = need("ODOO_DB")
ODOO_USER = need("ODOO_USER")
ODOO_PASSWORD = need("ODOO_PASSWORD")

class OdooClient:
    def __init__(self, url: str, db: str, user: str, password: str) -> None:
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.password = password
        self.common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        self.uid = self.common.authenticate(self.db, self.user, self.password, {})
        if not self.uid:
            raise RuntimeError("Odoo auth failed")
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)

    def search_read(self, model: str, domain: list, fields: list, limit: Optional[int] = None) -> List[dict]:
        return self.models.execute_kw(
            self.db, self.uid, self.password,
            model, "search_read",
            [domain],
            {"fields": fields, "limit": limit or 0}
        )

    def read(self, model: str, ids: List[int], fields: list) -> List[dict]:
        return self.models.execute_kw(self.db, self.uid, self.password, model, "read", [ids, fields])

client = OdooClient(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)

@lru_cache(maxsize=1)
def internal_location_ids() -> List[int]:
    locs = client.search_read("stock.location", [("usage", "=", "internal")], ["id"], 0)
    return [r["id"] for r in locs]

def _uom_name(uom_field: Any) -> str:
    if isinstance(uom_field, (list, tuple)) and len(uom_field) == 2:
        return str(uom_field[1])
    if isinstance(uom_field, int):
        rec = client.read("uom.uom", [uom_field], ["name"])
        if rec:
            return rec[0].get("name") or ""
    return ""

# ====== models ======
class QtyCheckReq(BaseModel):
    identifiers: List[str]
    mode: str = "auto"   # "auto" | "default_code" | "barcode"

class QtyItem(BaseModel):
    id: int
    display_name: str
    default_code: Optional[str] = None
    barcode: Optional[str] = None
    qty_available: float
    reserved_qty: float
    free_qty: float
    uom_name: Optional[str] = None

class QtyPerInput(BaseModel):
    input: str
    matches: List[QtyItem]

class QtyCheckResp(BaseModel):
    ok: bool
    items: List[QtyPerInput]

router = APIRouter()

def _lookup_products(keys: List[str], mode: str) -> Dict[str, List[dict]]:
    keys = [s.strip() for s in keys if str(s).strip()]
    if not keys:
        return {}

    if mode == "default_code":
        domain = [("default_code", "in", keys)]
    elif mode == "barcode":
        domain = [("barcode", "in", keys)]
    else:
        domain = ["|", ("default_code", "in", keys), ("barcode", "in", keys)]

    prods = client.search_read(
        "product.product",
        domain,
        ["display_name", "default_code", "barcode", "qty_available", "uom_id"],
        0,
    )

    # group by requested key (preserve order of keys)
    grouped: Dict[str, List[dict]] = {k: [] for k in keys}
    for p in prods:
        dc = (p.get("default_code") or "").strip()
        bc = (p.get("barcode") or "").strip()
        for k in keys:
            if (mode in ("auto", "default_code") and dc and k == dc) or \
               (mode in ("auto", "barcode") and bc and k == bc):
                grouped[k].append(p)
    return grouped

@router.post("/check", response_model=QtyCheckResp)
def qty_check(req: QtyCheckReq):
    if not req.identifiers:
        raise HTTPException(status_code=400, detail="identifiers must be non-empty")
    keys = [s.strip() for s in req.identifiers if str(s).strip()]
    if not keys:
        raise HTTPException(status_code=400, detail="identifiers empty after trim")

    grouped = _lookup_products(keys, req.mode)

    # reserved per product (internal locations only)
    prod_ids = {p["id"] for arr in grouped.values() for p in arr}
    reserved_by_prod: Dict[int, float] = {pid: 0.0 for pid in prod_ids}
    if prod_ids:
        quants = client.search_read(
            "stock.quant",
            [("product_id", "in", list(prod_ids)), ("location_id", "in", internal_location_ids())],
            ["product_id", "reserved_quantity"],
            0,
        )
        for q in quants:
            pid = q["product_id"][0] if isinstance(q["product_id"], (list, tuple)) else q["product_id"]
            reserved_by_prod[pid] = reserved_by_prod.get(pid, 0.0) + float(q.get("reserved_quantity") or 0.0)

    items: List[QtyPerInput] = []
    for k in keys:  # keep user order
        matches: List[QtyItem] = []
        for p in grouped.get(k, []):
            pid = p["id"]
            qty = float(p.get("qty_available") or 0.0)
            resv = float(reserved_by_prod.get(pid, 0.0))
            free = max(qty - resv, 0.0)
            matches.append(QtyItem(
                id=pid,
                display_name=p.get("display_name") or f"Product {pid}",
                default_code=p.get("default_code"),
                barcode=p.get("barcode"),
                qty_available=qty,
                reserved_qty=resv,
                free_qty=free,
                uom_name=_uom_name(p.get("uom_id")),
            ))
        items.append(QtyPerInput(input=k, matches=matches))

    return QtyCheckResp(ok=True, items=items)

# ---------- Backward-compatible aliases ----------

class OneReq(BaseModel):
    identifier: str
    mode: str = "auto"

@router.post("/one")
def qty_one(req: OneReq):
    resp = qty_check(QtyCheckReq(identifiers=[req.identifier], mode=req.mode))
    # pick first match (if any)
    prod: Optional[dict] = None
    if resp.items and resp.items[0].matches:
        # convert pydantic object to dict
        prod = resp.items[0].matches[0].model_dump()
    return {"ok": True, "product": prod}

class ListReq(BaseModel):
    identifiers: List[str]
    mode: str = "auto"

@router.post("/list")
def qty_list(req: ListReq):
    resp = qty_check(QtyCheckReq(identifiers=req.identifiers, mode=req.mode))
    # flatten to a simple list for legacy callers
    flat: List[dict] = []
    for grp in resp.items:
        flat.extend([m.model_dump() for m in grp.matches])
    return {"ok": True, "items": [m.model_dump() for grp in resp.items for m in grp.matches]}

