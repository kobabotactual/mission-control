#!/usr/bin/env python3
"""Mission Control - Real-time Kanban with Chat"""
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock
import json
import os
import subprocess
import threading
from datetime import datetime
import time

app = Flask(__name__)
sock = Sock(app)
DATA_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/data.json'
CHAT_HISTORY_FILE = '/home/ubuntu/.openclaw/workspace/mission-control/chat_history.json'
TELEGRAM_USER_ID = '6301634831'

# Store connected WebSocket clients
chat_clients = []

# Message queue for incoming Telegram messages
message_queue = []
queue_lock = threading.Lock()

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
    return {"messages": [], "updated": datetime.now().isoformat()}

def save_chat_history(data):
    data['updated'] = datetime.now().isoformat()
    with open(CHAT_HISTORY_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def send_telegram_message(text):
    """Send a message to Telegram via OpenClaw CLI"""
    try:
        # Use openclaw message tool to send to Telegram
        result = subprocess.run(
            ['openclaw', 'message', 'send', '--target', TELEGRAM_USER_ID, '--message', text],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return True, "Sent"
        else:
            return False, result.stderr or "Unknown error"
    except Exception as e:
        return False, str(e)

def broadcast_to_clients(message):
    """Broadcast message to all connected WebSocket clients"""
    disconnected = []
    for client in chat_clients:
        try:
            client.send(json.dumps(message))
        except Exception:
            disconnected.append(client)
    
    # Clean up disconnected clients
    for client in disconnected:
        if client in chat_clients:
            chat_clients.remove(client)

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

@app.route('/api/chat/send', methods=['POST'])
def send_chat_message():
    data = request.json
    text = data.get('text', '').strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400
    
    # Add to chat history
    history = load_chat_history()
    message = {
        "id": f"out_{int(time.time() * 1000)}",
        "text": text,
        "from": "me",
        "timestamp": datetime.now().isoformat()
    }
    history["messages"].append(message)
    save_chat_history(history)
    
    # Send to Telegram
    success, error = send_telegram_message(text)
    
    # Broadcast to all connected clients
    broadcast_to_clients({
        "type": "message",
        "message": message
    })
    
    if success:
        return jsonify({"status": "ok", "message": message})
    else:
        return jsonify({"status": "error", "error": error}), 500

@sock.route('/ws/chat')
def chat_websocket(ws):
    """WebSocket endpoint for real-time chat"""
    chat_clients.append(ws)
    
    # Send chat history to new client
    history = load_chat_history()
    try:
        ws.send(json.dumps({
            "type": "history",
            "messages": history.get("messages", [])
        }))
    except Exception:
        pass
    
    # Keep connection alive and listen for messages
    while True:
        try:
            # Receive messages from client (browser -> server)
            raw_data = ws.receive()
            if raw_data is None:
                break
            
            data = json.loads(raw_data)
            
            if data.get('type') == 'send':
                text = data.get('text', '').strip()
                if text:
                    # Add to chat history
                    history = load_chat_history()
                    message = {
                        "id": f"out_{int(time.time() * 1000)}",
                        "text": text,
                        "from": "me",
                        "timestamp": datetime.now().isoformat()
                    }
                    history["messages"].append(message)
                    save_chat_history(history)
                    
                    # Send to Telegram
                    success, error = send_telegram_message(text)
                    
                    # Broadcast to all connected clients
                    broadcast_to_clients({
                        "type": "message",
                        "message": message
                    })
            
            elif data.get('type') == 'pong':
                # Client is alive
                pass
                
        except Exception as e:
            print(f"WebSocket error: {e}")
            break
    
    # Clean up
    if ws in chat_clients:
        chat_clients.remove(ws)

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('.', path)

if __name__ == '__main__':
    # Ensure chat history file exists
    if not os.path.exists(CHAT_HISTORY_FILE):
        save_chat_history({"messages": []})
    
    # Run with threading support for WebSocket
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
