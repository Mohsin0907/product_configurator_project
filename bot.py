# bot.py
# Telegram UI for:
#  1) Create Variant for a Product
#  2) Check On-hand Quantity (single or list)
#  3) List available Variants of a Product
#
# Requirements:
#   pip install python-telegram-bot==20.7 python-dotenv requests

from __future__ import annotations

import os
import re
import math
import logging
from typing import Dict, List, Any

import requests
from dotenv import load_dotenv
from html import escape as html_escape

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================
# Env & logging
# =========================
load_dotenv()

BOT_TOKEN   = (os.getenv("BOT_TOKEN") or "").strip()
GATEWAY_URL = (os.getenv("GATEWAY_URL") or "").rstrip("/")
CREATED_BY  = (os.getenv("CREATED_BY") or "").strip()
QTY_LOCATION_ID = os.getenv("QTY_LOCATION_ID")  # optional (unused server-side for now)

if not (BOT_TOKEN and GATEWAY_URL and CREATED_BY):
    raise RuntimeError("Missing .env values: BOT_TOKEN, GATEWAY_URL, CREATED_BY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("sicli-bot")

GATEWAY_TIMEOUT = (5, 60)

# =========================
# HTTP helpers
# =========================
def _full_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return GATEWAY_URL + path

def gw_post(path: str, payload: dict) -> dict:
    url = _full_url(path)
    try:
        r = requests.post(url, json=payload, timeout=GATEWAY_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ReadTimeout:
        return {"ok": False, "message": "Gateway timeout (no response in 60s)."}
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "message": f"Gateway connection error: {e}"}
    except requests.HTTPError as e:
        text = r.text if 'r' in locals() else str(e)
        code = getattr(r, "status_code", "?")
        return {"ok": False, "message": f"Gateway HTTP {code}: {text}"}
    except Exception as e:
        return {"ok": False, "message": f"Gateway error: {e}"}

# =========================
# UI helpers
# =========================
def chunk(seq: List[Any], size: int) -> List[List[Any]]:
    return [seq[i:i+size] for i in range(0, len(seq), size)]

def truncate(txt: str, n: int = 40) -> str:
    txt = "" if txt is None else str(txt)
    return txt if len(txt) <= n else txt[: n - 1] + "…"

def _clean_value_name(attr_name: str, value_name: str) -> str:
    """Remove duplicated attribute prefix in value labels like 'THICKNESS: 1 mm' -> '1 mm'."""
    if not value_name:
        return value_name
    a = (attr_name or "").strip().lower()
    v = value_name.strip()
    if a and v.lower().startswith(a):
        v = re.sub(rf"^{re.escape(attr_name)}\s*[:\-–]\s*", "", v, flags=re.I)
    return v

async def fast_ack(update: Update, text: str = "Working…"):
    try:
        if update.callback_query:
            await update.callback_query.answer(text=text, cache_time=0)
    except Exception:
        pass

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("✨ Create Variant for a Product")],
            [KeyboardButton("📦 Check On-hand Quantity")],
            [KeyboardButton("📋 List available Variants of a Product")],
            [KeyboardButton("🛒 Create Purchase Order")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=False,
    )

# ===== Stock formatting =====
def fmt_qty_item(p: dict) -> str:
    name = html_escape(p.get("display_name") or "")
    code = html_escape(p.get("default_code") or "—")
    bc   = html_escape(p.get("barcode") or "—")
    uom  = html_escape(p.get("uom") or p.get("uom_name") or "")

    on_hand = (
        p.get("on_hand")
        or p.get("qty_on_hand")
        or p.get("qty_available")
        or 0
    )
    # Reserved/free are optional; keep only On hand line per your spec
    lines = [
        f"<b>Internal Reference:</b> <code>{code}</code> | <b>Barcode:</b> <code>{bc}</code>",
        f"<b>Name:</b> {name}",
        f"<b>On hand:</b> {on_hand}" + (f" {uom}" if uom else ""),
    ]
    return "\n".join(lines)

# =========================
# Conversation states
# =========================
SEARCH, PICK_TEMPLATE, PICK_ATTR, ASK_CODE, ASK_BARCODE = range(5)
LV_ASK_NAME, LV_PICK_TEMPLATE = range(100, 102)
PO_ASK_SUPPLIER, PO_ASK_PRODUCT, PO_ASK_QTY, PO_ASK_PRICE, PO_ASK_MORE, PO_REVIEW = range(200, 206)

# =========================
# Keyboards
# =========================
def build_template_keyboard(matches: List[dict], page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    total = len(matches)
    if total == 0:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔎 New search", callback_data="search:new")]])

    last_page = max(0, math.ceil(total / per_page) - 1)
    page = max(0, min(page, last_page))
    start, end = page * per_page, page * per_page + per_page
    view = matches[start:end]

    rows: List[List[InlineKeyboardButton]] = []
    for m in view:
        label = truncate(m.get("name") or f"Template {m['id']}", 40)
        rows.append([InlineKeyboardButton(label, callback_data=f"tpl:{m['id']}")])

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"tplpage:{page-1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("More ▶", callback_data=f"tplpage:{page+1}"))
    nav.append(InlineKeyboardButton("🔎 New search", callback_data="search:new"))

    rows.append(nav)
    return InlineKeyboardMarkup(rows)

def build_attr_keyboard(attr: dict, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    vals = attr.get("values") or []
    total = len(vals)
    last_page = max(0, math.ceil(total / per_page) - 1)
    page = max(0, min(page, last_page))
    start, end = page * per_page, page * per_page + per_page
    view = vals[start:end]

    rows: List[List[InlineKeyboardButton]] = []
    for pair in chunk(view, 2):  # 2 columns
        row = [
            InlineKeyboardButton(
                truncate(_clean_value_name(attr.get("attribute_name") or "", v.get("name") or f"#{v['id']}"), 24),
                callback_data=f"val:{attr['attribute_id']}:{v['id']}"
            )
            for v in pair
        ]
        rows.append(row)

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data="attrpage:-1"))
    if page < last_page:
        nav.append(InlineKeyboardButton("More ▶", callback_data="attrpage:+1"))
    nav.append(InlineKeyboardButton("↩ Back", callback_data="attr:back"))
    rows.append(nav)

    return InlineKeyboardMarkup(rows)

# =========================
# Session helpers
# =========================
def reset_session(data: Dict[str, Any]):
    data.clear()
    data.update({
        "stage": None,
        "query": None,
        "matches": [],
        "tpl_page": 0,
        "template": None,
        "attributes": [],          # [{attribute_id, attribute_name, values:[{id,name}], page, selected}]
        "current_idx": 0,
        "default_code": None,
        "barcode": None,
        # qtylist prompt control
        "awaiting_qty_list": False,
        "qty_prompt_id": None,
        # list variants mini-flow
        "lv_stage": None,
        "lv_matches": [],
        "lv_page": 0,
    })

def selections_list(data: Dict[str, Any]) -> List[dict]:
    out = []
    for a in data.get("attributes", []):
        sel = a.get("selected")
        if sel:
            out.append({"attribute_id": a["attribute_id"], "value_id": sel})
    return out

# =========================
# Start / Menu
# =========================
async def menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_session(context.user_data)
    txt = "👋 <b>SICLI Assistant</b>\nChoose a service below:"
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=main_menu_kb())
    return ConversationHandler.END

# ===== Variant flow entry =====
async def variant_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_session(context.user_data)
    context.user_data["stage"] = "search"
    await update.message.reply_text(
        "Type the <b>base product name</b> (we’ll only show items <b>created by you</b>).",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return SEARCH

# ===== Qty list entry (button) — FIXED with ForceReply + prompt ID check =====
async def qty_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_session(context.user_data)
    context.user_data["awaiting_qty_list"] = True
    sent = await update.message.reply_text(
        "Send Internal Reference(s) or Barcode(s).\n"
        "You can paste a single ID or a comma/newline-separated list.",
        reply_markup=ForceReply(selective=True),  # require replying to THIS message
    )
    context.user_data["qty_prompt_id"] = sent.message_id
    return ConversationHandler.END

# ===== List variants entry =====
async def lv_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_session(context.user_data)
    context.user_data["lv_stage"] = "ask_name"
    await update.message.reply_text("Enter Product name:", reply_markup=ReplyKeyboardRemove())
    return LV_ASK_NAME

# =========================
# Variant flow (search -> pick template -> pick attributes -> ask code+barcode -> prepare -> create/use)
# =========================
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_session(context.user_data)
    await update.message.reply_text("❌ Cancelled. Use /start to choose a service.", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def on_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("Please type a product name.")
        return SEARCH

    context.user_data["query"] = q
    res = gw_post("/config/start", {"query": q, "created_by": CREATED_BY})
    if not res.get("ok"):
        await update.message.reply_text(f"Server error while searching:\n{res.get('message')}")
        return SEARCH

    matches = res.get("matches") or []
    context.user_data["matches"] = matches
    context.user_data["tpl_page"] = 0

    if not matches:
        await update.message.reply_text("No matches. Try another name or /cancel.")
        return SEARCH

    kb = build_template_keyboard(matches, page=0)
    await update.message.reply_text(
        f"🔎 <b>Found {len(matches)} matches</b> for “{html_escape(q)}” (created by you):",
        reply_markup=kb,
        parse_mode="HTML"
    )
    context.user_data["stage"] = "pick_template"
    return PICK_TEMPLATE

async def on_template_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await fast_ack(update)
    cq = update.callback_query
    data = cq.data or ""
    ud = context.user_data

    if data == "search:new":
        ud["stage"] = "search"
        await cq.message.edit_text("Okay, send the base product <b>name</b>.", parse_mode="HTML")
        return SEARCH

    if data.startswith("tplpage:"):
        try:
            page = int(data.split(":")[1])
        except Exception:
            page = 0
        ud["tpl_page"] = page
        kb = build_template_keyboard(ud.get("matches", []), page=page)
        try:
            await cq.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            await cq.message.edit_text("Pick a base product:", reply_markup=kb)
        return PICK_TEMPLATE

    if data.startswith("tpl:"):
        tpl_id = int(data.split(":")[1])
        tpl_name = None
        for m in ud.get("matches", []):
            if int(m["id"]) == tpl_id:
                tpl_name = m.get("name") or f"Template {tpl_id}"
                break

        res = gw_post("/config/init", {"template_id": tpl_id})
        if not res.get("ok"):
            await cq.message.edit_text(f"Server error while loading template:\n{res.get('message')}")
            return SEARCH

        attrs = res.get("attributes") or []
        decorated = []
        for a in attrs:
            a2 = dict(a)
            a2["page"] = 0
            a2["selected"] = None
            decorated.append(a2)

        ud["template"] = {"id": tpl_id, "name": tpl_name}
        ud["attributes"] = decorated
        ud["current_idx"] = 0
        ud["stage"] = "pick_attr"

        return await send_current_attribute(update, context, replace=True)

    return PICK_TEMPLATE

async def send_current_attribute(update: Update, context: ContextTypes.DEFAULT_TYPE, replace: bool = False):
    ud = context.user_data
    idx = ud.get("current_idx", 0)
    attrs = ud.get("attributes", [])
    if idx >= len(attrs):
        return await ask_internal_ref(update, context)

    a = attrs[idx]
    name = a.get("attribute_name") or f"Attribute {a['attribute_id']}"
    kb = build_attr_keyboard(a, page=a.get("page", 0))
    msg = f"Please pick <b>one</b> value for:\n• <b>{html_escape(name)}</b>"

    if update.callback_query and replace:
        await update.callback_query.edit_message_text(
            msg, reply_markup=kb, parse_mode="HTML"
        )
    else:
        if update.callback_query:
            await update.callback_query.message.reply_text(msg, reply_markup=kb, parse_mode="HTML")
        else:
            await update.message.reply_text(msg, reply_markup=kb, parse_mode="HTML")
    return PICK_ATTR

async def on_attr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await fast_ack(update)
    cq = update.callback_query
    data = cq.data or ""
    ud = context.user_data
    idx = ud.get("current_idx", 0)
    attrs = ud.get("attributes", [])
    if idx >= len(attrs):
        return await send_current_attribute(update, context, replace=True)

    if data == "attr:back":
        if idx > 0:
            ud["current_idx"] = idx - 1
        return await send_current_attribute(update, context, replace=True)

    if data.startswith("attrpage:"):
        step = data.split(":")[1]
        step_val = -1 if step == "-1" else 1
        a = attrs[idx]
        a["page"] = max(0, a.get("page", 0) + step_val)
        return await send_current_attribute(update, context, replace=True)

    if data.startswith("val:"):
        parts = data.split(":")
        if len(parts) != 3:
            return PICK_ATTR
        _, _attr_id_str, val_id_str = parts
        val_id = int(val_id_str)

        a = attrs[idx]
        a["selected"] = val_id

        ud["current_idx"] = idx + 1
        if ud["current_idx"] >= len(attrs):
            return await ask_internal_ref(update, context)
        else:
            return await send_current_attribute(update, context, replace=True)

    return PICK_ATTR

async def ask_internal_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["stage"] = "ask_code"
    txt = "✅ Selections complete.\n\nEnter <b>Internal Reference</b> (required)."
    if update.callback_query:
        await update.callback_query.message.reply_text(txt, reply_markup=ForceReply(selective=True), parse_mode="HTML")
    else:
        await update.message.reply_text(txt, reply_markup=ForceReply(selective=True), parse_mode="HTML")
    return ASK_CODE

async def on_code_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = (update.message.text or "").strip()
    if not code:
        await update.message.reply_text("Internal Reference is required. Please enter it.", reply_markup=ForceReply(selective=True))
        return ASK_CODE
    context.user_data["default_code"] = code
    context.user_data["stage"] = "ask_barcode"
    await update.message.reply_text("Enter <b>Barcode</b> (required).", reply_markup=ForceReply(selective=True), parse_mode="HTML")
    return ASK_BARCODE

async def on_barcode_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    barcode = (update.message.text or "").strip()
    if not barcode:
        await update.message.reply_text("Barcode is required. Please enter it.", reply_markup=ForceReply(selective=True))
        return ASK_BARCODE
    context.user_data["barcode"] = barcode

    tpl = context.user_data.get("template") or {}
    payload = {
        "template_id": tpl.get("id"),
        "selections": selections_list(context.user_data),
        "default_code": context.user_data.get("default_code"),
        "barcode": context.user_data.get("barcode"),
    }
    res = gw_post("/config/prepare", payload)
    if not res.get("ok"):
        await update.message.reply_text(f"Server error on duplicate check:\n{res.get('message')}")
        return SEARCH

    existing = res.get("existing_variant")
    sel_lines = []
    for a in context.user_data.get("attributes", []):
        name = a.get("attribute_name") or f"Attr {a['attribute_id']}"
        vid = a.get("selected")
        vname = None
        for v in a.get("values", []):
            if int(v["id"]) == int(vid):
                vname = _clean_value_name(name, v.get("name") or "")
                break
        sel_lines.append(f"• {html_escape(name)}: <code>{html_escape(vname or vid)}</code>")

    summary = (
        f"🧾 <b>Review</b>\n"
        f"Base: <b>{html_escape(tpl.get('name'))}</b>\n"
        + "\n".join(sel_lines)
        + "\n\n"
        f"Internal Ref: <code>{html_escape(context.user_data.get('default_code'))}</code>\n"
        f"Barcode: <code>{html_escape(context.user_data.get('barcode'))}</code>\n"
    )

    if existing:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Use existing", callback_data=f"use:{existing['id']}")],
            [InlineKeyboardButton("🔁 Change selections", callback_data="change:attrs")],
        ])
        await update.message.reply_text(summary + "\n⚠️ Variant already exists.", reply_markup=kb, parse_mode="HTML")
        return PICK_TEMPLATE
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ Create Variant", callback_data="create:new")],
            [InlineKeyboardButton("🔁 Change selections", callback_data="change:attrs")],
        ])
        await update.message.reply_text(summary + "\n✅ No duplicate found.", reply_markup=kb, parse_mode="HTML")
        return PICK_TEMPLATE

async def on_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await fast_ack(update)
    cq = update.callback_query
    data = cq.data or ""

    if data.startswith("use:"):
        vid = int(data.split(":")[1])
        await cq.message.edit_text(f"✅ Using existing variant (ID: {vid}).\nDone.")
        await cq.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
        reset_session(context.user_data)
        return ConversationHandler.END

    if data == "change:attrs":
        context.user_data["current_idx"] = 0
        for a in context.user_data.get("attributes", []):
            a["page"] = 0
        return await send_current_attribute(update, context, replace=True)

    if data == "create:new":
        tpl = context.user_data.get("template") or {}
        payload = {
            "template_id": tpl.get("id"),
            "selections": selections_list(context.user_data),
            "default_code": context.user_data.get("default_code"),
            "barcode": context.user_data.get("barcode"),
        }
        res = gw_post("/config/create", payload)
        if not res.get("ok"):
            await cq.message.edit_text(f"Server error while creating:\n{res.get('message')}")
            await cq.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
            return ConversationHandler.END

        v = res.get("variant") or {}
        created = res.get("created")
        status = "created" if created else "exists"
        txt = (
            "✅ Variant " + status + ".\n\n"
            f"ID: {v.get('id')}\n"
            f"Name: {v.get('display_name')}\n"
            f"Internal Ref: {v.get('default_code') or '—'}\n"
            f"Barcode: {v.get('barcode') or '—'}\n"
        )
        await cq.message.edit_text(txt)
        await cq.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
        reset_session(context.user_data)
        return ConversationHandler.END

    return PICK_TEMPLATE

# =========================
# Qty list flow (single or list) — now requires replying to bot prompt
# =========================
def _parse_ident_list(text: str) -> list[str]:
    if not text:
        return []
    parts = [x.strip() for x in text.replace("\n", ",").split(",")]
    return [x for x in parts if x]

async def cmd_qtylist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args).strip()
    if not raw:
        # No inline args → ask with ForceReply and track prompt id
        reset_session(context.user_data)
        context.user_data["awaiting_qty_list"] = True
        sent = await update.message.reply_text(
            "Send identifiers separated by comma or new lines, e.g.\n"
            "12345, 67890\nor\n12345\n67890",
            reply_markup=ForceReply(selective=True),
        )
        context.user_data["qty_prompt_id"] = sent.message_id
        return
    await _handle_qtylist_payload(update, context, _parse_ident_list(raw))

async def on_qtylist_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only accept if we are awaiting AND the user is replying to our prompt
    if not context.user_data.get("awaiting_qty_list"):
        return
    prompt_id = context.user_data.get("qty_prompt_id")
    if not (update.message and update.message.reply_to_message):
        return
    if prompt_id and update.message.reply_to_message.message_id != prompt_id:
        return

    # consume and clear the flag
    context.user_data["awaiting_qty_list"] = False
    context.user_data["qty_prompt_id"] = None

    idents = _parse_ident_list(update.message.text or "")
    if not idents:
        await update.message.reply_text("No valid identifiers found.")
        await update.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
        return
    await _handle_qtylist_payload(update, context, idents)

async def _handle_qtylist_payload(update: Update, context: ContextTypes.DEFAULT_TYPE, idents: List[str]):
    if not idents:
        await update.message.reply_text("No valid identifiers found.")
        await update.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
        return

    res = gw_post("/qty/check", {"identifiers": idents, "mode": "auto"})
    if not res.get("ok"):
        await update.message.reply_text(res.get("message", "Lookup failed."))
        await update.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
        return

    items = res.get("items") or []
    if not items:
        await update.message.reply_text("No products found.")
        await update.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
        return

    out_chunks: List[str] = []
    for entry in items:
        key = entry.get("input") or ""
        matches = entry.get("matches") or []
        if not matches:
            out_chunks.append(f"🔎 <b>{html_escape(key)}</b>\nNot found.")
            continue
        block = [f"🔎 <b>{html_escape(key)}</b>"]
        for p in matches:
            block.append(fmt_qty_item(p))
            block.append("— — —")
        out_chunks.append("\n".join(block))

    buf = []
    for block in out_chunks:
        if len("\n\n".join(buf + [block])) > 3500:
            await update.message.reply_html("\n\n".join(buf))
            buf = [block]
        else:
            buf.append(block)
    if buf:
        await update.message.reply_html("\n\n".join(buf))

    await update.message.reply_text("Choose a service:", reply_markup=main_menu_kb())

# =========================
# List-variants mini flow
# =========================
def _fmt_values_inline(pairs: List[dict]) -> str:
    bits = []
    for pr in pairs or []:
        a = pr.get("attribute")
        v = pr.get("value")
        if a or v:
            bits.append(f"{html_escape(a or '')}: {html_escape(v or '')}")
    return "; ".join(bits) if bits else "—"

async def lv_on_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("Please enter a product name.")
        return LV_ASK_NAME

    res = gw_post("/config/start", {"query": q, "created_by": CREATED_BY})
    if not res.get("ok"):
        await update.message.reply_text(f"Search failed:\n{res.get('message')}")
        return LV_ASK_NAME

    matches = res.get("matches") or []
    if not matches:
        await update.message.reply_text("No matches. Try another name or /start.")
        return ConversationHandler.END

    context.user_data["lv_matches"] = matches
    context.user_data["lv_page"] = 0

    kb = build_template_keyboard(matches, page=0)
    await update.message.reply_text(
        f"🔎 <b>Found {len(matches)} matches</b> for “{html_escape(q)}”. Pick a product:",
        reply_markup=kb, parse_mode="HTML"
    )
    return LV_PICK_TEMPLATE

async def lv_on_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await fast_ack(update)
    cq = update.callback_query
    data = cq.data or ""

    if data == "search:new":
        await cq.message.edit_text("Enter Product name:")
        return LV_ASK_NAME

    if data.startswith("tplpage:"):
        page = int(data.split(":")[1])
        context.user_data["lv_page"] = page
        kb = build_template_keyboard(context.user_data.get("lv_matches", []), page=page)
        try:
            await cq.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            await cq.message.edit_text("Pick a product:", reply_markup=kb)
        return LV_PICK_TEMPLATE

    if data.startswith("tpl:"):
        tpl_id = int(data.split(":")[1])
        res = gw_post("/variants/of_template", {"template_id": tpl_id, "active_only": True})
        if not res.get("ok"):
            await cq.message.edit_text(f"Error reading variants:\n{res.get('message')}")
            await cq.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
            return ConversationHandler.END

        tpl = res.get("template") or {}
        rows = res.get("variants") or []
        total = res.get("count") or len(rows)

        header = f"📋 <b>{html_escape(tpl.get('name') or f'Template {tpl_id}')}</b>\n" \
                 f"{total} variant(s) found.\n"
        await cq.message.edit_text(header, parse_mode="HTML")

        buf: List[str] = []
        for v in rows:
            line = (
                f"<b>Internal Reference:</b> <code>{html_escape(v.get('default_code') or '—')}</code> | "
                f"<b>Barcode:</b> <code>{html_escape(v.get('barcode') or '—')}</code>\n"
                f"<b>Name:</b> {html_escape(v.get('display_name') or '')}\n"
                f"<b>On hand:</b> {v.get('qty_on_hand') if v.get('qty_on_hand') is not None else 0}\n"
                f"— — —\n"
            )
            if len("\n".join(buf + [line])) > 3500:
                await cq.message.reply_html("\n".join(buf))
                buf = [line]
            else:
                buf.append(line)
        if buf:
            await cq.message.reply_html("\n".join(buf))

        await cq.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
        return ConversationHandler.END

    return LV_PICK_TEMPLATE

# =========================
# Purchase Order Flow
# =========================
async def po_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for purchase order creation"""
    context.user_data.clear()
    await update.message.reply_text(
        "🛒 <b>Create Purchase Order</b>\n\n"
        "Please enter the supplier/vendor name:",
        parse_mode="HTML"
    )
    return PO_ASK_SUPPLIER

async def po_on_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle supplier input"""
    supplier_name = (update.message.text or "").strip()
    if not supplier_name:
        await update.message.reply_text("Please enter a valid supplier name.")
        return PO_ASK_SUPPLIER
    
    # Store supplier name for now (we'll need to implement supplier search)
    context.user_data["supplier_name"] = supplier_name
    
    await update.message.reply_text(
        f"✅ Supplier: <b>{html_escape(supplier_name)}</b>\n\n"
        "Now enter the product name or code:",
        parse_mode="HTML"
    )
    return PO_ASK_PRODUCT

async def po_on_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product input"""
    product_query = (update.message.text or "").strip()
    if not product_query:
        await update.message.reply_text("Please enter a valid product name or code.")
        return PO_ASK_PRODUCT
    
    # Search for product
    res = gw_post("/config/start", {"query": product_query, "created_by": CREATED_BY})
    if not res.get("ok"):
        await update.message.reply_text(f"Product search failed:\n{res.get('message')}")
        return PO_ASK_PRODUCT
    
    matches = res.get("matches") or []
    if not matches:
        await update.message.reply_text("No products found. Try another name or /cancel.")
        return PO_ASK_PRODUCT
    
    if len(matches) == 1:
        # Single match, use it
        product = matches[0]
        context.user_data["product_id"] = product["id"]
        context.user_data["product_name"] = product.get("name") or f"Product {product['id']}"
        
        await update.message.reply_text(
            f"✅ Product: <b>{html_escape(context.user_data['product_name'])}</b>\n\n"
            "Enter the quantity to order:",
            parse_mode="HTML"
        )
        return PO_ASK_QTY
    else:
        # Multiple matches, let user choose
        context.user_data["product_matches"] = matches
        context.user_data["product_page"] = 0
        
        kb = build_template_keyboard(matches, page=0)
        await update.message.reply_text(
            f"🔎 <b>Found {len(matches)} products</b> for \"{html_escape(product_query)}\". Pick one:",
            reply_markup=kb, parse_mode="HTML"
        )
        return PO_ASK_PRODUCT

async def po_on_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quantity input"""
    try:
        qty = float((update.message.text or "").strip())
        if qty <= 0:
            await update.message.reply_text("Quantity must be positive. Please try again.")
            return PO_ASK_QTY
    except ValueError:
        await update.message.reply_text("Please enter a valid number for quantity.")
        return PO_ASK_QTY
    
    context.user_data["quantity"] = qty
    
    await update.message.reply_text(
        f"✅ Quantity: <b>{qty}</b>\n\n"
        "Enter the unit price:",
        parse_mode="HTML"
    )
    return PO_ASK_PRICE

async def po_on_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle price input"""
    try:
        price = float((update.message.text or "").strip())
        if price < 0:
            await update.message.reply_text("Price cannot be negative. Please try again.")
            return PO_ASK_PRICE
    except ValueError:
        await update.message.reply_text("Please enter a valid number for price.")
        return PO_ASK_PRICE
    
    context.user_data["price"] = price
    
    # Store the first order line
    context.user_data["order_lines"] = [{
        "product_id": context.user_data["product_id"],
        "name": context.user_data["product_name"],
        "product_qty": context.user_data["quantity"],
        "price_unit": context.user_data["price"],
        "product_uom": 1  # Default UoM (we can enhance this later)
    }]
    
    await update.message.reply_text(
        f"✅ Price: <b>${price:.2f}</b>\n\n"
        "Do you want to add more products to this order?\n"
        "Reply with 'yes' to add more, or 'no' to review the order.",
        parse_mode="HTML"
    )
    return PO_ASK_MORE

async def po_on_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle whether to add more products"""
    response = (update.message.text or "").strip().lower()
    
    if response in ['yes', 'y', 'more', 'add']:
        await update.message.reply_text(
            "Enter the next product name or code:"
        )
        return PO_ASK_PRODUCT
    elif response in ['no', 'n', 'done', 'finish']:
        # Show order review
        return await _show_po_review(update, context)
    else:
        await update.message.reply_text(
            "Please reply with 'yes' to add more products or 'no' to finish."
        )
        return PO_ASK_MORE

async def _show_po_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show purchase order review"""
    order_lines = context.user_data.get("order_lines", [])
    supplier = context.user_data.get("supplier_name", "Unknown")
    
    total = sum(line["product_qty"] * line["price_unit"] for line in order_lines)
    
    review_text = (
        f"📋 <b>Purchase Order Review</b>\n\n"
        f"<b>Supplier:</b> {html_escape(supplier)}\n"
        f"<b>Total Items:</b> {len(order_lines)}\n"
        f"<b>Total Amount:</b> ${total:.2f}\n\n"
        f"<b>Order Lines:</b>\n"
    )
    
    for i, line in enumerate(order_lines, 1):
        review_text += (
            f"{i}. {html_escape(line['name'])}\n"
            f"   Qty: {line['product_qty']} × ${line['price_unit']:.2f} = ${line['product_qty'] * line['price_unit']:.2f}\n\n"
        )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Order", callback_data="confirm")],
        [InlineKeyboardButton("➕ Add More Products", callback_data="add_more")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ])
    
    await update.message.reply_text(review_text, reply_markup=kb, parse_mode="HTML")
    return PO_REVIEW

async def po_on_product_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product selection from multiple matches"""
    await fast_ack(update)
    cq = update.callback_query
    data = cq.data or ""
    
    if data.startswith("tpl:"):
        tpl_id = int(data.split(":")[1])
        matches = context.user_data.get("product_matches", [])
        
        # Find the selected product
        selected_product = None
        for match in matches:
            if match["id"] == tpl_id:
                selected_product = match
                break
        
        if selected_product:
            context.user_data["product_id"] = selected_product["id"]
            context.user_data["product_name"] = selected_product.get("name") or f"Product {selected_product['id']}"
            
            await cq.message.edit_text(
                f"✅ Product: <b>{html_escape(context.user_data['product_name'])}</b>\n\n"
                "Enter the quantity to order:",
                parse_mode="HTML"
            )
            return PO_ASK_QTY
        else:
            await cq.message.edit_text("❌ Product not found. Please try again.")
            return PO_ASK_PRODUCT
    
    return PO_ASK_PRODUCT

async def po_on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle review callback"""
    await fast_ack(update)
    cq = update.callback_query
    data = cq.data or ""
    
    if data == "confirm":
        # Create the purchase order
        await _create_purchase_order(update, context)
        return ConversationHandler.END
    elif data == "add_more":
        await cq.message.edit_text("Enter the next product name or code:")
        return PO_ASK_PRODUCT
    elif data == "cancel":
        await cq.message.edit_text("❌ Purchase order cancelled.")
        await cq.message.reply_text("Choose a service:", reply_markup=main_menu_kb())
        return ConversationHandler.END
    
    return PO_REVIEW

async def _create_purchase_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Actually create the purchase order via gateway"""
    try:
        # For now, we'll use a placeholder partner_id (1)
        # In a real implementation, you'd search for the supplier
        payload = {
            "partner_id": 1,  # TODO: Implement supplier search
            "order_lines": context.user_data.get("order_lines", []),
            "created_by": CREATED_BY
        }
        
        res = gw_post("/purchase/create", payload)
        
        if res.get("ok"):
            order_name = res.get("order_name", "Unknown")
            await update.callback_query.message.edit_text(
                f"✅ <b>Purchase Order Created Successfully!</b>\n\n"
                f"<b>Order:</b> {html_escape(order_name)}\n"
                f"<b>Status:</b> Draft\n\n"
                f"Your purchase order has been created and is ready for approval.",
                parse_mode="HTML"
            )
        else:
            await update.callback_query.message.edit_text(
                f"❌ <b>Failed to Create Purchase Order</b>\n\n"
                f"Error: {html_escape(res.get('message', 'Unknown error'))}",
                parse_mode="HTML"
            )
        
        await update.callback_query.message.reply_text(
            "Choose a service:", reply_markup=main_menu_kb()
        )
        
    except Exception as e:
        await update.callback_query.message.edit_text(
            f"❌ <b>Error Creating Purchase Order</b>\n\n"
            f"An unexpected error occurred: {html_escape(str(e))}",
            parse_mode="HTML"
        )
        await update.callback_query.message.reply_text(
            "Choose a service:", reply_markup=main_menu_kb()
        )

# =========================
# Conversation builders
# =========================
def variant_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^✨ Create Variant for a Product$"), variant_entry),
            CommandHandler("variant", variant_entry),
        ],
        states={
            SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_search_text)],
            PICK_TEMPLATE: [
                CallbackQueryHandler(on_template_nav,    pattern=r"^(tpl:\d+|tplpage:-?\d+|search:new)$"),
                CallbackQueryHandler(on_review_callback, pattern=r"^(use:\d+|change:attrs|create:new)$"),
            ],
            PICK_ATTR: [
                CallbackQueryHandler(on_attr_callback,   pattern=r"^(val:\d+:\d+|attrpage:[+-]1|attr:back)$"),
            ],
            ASK_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_code_text)],
            ASK_BARCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_barcode_text)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        name="variant_flow",
        persistent=False,
    )

def list_variants_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^📋 List available Variants of a Product$"), lv_entry)],
        states={
            LV_ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, lv_on_name)],
            LV_PICK_TEMPLATE: [CallbackQueryHandler(lv_on_template, pattern=r"^(tpl:\d+|tplpage:-?\d+|search:new)$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        name="list_variants_flow",
        persistent=False,
    )

def purchase_order_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🛒 Create Purchase Order$"), po_entry)],
        states={
            PO_ASK_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, po_on_supplier)],
            PO_ASK_PRODUCT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, po_on_product),
                CallbackQueryHandler(po_on_product_select, pattern=r"^tpl:\d+$")
            ],
            PO_ASK_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, po_on_qty)],
            PO_ASK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, po_on_price)],
            PO_ASK_MORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, po_on_more)],
            PO_REVIEW: [CallbackQueryHandler(po_on_review, pattern=r"^(confirm|add_more|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        name="purchase_order_flow",
        persistent=False,
    )

# =========================
# main
# =========================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start shows menu
    app.add_handler(CommandHandler("start", menu_entry))

    # Menu buttons → flows
    app.add_handler(variant_conversation())
    app.add_handler(list_variants_conversation())
    app.add_handler(purchase_order_conversation())
    app.add_handler(MessageHandler(filters.Regex(r"^📦 Check On-hand Quantity$"), qty_entry))

    # Also keep /qtylist command + its follow-up (requires reply to prompt)
    app.add_handler(CommandHandler("qtylist", cmd_qtylist))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_qtylist_text), group=1)
    
    # Purchase order command
    app.add_handler(CommandHandler("purchase", po_entry))

    app.add_error_handler(on_error)
    log.info("Starting Sicli-Bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
