# purchase_router.py
# Mounted at /purchase by gateway.py
# Endpoints:
#   POST /purchase/create   -> create a new purchase order
#   POST /purchase/list     -> list purchase orders
#   POST /purchase/status   -> check order status
#   POST /purchase/approve  -> approve/reject order

import os
import xmlrpc.client
from functools import lru_cache
from typing import List, Dict, Any, Optional
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, validator
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

    def create(self, model: str, values: dict) -> int:
        return self.models.execute_kw(self.db, self.uid, self.password, model, "create", [values])

    def write(self, model: str, ids: List[int], values: dict) -> bool:
        return self.models.execute_kw(self.db, self.uid, self.password, model, "write", [ids, values])

    def execute(self, model: str, method: str, args: list) -> Any:
        return self.models.execute_kw(self.db, self.uid, self.password, model, method, args)

client = OdooClient(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)

# ====== models ======
class PurchaseOrderLine(BaseModel):
    product_id: int
    name: str
    product_qty: float
    price_unit: float
    product_uom: int
    
    @validator('product_qty')
    def validate_qty(cls, v):
        if v <= 0:
            raise ValueError('Quantity must be positive')
        return v
    
    @validator('price_unit')
    def validate_price(cls, v):
        if v < 0:
            raise ValueError('Price cannot be negative')
        return v

class CreatePurchaseOrderReq(BaseModel):
    partner_id: int
    order_lines: List[PurchaseOrderLine]
    date_order: Optional[str] = None  # ISO date string
    notes: Optional[str] = None
    created_by: str

class PurchaseOrderResp(BaseModel):
    ok: bool
    order_id: Optional[int] = None
    order_name: Optional[str] = None
    message: str

class PurchaseOrderItem(BaseModel):
    id: int
    name: str
    partner_id: List[int]  # [id, name]
    date_order: str
    amount_total: float
    state: str
    order_line: List[int]

class ListPurchaseOrdersReq(BaseModel):
    partner_id: Optional[int] = None
    state: Optional[str] = None
    limit: Optional[int] = 50

class ListPurchaseOrdersResp(BaseModel):
    ok: bool
    orders: List[PurchaseOrderItem]
    message: str

class OrderStatusReq(BaseModel):
    order_id: int

class OrderStatusResp(BaseModel):
    ok: bool
    order: Optional[PurchaseOrderItem] = None
    message: str

class ApproveOrderReq(BaseModel):
    order_id: int
    action: str  # "approve" or "reject"
    reason: Optional[str] = None

class ApproveOrderResp(BaseModel):
    ok: bool
    message: str

router = APIRouter()

@router.post("/create", response_model=PurchaseOrderResp)
async def create_purchase_order(req: CreatePurchaseOrderReq):
    """Create a new purchase order"""
    try:
        # Prepare order lines
        order_lines = []
        for line in req.order_lines:
            order_lines.append((0, 0, {
                'product_id': line.product_id,
                'name': line.name,
                'product_qty': line.product_qty,
                'price_unit': line.price_unit,
                'product_uom': line.product_uom,
            }))

        # Create purchase order
        order_vals = {
            'partner_id': req.partner_id,
            'order_line': order_lines,
            'date_order': req.date_order or None,
            'notes': req.notes or '',
        }
        
        order_id = client.create('purchase.order', order_vals)
        
        if order_id:
            # Get the order name
            order = client.read('purchase.order', [order_id], ['name'])
            order_name = order[0]['name'] if order else f"PO{order_id}"
            
            return PurchaseOrderResp(
                ok=True,
                order_id=order_id,
                order_name=order_name,
                message=f"Purchase order {order_name} created successfully"
            )
        else:
            return PurchaseOrderResp(
                ok=False,
                message="Failed to create purchase order"
            )
            
    except Exception as e:
        return PurchaseOrderResp(
            ok=False,
            message=f"Error creating purchase order: {str(e)}"
        )

@router.post("/list", response_model=ListPurchaseOrdersResp)
async def list_purchase_orders(req: ListPurchaseOrdersReq):
    """List purchase orders with optional filters"""
    try:
        domain = []
        if req.partner_id:
            domain.append(('partner_id', '=', req.partner_id))
        if req.state:
            domain.append(('state', '=', req.state))
            
        fields = ['id', 'name', 'partner_id', 'date_order', 'amount_total', 'state', 'order_line']
        orders = client.search_read('purchase.order', domain, fields, req.limit)
        
        order_items = []
        for order in orders:
            order_items.append(PurchaseOrderItem(
                id=order['id'],
                name=order['name'],
                partner_id=order['partner_id'],
                date_order=order['date_order'],
                amount_total=order['amount_total'],
                state=order['state'],
                order_line=order['order_line']
            ))
            
        return ListPurchaseOrdersResp(
            ok=True,
            orders=order_items,
            message=f"Found {len(order_items)} purchase orders"
        )
        
    except Exception as e:
        return ListPurchaseOrdersResp(
            ok=False,
            orders=[],
            message=f"Error listing purchase orders: {str(e)}"
        )

@router.post("/status", response_model=OrderStatusResp)
async def get_order_status(req: OrderStatusReq):
    """Get status of a specific purchase order"""
    try:
        fields = ['id', 'name', 'partner_id', 'date_order', 'amount_total', 'state', 'order_line']
        orders = client.read('purchase.order', [req.order_id], fields)
        
        if not orders:
            return OrderStatusResp(
                ok=False,
                message=f"Purchase order {req.order_id} not found"
            )
            
        order = orders[0]
        order_item = PurchaseOrderItem(
            id=order['id'],
            name=order['name'],
            partner_id=order['partner_id'],
            date_order=order['date_order'],
            amount_total=order['amount_total'],
            state=order['state'],
            order_line=order['order_line']
        )
        
        return OrderStatusResp(
            ok=True,
            order=order_item,
            message=f"Order {order['name']} status: {order['state']}"
        )
        
    except Exception as e:
        return OrderStatusResp(
            ok=False,
            message=f"Error getting order status: {str(e)}"
        )

@router.post("/approve", response_model=ApproveOrderResp)
async def approve_order(req: ApproveOrderReq):
    """Approve or reject a purchase order"""
    try:
        if req.action not in ['approve', 'reject']:
            return ApproveOrderResp(
                ok=False,
                message="Action must be 'approve' or 'reject'"
            )
            
        if req.action == 'approve':
            # Approve the order
            result = client.execute('purchase.order', 'button_confirm', [req.order_id])
            message = f"Purchase order {req.order_id} approved successfully"
        else:
            # Reject the order (cancel it)
            result = client.execute('purchase.order', 'button_cancel', [req.order_id])
            message = f"Purchase order {req.order_id} rejected"
            
        if result:
            return ApproveOrderResp(ok=True, message=message)
        else:
            return ApproveOrderResp(
                ok=False,
                message=f"Failed to {req.action} order {req.order_id}"
            )
            
    except Exception as e:
        return ApproveOrderResp(
            ok=False,
            message=f"Error {req.action}ing order: {str(e)}"
        )