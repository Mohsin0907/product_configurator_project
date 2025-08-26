#!/usr/bin/env python3
"""
Test script for Purchase Order API endpoints
Run this to test the purchase order functionality
"""

import requests
import json
from datetime import datetime

# Configuration - update these values
GATEWAY_URL = "http://localhost:8000"  # Update with your gateway URL
CREATED_BY = "test@example.com"

def test_purchase_order_create():
    """Test creating a purchase order"""
    print("🛒 Testing Purchase Order Creation...")
    
    payload = {
        "partner_id": 1,  # Supplier ID
        "order_lines": [
            {
                "product_id": 1,  # Product ID
                "name": "Test Product",
                "product_qty": 10.0,
                "price_unit": 25.50,
                "product_uom": 1
            }
        ],
        "created_by": CREATED_BY
    }
    
    try:
        response = requests.post(f"{GATEWAY_URL}/purchase/create", json=payload)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print("✅ Success!")
            print(f"Order ID: {result.get('order_id')}")
            print(f"Order Name: {result.get('order_name')}")
            print(f"Message: {result.get('message')}")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Request failed: {e}")

def test_purchase_order_list():
    """Test listing purchase orders"""
    print("\n📋 Testing Purchase Order List...")
    
    payload = {
        "limit": 10
    }
    
    try:
        response = requests.post(f"{GATEWAY_URL}/purchase/list", json=payload)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print("✅ Success!")
            print(f"Found {len(result.get('orders', []))} orders")
            print(f"Message: {result.get('message')}")
            
            # Show first few orders
            for i, order in enumerate(result.get('orders', [])[:3]):
                print(f"  {i+1}. {order.get('name')} - {order.get('state')} - ${order.get('amount_total', 0):.2f}")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Request failed: {e}")

def test_purchase_order_status():
    """Test getting order status"""
    print("\n📊 Testing Purchase Order Status...")
    
    # You'll need to create an order first or use an existing order ID
    order_id = 1  # Update with actual order ID
    
    payload = {
        "order_id": order_id
    }
    
    try:
        response = requests.post(f"{GATEWAY_URL}/purchase/status", json=payload)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print("✅ Success!")
            if result.get('order'):
                order = result['order']
                print(f"Order: {order.get('name')}")
                print(f"Status: {order.get('state')}")
                print(f"Amount: ${order.get('amount_total', 0):.2f}")
            else:
                print(f"Message: {result.get('message')}")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Request failed: {e}")

def test_health_check():
    """Test gateway health"""
    print("\n🏥 Testing Gateway Health...")
    
    try:
        response = requests.get(f"{GATEWAY_URL}/health")
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print("✅ Gateway is healthy!")
            print(f"Odoo UID: {result.get('uid')}")
        else:
            print(f"❌ Gateway unhealthy: {response.text}")
            
    except Exception as e:
        print(f"❌ Health check failed: {e}")

def main():
    """Run all tests"""
    print("🚀 Purchase Order API Test Suite")
    print("=" * 40)
    
    # Test health first
    test_health_check()
    
    # Test purchase order endpoints
    test_purchase_order_create()
    test_purchase_order_list()
    test_purchase_order_status()
    
    print("\n✨ Test suite completed!")

if __name__ == "__main__":
    main()