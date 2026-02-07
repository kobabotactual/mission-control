#!/usr/bin/env python3
"""Mission Control - OpenClaw Gateway Chat Interface

Connects to OpenClaw Gateway via CLI commands for sending messages.
Receives responses via WebSocket for real-time updates.
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock
import json
import os
import subprocess
import threading
import time
from datetime import datetime

# Optional websocket-client for Gateway connection
try:
    import websocket
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    print("[Warning] websocket-client not installed. Install with: pip3 install websocket-client")

app = Flask(__name__)
sock = Sock(app)

# File paths
DATA_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/data.json'
CHAT_HISTORY_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/chat_history.json'

# Gateway configuration
GATEWAY_HOST = '127.0.0.1'
GATEWAY_PORT = 18789
GATEWAY_TOKEN = 'a67e3d0ff02b513da41bae4d5326e82dde674f348a2e39c4'
GATEWAY_WS_URL = f'ws://{GATEWAY_HOST}:{GATEWAY_PORT}'

# Telegram target for the main user
TELEGRAM_TARGET = 'telegram:6301634831'

# Store connected WebSocket clients (browsers)
chat_clients = []
clients_lock = threading.Lock()

# Gateway WebSocket connection (for receiving messages)
gateway_ws = None
gateway_connected = False

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
    if os.path.exists(CHAT_HISTORY_FILE):
        with open(CHAT_HISTORY_FILE, 'r') as f:
            return json.load(f)
    return {
        "messages": [],
        "updated": datetime.now().isoformat()
    }

def save_chat_history(data):
    data['updated'] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(CHAT_HISTORY_FILE), exist_ok=True)
    with open(CHAT_HISTORY_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def broadcast_to_clients(message):
    """Broadcast message to all connected WebSocket clients"""
    with clients_lock:
        disconnected = []
        for client in chat_clients:
            try:
                client.send(json.dumps(message))
            except Exception as e:
                disconnected.append(client)
        
        for client in disconnected:
            if client in chat_clients:
                chat_clients.remove(client)

def send_to_agent(message_text):
    """Send a message to the main OpenClaw agent via CLI"""
    try:
        result = subprocess.run(
            ['openclaw', 'message', 'send', 
             '--target', TELEGRAM_TARGET,
             '--message', message_text],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if result.returncode == 0:
            return True, "Message delivered"
        else:
            error = result.stderr or result.stdout or "Unknown error"
            return False, error
            
    except subprocess.TimeoutExpired:
        return False, "Timeout sending message"
    except Exception as e:
        return False, str(e)

# ==================== HTTP ROUTES ====================

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
    return jsonify(load_chat_history())

@app.route('/api/chat/clear', methods=['POST'])
def clear_chat():
    history = {
        "messages": [],
        "updated": datetime.now().isoformat()
    }
    save_chat_history(history)
    broadcast_to_clients({"type": "clear"})
    return jsonify({"status": "ok"})

@app.route('/api/chat/send', methods=['POST'])
def send_chat_message_http():
    """HTTP endpoint to send messages"""
    data = request.json
    text = data.get('text', '').strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400
    
    # Add user message to history
    history = load_chat_history()
    message = {
        "id": f"me_{int(time.time() * 1000)}",
        "text": text,
        "from": "me",
        "timestamp": datetime.now().isoformat()
    }
    history["messages"].append(message)
    save_chat_history(history)
    
    # Send to agent via CLI
    success, result = send_to_agent(text)
    
    # Broadcast to all connected clients
    broadcast_to_clients({
        "type": "message",
        "message": message
    })
    
    if success:
        return jsonify({"status": "ok", "message": message})
    else:
        return jsonify({"status": "error", "error": result}), 500

@app.route('/api/gateway/status')
def gateway_status():
    """Check Gateway connection status"""
    return jsonify({
        "connected": True,  # We use CLI which is always "connected" if openclaw works
        "mode": "cli",
        "target": TELEGRAM_TARGET
    })

# ==================== BROWSER WEBSOCKET ====================

@sock.route('/ws/chat')
def chat_websocket(ws):
    """WebSocket endpoint for browser clients"""
    with clients_lock:
        chat_clients.append(ws)
    
    # Send chat history to new client
    history = load_chat_history()
    try:
        ws.send(json.dumps({
            "type": "init",
            "gateway_connected": True,
            "messages": history.get("messages", [])
        }))
    except Exception as e:
        print(f"[WebSocket] Error sending init: {e}")
    
    print(f"[WebSocket] Browser client connected")
    
    # Listen for messages from browser
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
                    history = load_chat_history()
                    message = {
                        "id": f"me_{int(time.time() * 1000)}",
                        "text": text,
                        "from": "me",
                        "timestamp": datetime.now().isoformat()
                    }
                    history["messages"].append(message)
                    save_chat_history(history)
                    
                    # Send to agent via CLI
                    success, result = send_to_agent(text)
                    
                    # Broadcast to all connected browsers
                    broadcast_to_clients({
                        "type": "message",
                        "message": message
                    })
                    
                    if not success:
                        ws.send(json.dumps({
                            "type": "error",
                            "error": result
                        }))
            
            elif msg_type == 'ping':
                ws.send(json.dumps({"type": "pong"}))
            
            elif msg_type == 'clear':
                history = {
                    "messages": [],
                    "updated": datetime.now().isoformat()
                }
                save_chat_history(history)
                broadcast_to_clients({"type": "clear"})
                
        except Exception as e:
            print(f"[WebSocket] Browser error: {e}")
            break
    
    # Clean up
    with clients_lock:
        if ws in chat_clients:
            chat_clients.remove(ws)
    print("[WebSocket] Browser client disconnected")

# ==================== STATIC FILES ====================

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('.', path)

# ==================== MAIN ====================

if __name__ == '__main__':
    if not os.path.exists(CHAT_HISTORY_FILE):
        save_chat_history({"messages": []})
    
    print("="*60)
    print("[Mission Control] Starting server")
    print("="*60)
    print(f"[Mission Control] URL: http://0.0.0.0:8080")
    print(f"[Mission Control] Chat WebSocket: ws://localhost:8080/ws/chat")
    print(f"[Mission Control] Mode: CLI (via openclaw message send)")
    print("="*60)
    
    # Run with threading support for WebSocket
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
