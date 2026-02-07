#!/usr/bin/env python3
"""Mission Control - Real-time Kanban with OpenClaw Chat via CLI

This server provides:
1. Kanban board API for task management
2. Chat that uses OpenClaw CLI to communicate with Koba

Message Flow:
  Browser → Mission Control (WebSocket) → openclaw CLI → Koba
  (Receiving messages via file polling since Gateway WebSocket doesn't exist)
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock
import json
import os
import threading
import time
import subprocess
from datetime import datetime

app = Flask(__name__)
sock = Sock(app)

# File paths
DATA_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/data.json'
CHAT_HISTORY_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/chat_history.json'
INBOX_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/inbox.json'

# Session configuration - main session to communicate with
MAIN_SESSION_KEY = 'agent:main:main'

# Connected browser clients
browser_clients = []
browser_clients_lock = threading.Lock()

# Chat history
chat_history = []
history_lock = threading.Lock()

# Inbox for messages from Koba (since we can't use Gateway WebSocket)
# Other processes can write to inbox.json to send messages to Mission Control
inbox_messages = []
inbox_lock = threading.Lock()


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


def send_to_koba(text):
    """Send a message to Koba using OpenClaw CLI"""
    try:
        # Use subprocess to call openclaw sessions send
        result = subprocess.run(
            ['openclaw', 'sessions', 'send', 
             '--sessionKey', MAIN_SESSION_KEY, 
             '--message', text],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            print(f"[CLI] Message sent to {MAIN_SESSION_KEY}")
            return True
        else:
            print(f"[CLI] Send failed: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("[CLI] Send timeout")
        return False
    except Exception as e:
        print(f"[CLI] Error sending message: {e}")
        return False


def poll_inbox():
    """Poll inbox file for messages from Koba (since Gateway WebSocket doesn't exist)
    
    Other processes/scripts can write to inbox.json to send messages to Mission Control:
    {
        "messages": [
            {"text": "Hello from Koba", "timestamp": "...", "id": "unique_id"}
        ]
    }
    """
    global inbox_messages
    last_check = 0
    
    while True:
        try:
            if os.path.exists(INBOX_FILE):
                mtime = os.path.getmtime(INBOX_FILE)
                if mtime > last_check:
                    last_check = mtime
                    with open(INBOX_FILE, 'r') as f:
                        data = json.load(f)
                        new_messages = data.get('messages', [])
                        
                        # Find messages we haven't processed yet
                        with inbox_lock:
                            existing_ids = {m.get('id') for m in inbox_messages}
                            for msg in new_messages:
                                if msg.get('id') not in existing_ids:
                                    inbox_messages.append(msg)
                                    # Add to chat history and broadcast
                                    chat_msg = {
                                        "id": msg.get('id') or f"koba_{int(time.time() * 1000)}",
                                        "text": msg.get('text', ''),
                                        "from": "koba",
                                        "timestamp": msg.get('timestamp') or datetime.now().isoformat()
                                    }
                                    add_message(chat_msg)
                                    broadcast_to_browsers({
                                        "type": "message",
                                        "message": chat_msg
                                    })
                                    print(f"[Inbox] Received message from Koba: {msg.get('text', '')[:50]}...")
        except json.JSONDecodeError:
            print("[Inbox] Invalid JSON in inbox file")
        except Exception as e:
            print(f"[Inbox] Error polling inbox: {e}")
        
        time.sleep(2)  # Poll every 2 seconds


def start_inbox_polling():
    """Start inbox polling in background thread"""
    thread = threading.Thread(target=poll_inbox, daemon=True)
    thread.start()
    return thread


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


@app.route('/api/chat/clear', methods=['POST'])
def clear_chat():
    """Clear chat history"""
    global chat_history
    with history_lock:
        chat_history = []
    save_chat_history()
    broadcast_to_browsers({"type": "clear"})
    return jsonify({"status": "ok"})


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
    
    # Send to Koba via CLI
    sent = send_to_koba(text)
    
    # Broadcast to browsers
    broadcast_to_browsers({
        "type": "message",
        "message": msg
    })
    
    return jsonify({
        "status": "ok" if sent else "failed",
        "message": msg,
        "sent": sent
    })


@app.route('/api/gateway/status')
def gateway_status():
    """Check Gateway connection status"""
    return jsonify({
        "connected": True,  # CLI is always available
        "mode": "cli",
        "session": MAIN_SESSION_KEY
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
            "type": "init",
            "gateway_connected": True,
            "messages": chat_history
        }))
    except Exception as e:
        print(f"[Browser] Failed to send init: {e}")
    
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
                    
                    # Send to Koba via CLI
                    success = send_to_koba(text)
                    
                    # Broadcast to all browsers
                    broadcast_to_browsers({
                        "type": "message",
                        "message": msg
                    })
                    
                    if not success:
                        ws.send(json.dumps({
                            "type": "error",
                            "error": "Failed to send message"
                        }))
            
            elif msg_type == 'ping':
                ws.send(json.dumps({"type": "pong"}))
            
            elif msg_type == 'clear':
                with history_lock:
                    chat_history.clear()
                save_chat_history()
                broadcast_to_browsers({"type": "clear"})
                
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
    print(f"[Mission Control] Session: {MAIN_SESSION_KEY}")
    print("=" * 60)
    
    # Load history
    load_chat_history()
    
    # Start inbox polling for messages from Koba
    start_inbox_polling()
    print("[Mission Control] Inbox polling started")
    
    # Start Flask server
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
