# Purchase Order Functionality

## Overview
The Telegram bot now includes the ability to create purchase orders through a conversational interface.

## New Features

### 🛒 Create Purchase Order
- **Menu Button**: Added "🛒 Create Purchase Order" to the main menu
- **Command**: `/purchase` command for quick access
- **Conversational Flow**: Step-by-step guided creation process

## How It Works

### 1. Supplier Selection
- User enters supplier/vendor name
- Currently uses placeholder partner_id (1) - TODO: implement supplier search

### 2. Product Selection
- User enters product name or code
- Bot searches existing products
- If multiple matches found, user can select from list
- If single match, automatically selected

### 3. Order Details
- **Quantity**: User enters positive number
- **Price**: User enters unit price (non-negative)
- **Multiple Products**: Option to add more products to same order

### 4. Review & Confirmation
- Shows complete order summary
- Displays supplier, items, quantities, prices, and total
- Options to confirm, add more products, or cancel

### 5. Order Creation
- Creates purchase order in Odoo via gateway
- Returns order confirmation with order number
- Order status: Draft (ready for approval)

## Gateway Endpoints

### POST `/purchase/create`
Creates a new purchase order
```json
{
  "partner_id": 1,
  "order_lines": [
    {
      "product_id": 123,
      "name": "Product Name",
      "product_qty": 10.0,
      "price_unit": 25.50,
      "product_uom": 1
    }
  ],
  "created_by": "user@example.com"
}
```

### POST `/purchase/list`
Lists purchase orders with optional filters
```json
{
  "partner_id": 1,
  "state": "draft",
  "limit": 50
}
```

### POST `/purchase/status`
Gets status of a specific purchase order
```json
{
  "order_id": 123
}
```

### POST `/purchase/approve`
Approves or rejects a purchase order
```json
{
  "order_id": 123,
  "action": "approve",
  "reason": "Optional reason"
}
```

## Conversation States

- `PO_ASK_SUPPLIER` (200): Waiting for supplier name
- `PO_ASK_PRODUCT` (201): Waiting for product selection
- `PO_ASK_QTY` (202): Waiting for quantity
- `PO_ASK_PRICE` (203): Waiting for price
- `PO_ASK_MORE` (204): Asking if more products needed
- `PO_REVIEW` (205): Review and confirmation

## Usage Examples

### Basic Flow
1. User clicks "🛒 Create Purchase Order"
2. Bot: "Please enter the supplier/vendor name:"
3. User: "ABC Supplies"
4. Bot: "Now enter the product name or code:"
5. User: "Widget A"
6. Bot: "Enter the quantity to order:"
7. User: "50"
8. Bot: "Enter the unit price:"
9. User: "10.99"
10. Bot: "Do you want to add more products? (yes/no)"
11. User: "no"
12. Bot shows review and confirmation buttons

### Command Usage
```
/purchase - Start purchase order creation
/cancel - Cancel current operation
```

## Technical Details

### Files Modified
- `bot.py` - Added purchase order conversation handlers
- `purchase_router.py` - New purchase order API endpoints
- `gateway.py` - Integrated purchase router

### Dependencies
- Requires Odoo XML-RPC access
- Uses existing product search functionality
- Integrates with existing bot framework

## Future Enhancements

1. **Supplier Search**: Implement proper supplier lookup by name
2. **UoM Selection**: Allow users to select units of measure
3. **Order Templates**: Save and reuse common order configurations
4. **Approval Workflow**: Integrate with Odoo approval processes
5. **Order History**: View and manage existing purchase orders
6. **Notifications**: Alert users about order status changes

## Error Handling

- Input validation for quantities and prices
- Graceful handling of product search failures
- Clear error messages for users
- Fallback to main menu on errors

## Testing

To test the functionality:
1. Start the bot with `/start`
2. Click "🛒 Create Purchase Order"
3. Follow the conversation flow
4. Verify order creation in Odoo