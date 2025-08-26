# gateway.py
# FastAPI gateway for Odoo Product Variant Configurator
# STEP 1: /health, /config/start
# STEP 2: /config/init (list attributes & values)
# STEP 3: /config/prepare (map selections -> PTAVs & check if variant exists)
# STEP 4: Create the variant (or return the existing one)
# NEW:    /variants/of_template  (list all variants of a product template)
from typing import List, Optional, Dict, Any
import xmlrpc.client
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import dotenv_values

from stock_router import router as stock_router  # qty/check endpoints
from purchase_router import router as purchase_router  # purchase order endpoints

# -----------------------
# Load .env from THIS FOLDER (BOM-safe & robust on Windows)
# -----------------------
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

_raw = dotenv_values(str(ENV_PATH)) or {}
cfg: Dict[str, Optional[str]] = {}
for k, v in _raw.items():
    nk = (k or "")
    nk = nk.lstrip("\ufeff").strip()  # strip UTF-8 BOM + spaces
    cfg[nk] = (v.strip() if isinstance(v, str) else v)

def _require(key: str) -> str:
    val = (cfg.get(key) or "").strip()
    if not val:
        raise RuntimeError(f"Missing in .env: {key}")
    return val

ODOO_URL = _require("ODOO_URL").rstrip("/")
ODOO_DB = _require("ODOO_DB")
ODOO_USER = _require("ODOO_USER")
ODOO_PASSWORD = _require("ODOO_PASSWORD")

# -----------------------
# Odoo XML-RPC Client
# -----------------------
class OdooClient:
    def __init__(self, url: str, db: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.password = password
        self.uid: Optional[int] = None
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)

    def login(self) -> int:
        try:
            uid = self._common.authenticate(self.db, self.user, self.password, {})
        except Exception as e:
            raise RuntimeError(f"Odoo auth error: {e}")
        if not uid:
            raise RuntimeError("Odoo auth failed: bad database/user/password.")
        self.uid = uid
        return uid

    def _ensure(self):
        if self.uid is None:
            self.login()

    def execute_kw(self, model: str, method: str, args=None, kwargs=None):
        self._ensure()
        return self._models.execute_kw(
            self.db, self.uid, self.password, model, method, args or [], kwargs or {}
        )

    def search_read(
        self,
        model: str,
        domain: list,
        fields: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        order: Optional[str] = None,
    ):
        kw: Dict[str, Any] = {}
        if fields: kw["fields"] = fields
        if limit: kw["limit"] = limit
        if offset: kw["offset"] = offset
        if order: kw["order"] = order
        return self.execute_kw(model, "search_read", [domain], kw)

# -----------------------
# FastAPI app
# -----------------------
app = FastAPI(title="Odoo Variant Configurator Gateway", version="0.4")
client = OdooClient(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)

# Mount stock endpoints (/qty/check)
app.include_router(stock_router, prefix="/qty", tags=["stock"])

# Mount purchase order endpoints (/purchase/*)
app.include_router(purchase_router, prefix="/purchase", tags=["purchase"])

@app.get("/health")
def health():
    """Quick auth check against Odoo."""
    try:
        uid = client.login()
        return {"ok": True, "uid": uid}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Health check failed: {e}")

# -----------------------
# STEP 1: Search product templates (NAME ONLY) + optional `created_by` filter
# -----------------------
class StartReq(BaseModel):
    query: str
    created_by: Optional[str] = None

class Choice(BaseModel):
    id: int
    name: Optional[str] = None
    default_code: Optional[str] = None

class StartResp(BaseModel):
    ok: bool
    matches: List[Choice]
    message: Optional[str] = None

def _resolve_users(expr: str) -> List[int]:
    expr = (expr or "").strip()
    if not expr:
        return []
    rows = client.search_read(
        "res.users",
        ["|", ("name", "=", expr), ("login", "=", expr)],
        fields=["name"],
        limit=20,
    ) or []
    if not rows:
        rows = client.search_read(
            "res.users",
            [("name", "ilike", expr)],
            fields=["name"],
            limit=20,
        ) or []
    return [int(r["id"]) for r in rows]

@app.post("/config/start", response_model=StartResp)
def config_start(payload: StartReq):
    q = (payload.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="query is required")

    domain: List = [("name", "ilike", q)]
    if payload.created_by:
        uids = _resolve_users(payload.created_by)
        if not uids:
            return {"ok": True, "matches": [], "message": f"No users match '{payload.created_by}'"}
        domain.append(("create_uid", "in", uids))

    fields = ["name", "default_code"]
    try:
        rows = client.search_read("product.template", domain, fields=fields, limit=20) or []
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Odoo error: {e}")

    matches: List[Dict] = []
    for r in rows:
        dc = r.get("default_code")
        if not isinstance(dc, str) or not dc.strip():
            dc = None
        nm = r.get("name")
        if not isinstance(nm, str):
            nm = str(nm) if nm is not None else None
        matches.append({"id": r["id"], "name": nm, "default_code": dc})

    msg = "1 match" if len(matches) == 1 else f"{len(matches)} matches"
    if payload.created_by:
        msg += f" (created_by={payload.created_by})"
    return {"ok": True, "matches": matches, "message": msg}

# -----------------------
# STEP 2: Initialize configurator - list attributes & values for a template
# -----------------------
class InitReq(BaseModel):
    template_id: int

class TemplateSummary(BaseModel):
    id: int
    name: Optional[str] = None
    default_code: Optional[str] = None

class AttrValue(BaseModel):
    id: int
    name: Optional[str] = None

class AttributeBlock(BaseModel):
    attribute_id: int
    attribute_name: Optional[str] = None
    values: List[AttrValue]

class InitResp(BaseModel):
    ok: bool
    template: TemplateSummary
    attributes: List[AttributeBlock]

def _name_map(model: str, ids: List[int]) -> Dict[int, str]:
    if not ids:
        return {}
    pairs = client.execute_kw(model, "name_get", [ids]) or []
    return {int(pid): str(pname) for pid, pname in pairs}

@app.post("/config/init", response_model=InitResp)
def config_init(payload: InitReq):
    tid = int(payload.template_id)

    trows = client.search_read(
        "product.template",
        [("id", "=", tid)],
        fields=["name", "default_code"],
        limit=1,
    )
    if not trows:
        raise HTTPException(status_code=404, detail=f"product.template {tid} not found")
    trow = trows[0]
    t_name = trow.get("name")
    if not isinstance(t_name, str):
        t_name = str(t_name) if t_name is not None else None
    t_code = trow.get("default_code")
    if not isinstance(t_code, str) or not t_code.strip():
        t_code = None
    template_summary = {"id": trow["id"], "name": t_name, "default_code": t_code}

    lines = client.search_read(
        "product.template.attribute.line",
        [("product_tmpl_id", "=", tid)],
        fields=["attribute_id", "value_ids"],
        limit=200,
    ) or []

    attr_ids: List[int] = []
    all_val_ids: List[int] = []
    for ln in lines:
        a = ln.get("attribute_id")
        if isinstance(a, list) and a:
            attr_ids.append(int(a[0]))
        elif isinstance(a, int):
            attr_ids.append(a)
        v_ids = ln.get("value_ids") or []
        for x in v_ids:
            if isinstance(x, int):
                all_val_ids.append(x)

    attr_ids = sorted(set(attr_ids))
    all_val_ids = sorted(set(all_val_ids))

    attr_names = _name_map("product.attribute", attr_ids)
    val_names = _name_map("product.attribute.value", all_val_ids)

    blocks: List[AttributeBlock] = []
    for ln in lines:
        a = ln.get("attribute_id")
        if isinstance(a, list) and a:
            aid = int(a[0])
        elif isinstance(a, int):
            aid = a
        else:
            continue
        v_ids = ln.get("value_ids") or []
        vals: List[Dict] = []
        for vid in v_ids:
            if isinstance(vid, int):
                vals.append({"id": vid, "name": val_names.get(vid)})
        blocks.append(
            {
                "attribute_id": aid,
                "attribute_name": attr_names.get(aid),
                "values": vals,
            }
        )

    return {"ok": True, "template": template_summary, "attributes": blocks}

# -----------------------
# STEP 3: Prepare a variant selection (map to PTAVs + check if variant exists)
# -----------------------
class Selection(BaseModel):
    attribute_id: int
    value_id: int

class PrepareReq(BaseModel):
    template_id: int
    selections: List[Selection]
    default_code: Optional[str] = None
    barcode: Optional[str] = None

class VariantInfo(BaseModel):
    id: int
    display_name: Optional[str] = None
    default_code: Optional[str] = None
    barcode: Optional[str] = None
    active: Optional[bool] = True

class EnrichedSelection(BaseModel):
    attribute_id: int
    attribute_name: Optional[str] = None
    value_id: int
    value_name: Optional[str] = None
    ptav_id: Optional[int] = None

class PrepareResp(BaseModel):
    ok: bool
    template: TemplateSummary
    selections: List[EnrichedSelection]
    ptav_ids: List[int]
    existing_variant: Optional[VariantInfo] = None
    message: Optional[str] = None

def _model_fields(model: str) -> Dict[str, Dict]:
    return client.execute_kw(model, "fields_get", [], {"attributes": ["type"]}) or {}

def _ptav_ids_for_values(template_id: int, value_ids: List[int]) -> Dict[int, int]:
    fields = _model_fields("product.template.attribute.value")
    fav = "product_attribute_value_id" if "product_attribute_value_id" in fields else (
        "attribute_value_id" if "attribute_value_id" in fields else None
    )
    if not fav:
        raise HTTPException(status_code=500, detail="Cannot locate attribute-value field on product.template.attribute.value")

    rows = client.search_read(
        "product.template.attribute.value",
        [("product_tmpl_id", "=", template_id), (fav, "in", value_ids)],
        fields=[fav],
        limit=500,
    ) or []

    mapping: Dict[int, int] = {}
    for r in rows:
        raw = r.get(fav)
        if isinstance(raw, list) and raw:
            pav_id = int(raw[0])
        elif isinstance(raw, int):
            pav_id = raw
        else:
            continue
        mapping[pav_id] = int(r["id"])
    return mapping

def _find_variant_by_ptav_set(template_id: int, ptav_ids: List[int]) -> Optional[Dict]:
    if not ptav_ids:
        return None
    prods = client.search_read(
        "product.product",
        [("product_tmpl_id", "=", template_id), ("active", "in", [True, False])],
        fields=["display_name", "default_code", "barcode", "product_template_attribute_value_ids", "active"],
        limit=2000,
    ) or []

    want = set(int(x) for x in ptav_ids)
    for p in prods:
        have = set(int(x) for x in (p.get("product_template_attribute_value_ids") or []))
        if have == want:
            return {
                "id": int(p["id"]),
                "display_name": p.get("display_name"),
                "default_code": (p.get("default_code") or None),
                "barcode": (p.get("barcode") or None),
                "active": bool(p.get("active", True)),
            }
    return None

@app.post("/config/prepare", response_model=PrepareResp)
def config_prepare(payload: PrepareReq):
    tid = int(payload.template_id)

    trows = client.search_read(
        "product.template",
        [("id", "=", tid)],
        fields=["name", "default_code"],
        limit=1,
    )
    if not trows:
        raise HTTPException(status_code=404, detail=f"product.template {tid} not found")
    trow = trows[0]
    t_name = trow.get("name")
    if not isinstance(t_name, str):
        t_name = str(t_name) if t_name is not None else None
    t_code = trow.get("default_code")
    if not isinstance(t_code, str) or not t_code.strip():
        t_code = None
    template_summary = {"id": trow["id"], "name": t_name, "default_code": t_code}

    value_ids = [int(s.value_id) for s in payload.selections]
    attr_ids  = [int(s.attribute_id) for s in payload.selections]

    attr_names = _name_map("product.attribute", attr_ids)
    val_names  = _name_map("product.attribute.value", value_ids)

    pav_to_ptav = _ptav_ids_for_values(tid, value_ids)
    enriched: List[Dict] = []
    ptav_ids: List[int] = []
    for s in payload.selections:
        pav_id = int(s.value_id)
        ptav_id = pav_to_ptav.get(pav_id)
        if ptav_id:
            ptav_ids.append(ptav_id)
        enriched.append(
            {
                "attribute_id": int(s.attribute_id),
                "attribute_name": attr_names.get(int(s.attribute_id)),
                "value_id": pav_id,
                "value_name": val_names.get(pav_id),
                "ptav_id": ptav_id,
            }
        )

    existing = _find_variant_by_ptav_set(tid, ptav_ids)
    msg = ("Exact variant already exists." if existing
           else "No exact variant exists yet. Ready to create in the next step.")
    return {
        "ok": True,
        "template": template_summary,
        "selections": enriched,
        "ptav_ids": ptav_ids,
        "existing_variant": existing,
        "message": msg,
    }

# -----------------------
# STEP 4: Create the variant (or return the existing one)
# -----------------------
class CreateReq(BaseModel):
    template_id: int
    selections: List[Selection]
    default_code: Optional[str] = None
    barcode: Optional[str] = None

class CreateResp(BaseModel):
    ok: bool
    created: bool
    variant: 'VariantInfo'
    message: Optional[str] = None

@app.post("/config/create", response_model=CreateResp)
def config_create(payload: CreateReq):
    tid = int(payload.template_id)

    trows = client.search_read(
        "product.template",
        [("id", "=", tid)],
        fields=["name"],
        limit=1,
    )
    if not trows:
        raise HTTPException(status_code=404, detail=f"product.template {tid} not found")

    value_ids = [int(s.value_id) for s in payload.selections]

    pav_to_ptav = _ptav_ids_for_values(tid, value_ids)
    missing = [v for v in value_ids if v not in pav_to_ptav]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Selected values not on template {tid}: {missing}"
        )
    ptav_ids = [pav_to_ptav[v] for v in value_ids]

    existing = _find_variant_by_ptav_set(tid, ptav_ids)
    if existing:
        return {
            "ok": True,
            "created": False,
            "variant": existing,
            "message": "Exact variant already exists. You can use it or change selections.",
        }

    vals = {
        "product_tmpl_id": tid,
        "product_template_attribute_value_ids": [(6, 0, ptav_ids)],
    }
    if payload.default_code:
        vals["default_code"] = payload.default_code
    if payload.barcode:
        vals["barcode"] = payload.barcode

    try:
        new_id = client.execute_kw("product.product", "create", [vals])
    except Exception as e:
        fallback = _find_variant_by_ptav_set(tid, ptav_ids)
        if fallback:
            return {
                "ok": True,
                "created": False,
                "variant": fallback,
                "message": "Variant already exists (detected during create).",
            }
        raise HTTPException(status_code=502, detail=f"Odoo create error: {e}")

    v = client.search_read(
        "product.product",
        [("id", "=", new_id)],
        fields=["display_name", "default_code", "barcode", "active"],
        limit=1,
    ) or []
    info = {
        "id": new_id,
        "display_name": v[0].get("display_name") if v else None,
        "default_code": (v[0].get("default_code") if v else payload.default_code) or None,
        "barcode": (v[0].get("barcode") if v else payload.barcode) or None,
        "active": bool(v[0].get("active", True)) if v else True,
    }

    return {
        "ok": True,
        "created": True,
        "variant": info,
        "message": "Variant created.",
    }

# -----------------------
# NEW: List all variants of a template
# -----------------------
class VariantsOfTemplateReq(BaseModel):
    template_id: int
    active_only: bool = True  # include archived if set False

class VariantValuePair(BaseModel):
    attribute: Optional[str] = None
    value: Optional[str] = None

class VariantRow(BaseModel):
    id: int
    display_name: Optional[str] = None
    default_code: Optional[str] = None
    barcode: Optional[str] = None
    qty_on_hand: float = 0.0
    values: List[VariantValuePair] = []

class VariantsOfTemplateResp(BaseModel):
    ok: bool
    template: TemplateSummary
    count: int
    variants: List[VariantRow]

@app.post("/variants/of_template", response_model=VariantsOfTemplateResp, tags=["variants"])
def variants_of_template(payload: VariantsOfTemplateReq):
    tid = int(payload.template_id)

    trows = client.search_read(
        "product.template",
        [("id", "=", tid)],
        fields=["name", "default_code"],
        limit=1,
    )
    if not trows:
        raise HTTPException(status_code=404, detail=f"product.template {tid} not found")
    trow = trows[0]
    t_name = trow.get("name")
    if not isinstance(t_name, str):
        t_name = str(t_name) if t_name is not None else None
    t_code = trow.get("default_code")
    if not isinstance(t_code, str) or not t_code.strip():
        t_code = None
    template_summary = {"id": trow["id"], "name": t_name, "default_code": t_code}

    domain = [("product_tmpl_id", "=", tid)]
    if payload.active_only:
        domain.append(("active", "=", True))

    prods = client.search_read(
        "product.product",
        domain,
        fields=[
            "display_name",
            "default_code",
            "barcode",
            "qty_available",
            "product_template_attribute_value_ids",
            "active",
        ],
        limit=2000,
    ) or []

    # collect PTAV ids across results
    all_ptav_ids = set()
    for p in prods:
        for x in p.get("product_template_attribute_value_ids") or []:
            all_ptav_ids.add(int(x))
    all_ptav_ids = sorted(all_ptav_ids)

    ptavs = []
    ptav_to_attr: Dict[int, int] = {}
    ptav_to_pav: Dict[int, int] = {}
    attr_ids, pav_ids = set(), set()

    if all_ptav_ids:
        fields = _model_fields("product.template.attribute.value")
        fav = "product_attribute_value_id" if "product_attribute_value_id" in fields else (
            "attribute_value_id" if "attribute_value_id" in fields else None
        )
        if not fav:
            raise HTTPException(status_code=500, detail="PTAV field not found")

        ptavs = client.search_read(
            "product.template.attribute.value",
            [("id", "in", all_ptav_ids)],
            fields=[fav, "attribute_id"],
            limit=5000,
        ) or []

        for r in ptavs:
            ptav_id = int(r["id"])
            a_raw = r.get("attribute_id")
            if isinstance(a_raw, list) and a_raw:
                aid = int(a_raw[0])
            elif isinstance(a_raw, int):
                aid = a_raw
            else:
                continue
            v_raw = r.get(fav)
            if isinstance(v_raw, list) and v_raw:
                vid = int(v_raw[0])
            elif isinstance(v_raw, int):
                vid = v_raw
            else:
                continue
            ptav_to_attr[ptav_id] = aid
            ptav_to_pav[ptav_id] = vid
            attr_ids.add(aid)
            pav_ids.add(vid)

    attr_names = _name_map("product.attribute", sorted(attr_ids)) if attr_ids else {}
    val_names  = _name_map("product.attribute.value", sorted(pav_ids)) if pav_ids else {}

    rows: List[VariantRow] = []
    for p in prods:
        ptav_ids = [int(x) for x in (p.get("product_template_attribute_value_ids") or [])]
        pairs: List[VariantValuePair] = []
        for ptav in ptav_ids:
            aid = ptav_to_attr.get(ptav)
            vid = ptav_to_pav.get(ptav)
            if aid and vid:
                pairs.append(VariantValuePair(
                    attribute=attr_names.get(aid),
                    value=val_names.get(vid),
                ))
        pairs.sort(key=lambda x: (x.attribute or "").lower())

        rows.append(VariantRow(
            id=int(p["id"]),
            display_name=p.get("display_name"),
            default_code=(p.get("default_code") or None),
            barcode=(p.get("barcode") or None),
            qty_on_hand=float(p.get("qty_available") or 0.0),
            values=pairs,
        ))

    return {
        "ok": True,
        "template": template_summary,
        "count": len(rows),
        "variants": rows,
    }
