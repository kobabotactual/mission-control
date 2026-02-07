#!/usr/bin/env python3
"""Mission Control - Direct Chat Interface

Real-time chat that connects to OpenClaw Gateway.
Messages are delivered directly to the main agent session.
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock
import json
import os
import subprocess
import threading
import time
from datetime import datetime

app = Flask(__name__)
sock = Sock(app)

# File paths
DATA_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/data.json'
CHAT_HISTORY_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/chat_history.json'
SESSION_FILE = '/home/ubuntu/.openclaw/agents/main/sessions/78bb7fea-d3a9-4d0d-8c49-04882f598331.jsonl'

# Store connected WebSocket clients
chat_clients = []
clients_lock = threading.Lock()

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

def send_to_agent(message_text):
    """Send a message to the main OpenClaw agent
    
    Uses the message send command to deliver to the main session.
    This bypasses Telegram by using direct Gateway delivery.
    """
    try:
        # Try to send via openclaw message command without channel
        # This should use the default delivery method (Gateway if available)
        result = subprocess.run(
            ['openclaw', 'message', 'send', 
             '--target', 'telegram:6301634831',  # Main user
             '--message', message_text,
             '--json'],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if result.returncode == 0:
            return True, "Message delivered to Koba"
        else:
            error = result.stderr or "Unknown error"
            return False, error
            
    except subprocess.TimeoutExpired:
        return False, "Timeout sending message"
    except Exception as e:
        return False, str(e)

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
def send_chat_message():
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
    
    # Send to agent
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

# ==================== WEBSOCKET ====================

@sock.route('/ws/chat')
def chat_websocket(ws):
    """WebSocket endpoint for real-time chat"""
    with clients_lock:
        chat_clients.append(ws)
    
    # Send chat history to new client
    history = load_chat_history()
    try:
        ws.send(json.dumps({
            "type": "init",
            "messages": history.get("messages", [])
        }))
    except Exception:
        pass
    
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
                    history = load_chat_history()
                    message = {
                        "id": f"me_{int(time.time() * 1000)}",
                        "text": text,
                        "from": "me",
                        "timestamp": datetime.now().isoformat()
                    }
                    history["messages"].append(message)
                    save_chat_history(history)
                    
                    # Send to agent
                    success, result = send_to_agent(text)
                    
                    # Broadcast to all clients
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
                
        except Exception:
            break
    
    # Clean up
    with clients_lock:
        if ws in chat_clients:
            chat_clients.remove(ws)

# ==================== STATIC ====================

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('.', path)

# ==================== MAIN ====================

if __name__ == '__main__':
    if not os.path.exists(CHAT_HISTORY_FILE):
        save_chat_history({"messages": []})
    
    print("="*60)
    print("[Mission Control] Starting on http://0.0.0.0:8080")
    print("[Mission Control] Chat WebSocket: ws://localhost:8080/ws/chat")
    print("="*60)
    
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
