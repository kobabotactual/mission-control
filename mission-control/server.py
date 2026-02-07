#!/usr/bin/env python3
"""Mission Control - Real-time Kanban with Direct OpenClaw Gateway Chat

This server provides:
1. Kanban board API for task management
2. WebSocket chat that connects DIRECTLY to OpenClaw Gateway

Message Flow:
  Browser → Mission Control (WebSocket) → OpenClaw Gateway → Koba
  Koba → OpenClaw Gateway → Mission Control (WebSocket) → Browser
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock
import json
import os
import threading
import websocket
import time
from datetime import datetime

app = Flask(__name__)
sock = Sock(app)

# File paths
DATA_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/data.json'
CHAT_HISTORY_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/chat_history.json'

# Gateway configuration - connects to OpenClaw Gateway WebSocket
GATEWAY_WS_URL = os.environ.get('OPENCLAW_GATEWAY_URL', 'ws://localhost:8080/ws')

# Connected browser clients
browser_clients = []
browser_clients_lock = threading.Lock()

# Gateway connection
gateway_ws = None
gateway_connected = False
gateway_thread = None

# Chat history
chat_history = []
history_lock = threading.Lock()


def load_data():
    """Load kanban board data"""
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
    """Save kanban board data"""
    data['updated'] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def load_chat_history():
    """Load chat message history"""
    global chat_history
    if os.path.exists(CHAT_HISTORY_FILE):
        with open(CHAT_HISTORY_FILE, 'r') as f:
            data = json.load(f)
            chat_history = data.get('messages', [])
    return chat_history


def save_chat_history():
    """Persist chat history to disk"""
    with history_lock:
        with open(CHAT_HISTORY_FILE, 'w') as f:
            json.dump({
                "messages": chat_history,
                "updated": datetime.now().isoformat()
            }, f, indent=2)


def add_message(msg):
    """Add a message to history and persist"""
    with history_lock:
        chat_history.append(msg)
        # Keep only last 500 messages
        if len(chat_history) > 500:
            chat_history[:] = chat_history[-500:]
    save_chat_history()
    return msg


def broadcast_to_browsers(message):
    """Broadcast a message to all connected browser clients"""
    with browser_clients_lock:
        disconnected = []
        for client in browser_clients:
            try:
                client.send(json.dumps(message))
            except Exception as e:
                print(f"[Browser] Failed to send to client: {e}")
                disconnected.append(client)
        
        # Clean up disconnected clients
        for client in disconnected:
            if client in browser_clients:
                browser_clients.remove(client)


def on_gateway_open(ws):
    """Called when Gateway WebSocket connection opens"""
    print(f"[Gateway] ✓ Connected to OpenClaw Gateway at {GATEWAY_WS_URL}")
    global gateway_connected
    gateway_connected = True
    
    # Register as Mission Control client
    ws.send(json.dumps({
        "type": "register",
        "client": "mission_control",
        "capabilities": ["chat", "receive"]
    }))
    
    # Notify browser clients
    broadcast_to_browsers({
        "type": "gateway_status",
        "connected": True
    })


def on_gateway_message(ws, message):
    """Handle incoming messages from OpenClaw Gateway"""
    try:
        data = json.loads(message)
        msg_type = data.get('type', 'unknown')
        print(f"[Gateway] ← Received: {msg_type}")
        
        if msg_type == 'chat':
            # Message from Koba
            msg = {
                "id": data.get('id') or f"koba_{int(time.time() * 1000)}",
                "text": data.get('text', ''),
                "from": "koba",
                "timestamp": datetime.now().isoformat()
            }
            add_message(msg)
            broadcast_to_browsers({
                "type": "message",
                "message": msg
            })
            
        elif msg_type == 'typing':
            # Typing indicator from Koba
            broadcast_to_browsers({
                "type": "typing",
                "from": "koba",
                "active": data.get('active', False)
            })
            
        elif msg_type == 'ack':
            # Message acknowledgment
            broadcast_to_browsers({
                "type": "ack",
                "message_id": data.get('message_id'),
                "status": data.get('status', 'received')
            })
            
    except json.JSONDecodeError:
        print(f"[Gateway] Received non-JSON: {message[:100]}")
    except Exception as e:
        print(f"[Gateway] Error handling message: {e}")


def on_gateway_error(ws, error):
    """Handle Gateway connection errors"""
    print(f"[Gateway] ✗ Error: {error}")


def on_gateway_close(ws, close_status_code, close_msg):
    """Handle Gateway connection close"""
    print(f"[Gateway] ✗ Connection closed ({close_status_code}): {close_msg}")
    global gateway_connected
    gateway_connected = False
    
    # Notify browser clients
    broadcast_to_browsers({
        "type": "gateway_status",
        "connected": False
    })


def connect_to_gateway():
    """Maintain persistent WebSocket connection to OpenClaw Gateway"""
    global gateway_ws, gateway_connected
    
    while True:
        try:
            print(f"[Gateway] Connecting to {GATEWAY_WS_URL}...")
            gateway_ws = websocket.WebSocketApp(
                GATEWAY_WS_URL,
                on_open=on_gateway_open,
                on_message=on_gateway_message,
                on_error=on_gateway_error,
                on_close=on_gateway_close
            )
            gateway_ws.run_forever()
            
        except Exception as e:
            print(f"[Gateway] Connection failed: {e}")
        
        gateway_connected = False
        print("[Gateway] Reconnecting in 5 seconds...")
        time.sleep(5)


def send_to_gateway(message):
    """Send a message to OpenClaw Gateway"""
    if gateway_ws and gateway_connected:
        try:
            gateway_ws.send(json.dumps(message))
            return True
        except Exception as e:
            print(f"[Gateway] Send failed: {e}")
            return False
    return False


def start_gateway_connection():
    """Start Gateway connection in background thread"""
    global gateway_thread
    gateway_thread = threading.Thread(target=connect_to_gateway, daemon=True)
    gateway_thread.start()


# Flask Routes

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
    
    # Add user message
    msg = add_message({
        "id": f"user_{int(time.time() * 1000)}",
        "text": text,
        "from": "me",
        "timestamp": datetime.now().isoformat()
    })
    
    # Forward to Gateway
    gateway_available = send_to_gateway({
        "type": "chat",
        "text": text,
        "source": "mission_control",
        "message_id": msg["id"]
    })
    
    # Broadcast to browsers
    broadcast_to_browsers({
        "type": "message",
        "message": msg
    })
    
    return jsonify({
        "status": "ok" if gateway_available else "queued",
        "message": msg,
        "gateway_connected": gateway_available
    })


@sock.route('/ws/chat')
def browser_websocket(ws):
    """WebSocket endpoint for browser clients"""
    with browser_clients_lock:
        browser_clients.append(ws)
    
    print(f"[Browser] Client connected ({len(browser_clients)} total)")
    
    # Send initial state
    try:
        ws.send(json.dumps({
            "type": "history",
            "messages": chat_history,
            "gateway_connected": gateway_connected
        }))
    except Exception as e:
        print(f"[Browser] Failed to send history: {e}")
    
    # Listen for messages
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
                    # Add to history
                    msg = add_message({
                        "id": f"user_{int(time.time() * 1000)}",
                        "text": text,
                        "from": "me",
                        "timestamp": datetime.now().isoformat()
                    })
                    
                    # Forward to Gateway
                    send_to_gateway({
                        "type": "chat",
                        "text": text,
                        "source": "mission_control",
                        "message_id": msg["id"]
                    })
                    
                    # Broadcast to all browsers
                    broadcast_to_browsers({
                        "type": "message",
                        "message": msg
                    })
            
            elif msg_type == 'ping':
                ws.send(json.dumps({"type": "pong"}))
                
        except json.JSONDecodeError:
            print("[Browser] Invalid JSON received")
        except Exception as e:
            print(f"[Browser] Error: {e}")
            break
    
    # Cleanup
    with browser_clients_lock:
        if ws in browser_clients:
            browser_clients.remove(ws)
    print(f"[Browser] Client disconnected ({len(browser_clients)} remaining)")


@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('.', path)


if __name__ == '__main__':
    print("=" * 60)
    print("[Mission Control] Starting server")
    print("=" * 60)
    print(f"[Mission Control] URL: http://0.0.0.0:8080")
    print(f"[Mission Control] Chat WebSocket: ws://localhost:8080/ws/chat")
    print(f"[Mission Control] Gateway: {GATEWAY_WS_URL}")
    print("=" * 60)
    
    # Load history
    load_chat_history()
    
    # Connect to Gateway
    start_gateway_connection()
    
    # Start Flask server
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
