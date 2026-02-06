#!/usr/bin/env python3
"""Mission Control - Real-time Kanban with Direct OpenClaw Gateway Chat"""
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock
import json
import os
import threading
import websocket
from datetime import datetime
import time

app = Flask(__name__)
sock = Sock(app)
DATA_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/data.json'
CHAT_HISTORY_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/chat_history.json'

# Gateway configuration
GATEWAY_URL = os.environ.get('OPENCLAW_GATEWAY_URL', 'ws://localhost:8081/ws')

# Store connected browser WebSocket clients
browser_clients = []
browser_clients_lock = threading.Lock()

# Gateway WebSocket connection
gateway_ws = None
gateway_connected = False
gateway_thread = None

# Chat history
chat_history = []
history_lock = threading.Lock()

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {
        "todo": [],
        "doing": [],
        "done": [],
        "updated": datetime.now().isoformat()
    }

def save_data(data):
    data['updated'] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def load_chat_history():
    global chat_history
    if os.path.exists(CHAT_HISTORY_FILE):
        with open(CHAT_HISTORY_FILE, 'r') as f:
            data = json.load(f)
            chat_history = data.get('messages', [])
    return chat_history

def save_chat_history():
    with history_lock:
        with open(CHAT_HISTORY_FILE, 'w') as f:
            json.dump({
                "messages": chat_history,
                "updated": datetime.now().isoformat()
            }, f, indent=2)

def add_message(msg):
    """Add a message to chat history"""
    with history_lock:
        chat_history.append(msg)
        # Keep only last 500 messages
        if len(chat_history) > 500:
            chat_history[:] = chat_history[-500:]
    save_chat_history()
    return msg

def broadcast_to_browsers(message):
    """Broadcast message to all connected browser clients"""
    with browser_clients_lock:
        disconnected = []
        for client in browser_clients:
            try:
                client.send(json.dumps(message))
            except Exception as e:
                print(f"Failed to send to browser client: {e}")
                disconnected.append(client)
        
        # Clean up disconnected clients
        for client in disconnected:
            if client in browser_clients:
                browser_clients.remove(client)

def connect_to_gateway():
    """Maintain WebSocket connection to OpenClaw Gateway"""
    global gateway_ws, gateway_connected
    
    def on_open(ws):
        print(f"[Gateway] Connected to OpenClaw Gateway at {GATEWAY_URL}")
        global gateway_connected
        gateway_connected = True
        
        # Register as a web client
        ws.send(json.dumps({
            "type": "register",
            "client_type": "mission_control",
            "capabilities": ["chat", "receive_messages"]
        }))
        
        # Notify all browser clients that gateway is connected
        broadcast_to_browsers({
            "type": "gateway_status",
            "connected": True
        })
    
    def on_message(ws, message):
        try:
            data = json.loads(message)
            print(f"[Gateway] Received: {data.get('type', 'unknown')}")
            
            # Handle different message types from Gateway
            if data.get('type') == 'chat_message':
                # Message from OpenClaw (Koba)
                msg = {
                    "id": data.get('id') or f"koba_{int(time.time() * 1000)}",
                    "text": data.get('text', ''),
                    "from": "koba",
                    "timestamp": datetime.now().isoformat(),
                    "source": "gateway"
                }
                add_message(msg)
                broadcast_to_browsers({
                    "type": "message",
                    "message": msg
                })
                
            elif data.get('type') == 'typing':
                # Typing indicator from Koba
                broadcast_to_browsers({
                    "type": "typing",
                    "from": "koba",
                    "active": data.get('active', False)
                })
                
            elif data.get('type') == 'ack':
                # Message acknowledgment
                broadcast_to_browsers({
                    "type": "ack",
                    "message_id": data.get('message_id'),
                    "status": data.get('status', 'delivered')
                })
                
        except json.JSONDecodeError:
            print(f"[Gateway] Received non-JSON message: {message}")
        except Exception as e:
            print(f"[Gateway] Error handling message: {e}")
    
    def on_error(ws, error):
        print(f"[Gateway] Error: {error}")
    
    def on_close(ws, close_status_code, close_msg):
        print(f"[Gateway] Connection closed: {close_status_code} - {close_msg}")
        global gateway_connected
        gateway_connected = False
        
        # Notify browser clients
        broadcast_to_browsers({
            "type": "gateway_status",
            "connected": False
        })
        
        # Attempt reconnection after delay
        time.sleep(3)
        connect_to_gateway()
    
    try:
        gateway_ws = websocket.WebSocketApp(
            GATEWAY_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        gateway_ws.run_forever()
    except Exception as e:
        print(f"[Gateway] Failed to connect: {e}")
        gateway_connected = False
        time.sleep(5)
        connect_to_gateway()

def send_to_gateway(message):
    """Send a message to OpenClaw Gateway"""
    global gateway_ws, gateway_connected
    
    if gateway_ws and gateway_connected:
        try:
            gateway_ws.send(json.dumps(message))
            return True
        except Exception as e:
            print(f"[Gateway] Failed to send: {e}")
            return False
    else:
        print("[Gateway] Not connected, cannot send message")
        return False

# Start gateway connection in background thread
def start_gateway_connection():
    global gateway_thread
    gateway_thread = threading.Thread(target=connect_to_gateway, daemon=True)
    gateway_thread.start()

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/tasks')
def get_tasks():
    return jsonify(load_data())

@app.route('/api/tasks', methods=['POST'])
def update_tasks():
    data = request.json
    save_data(data)
    return jsonify({"status": "ok"})

@app.route('/api/chat/history')
def get_chat_history():
    return jsonify({
        "messages": chat_history,
        "updated": datetime.now().isoformat()
    })

@app.route('/api/chat/send', methods=['POST'])
def send_chat_message():
    """HTTP fallback for sending messages"""
    data = request.json
    text = data.get('text', '').strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400
    
    # Add user message to history
    msg = add_message({
        "id": f"user_{int(time.time() * 1000)}",
        "text": text,
        "from": "me",
        "timestamp": datetime.now().isoformat()
    })
    
    # Forward to Gateway
    success = send_to_gateway({
        "type": "chat_message",
        "text": text,
        "source": "mission_control",
        "message_id": msg["id"]
    })
    
    # Broadcast to all browser clients (including sender)
    broadcast_to_browsers({
        "type": "message",
        "message": msg
    })
    
    if success:
        return jsonify({"status": "ok", "message": msg})
    else:
        return jsonify({
            "status": "error",
            "error": "Gateway not connected",
            "message": msg
        }), 503

@sock.route('/ws/chat')
def browser_websocket(ws):
    """WebSocket endpoint for browser clients"""
    with browser_clients_lock:
        browser_clients.append(ws)
    
    print(f"[Browser] Client connected. Total clients: {len(browser_clients)}")
    
    # Send chat history to new client
    try:
        ws.send(json.dumps({
            "type": "history",
            "messages": chat_history,
            "gateway_connected": gateway_connected
        }))
    except Exception as e:
        print(f"[Browser] Failed to send history: {e}")
    
    # Keep connection alive and listen for messages
    while True:
        try:
            raw_data = ws.receive()
            if raw_data is None:
                break
            
            data = json.loads(raw_data)
            msg_type = data.get('type')
            
            if msg_type == 'send':
                text = data.get('text', '').strip()
                if text:
                    # Add to chat history
                    msg = add_message({
                        "id": f"user_{int(time.time() * 1000)}",
                        "text": text,
                        "from": "me",
                        "timestamp": datetime.now().isoformat()
                    })
                    
                    # Forward to OpenClaw Gateway
                    send_to_gateway({
                        "type": "chat_message",
                        "text": text,
                        "source": "mission_control",
                        "message_id": msg["id"]
                    })
                    
                    # Broadcast to all browser clients
                    broadcast_to_browsers({
                        "type": "message",
                        "message": msg
                    })
            
            elif msg_type == 'ping':
                ws.send(json.dumps({"type": "pong"}))
                
        except json.JSONDecodeError:
            print("[Browser] Received invalid JSON")
        except Exception as e:
            print(f"[Browser] Error: {e}")
            break
    
    # Clean up
    with browser_clients_lock:
        if ws in browser_clients:
            browser_clients.remove(ws)
    print(f"[Browser] Client disconnected. Total clients: {len(browser_clients)}")

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('.', path)

if __name__ == '__main__':
    # Ensure chat history file exists
    load_chat_history()
    
    # Start gateway connection
    start_gateway_connection()
    
    # Run with threading support for WebSocket
    print(f"[Server] Starting Mission Control on port 8080")
    print(f"[Server] Gateway URL: {GATEWAY_URL}")
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
