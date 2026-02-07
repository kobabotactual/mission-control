#!/usr/bin/env python3
"""Mission Control - Direct OpenClaw Gateway Chat Interface

Connects directly to OpenClaw Gateway via WebSocket and CLI commands.
Messages flow:
  Browser → Mission Control → OpenClaw Gateway → Koba (main agent)
  Koba → OpenClaw Gateway → Mission Control → Browser
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

# Main agent session (target for messages)
MAIN_SESSION_ID = '78bb7fea-d3a9-4d0d-8c49-04882f598331'

# Store connected WebSocket clients
chat_clients = []
clients_lock = threading.Lock()

# Message tracking for responses
last_message_time = 0
response_callbacks = []

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
        "session_id": MAIN_SESSION_ID,
        "updated": datetime.now().isoformat()
    }

def save_chat_history(data):
    data['updated'] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(CHAT_HISTORY_FILE), exist_ok=True)
    with open(CHAT_HISTORY_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def send_via_openclaw(message_text):
    """Send a message to the main OpenClaw agent via CLI
    
    Uses the 'openclaw message send' command to deliver messages
    directly to Koba's main agent session.
    """
    try:
        result = subprocess.run(
            ['openclaw', 'message', 'send', '--message', message_text, '--json'],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        print(f"[OpenClaw] send result: rc={result.returncode}")
        
        if result.returncode == 0:
            try:
                response = json.loads(result.stdout)
                return True, response.get('message', 'Sent')
            except:
                return True, "Message sent"
        else:
            error = result.stderr or result.stdout or "Unknown error"
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
                print(f"[WebSocket] Client disconnected: {e}")
                disconnected.append(client)
        
        for client in disconnected:
            if client in chat_clients:
                chat_clients.remove(client)

def check_for_responses():
    """Check sessions for new responses from Koba
    
    This polls the sessions to see if there are new messages
    from the agent after we sent something.
    """
    global last_message_time
    
    try:
        result = subprocess.run(
            ['openclaw', 'sessions', '--json'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            data = json.loads(result.stdout)
            sessions = data.get('sessions', [])
            
            for session in sessions:
                if session.get('sessionId') == MAIN_SESSION_ID:
                    updated_at = session.get('updatedAt', 0)
                    # If session was updated since our last check
                    if updated_at > last_message_time:
                        last_message_time = updated_at
                        return True
            
    except Exception as e:
        print(f"[Poll] Error checking sessions: {e}")
    
    return False

def response_polling_loop():
    """Background thread to poll for agent responses"""
    while True:
        try:
            # Poll every 3 seconds
            if check_for_responses():
                # Session was updated - there might be a response
                # For now, we rely on the user refreshing or we could
                # implement a more sophisticated response tracking
                pass
            
            time.sleep(3)
        except Exception as e:
            print(f"[Poll] Error: {e}")
            time.sleep(5)

# Start background polling thread
polling_thread = threading.Thread(target=response_polling_loop, daemon=True)
polling_thread.start()

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
    """Clear chat history"""
    history = {
        "messages": [],
        "session_id": MAIN_SESSION_ID,
        "updated": datetime.now().isoformat()
    }
    save_chat_history(history)
    
    broadcast_to_clients({"type": "clear"})
    return jsonify({"status": "ok"})

@app.route('/api/chat/send', methods=['POST'])
def send_chat_message_http():
    """HTTP endpoint to send messages (fallback)"""
    data = request.json
    text = data.get('text', '').strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400
    
    # Add user message to chat history
    history = load_chat_history()
    message = {
        "id": f"me_{int(time.time() * 1000)}",
        "text": text,
        "from": "me",
        "timestamp": datetime.now().isoformat()
    }
    history["messages"].append(message)
    save_chat_history(history)
    
    # Send to OpenClaw
    success, result = send_via_openclaw(text)
    
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
    """WebSocket endpoint for real-time chat
    
    Bidirectional communication:
    - Client sends: {"type": "send", "text": "..."}
    - Server broadcasts: {"type": "message", "message": {...}}
    """
    with clients_lock:
        chat_clients.append(ws)
    
    # Send chat history to new client
    history = load_chat_history()
    try:
        ws.send(json.dumps({
            "type": "init",
            "session_id": MAIN_SESSION_ID,
            "messages": history.get("messages", [])
        }))
    except Exception as e:
        print(f"[WebSocket] Error sending init: {e}")
    
    print(f"[WebSocket] Client connected")
    
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
                    history = load_chat_history()
                    message = {
                        "id": f"me_{int(time.time() * 1000)}",
                        "text": text,
                        "from": "me",
                        "timestamp": datetime.now().isoformat()
                    }
                    history["messages"].append(message)
                    save_chat_history(history)
                    
                    # Send to OpenClaw
                    success, result = send_via_openclaw(text)
                    
                    # Broadcast to all connected clients
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
                    "session_id": MAIN_SESSION_ID,
                    "updated": datetime.now().isoformat()
                }
                save_chat_history(history)
                broadcast_to_clients({"type": "clear"})
                
        except Exception as e:
            print(f"[WebSocket] Error: {e}")
            break
    
    # Clean up
    with clients_lock:
        if ws in chat_clients:
            chat_clients.remove(ws)
    print("[WebSocket] Client disconnected")

# ==================== STATIC FILES ====================

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('.', path)

# ==================== MAIN ====================

if __name__ == '__main__':
    # Ensure chat history file exists
    if not os.path.exists(CHAT_HISTORY_FILE):
        save_chat_history({
            "messages": [],
            "session_id": MAIN_SESSION_ID
        })
    
    print("="*60)
    print("[Mission Control] Starting server")
    print("="*60)
    print(f"[Mission Control] URL: http://0.0.0.0:8080")
    print(f"[Mission Control] Chat WebSocket: ws://localhost:8080/ws/chat")
    print(f"[Mission Control] Target Session: {MAIN_SESSION_ID}")
    print("="*60)
    
    # Run with threading support for WebSocket
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
