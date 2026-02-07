#!/usr/bin/env python3
"""Mission Control - Direct OpenClaw Gateway Chat Interface

Connects directly to OpenClaw Gateway via WebSocket for bidirectional communication.
Messages flow:
  Browser → Mission Control → OpenClaw Gateway (WebSocket) → Koba (main agent)
  Koba → OpenClaw Gateway → Mission Control (WebSocket) → Browser
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock
import json
import os
import threading
import time
from datetime import datetime

# Optional websocket-client for Gateway connection
try:
    import websocket
    # websocket.enableTrace(True)  # Debug tracing (disable for production)
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

# Store connected WebSocket clients (browsers)
chat_clients = []
clients_lock = threading.Lock()

# Gateway WebSocket connection
gateway_ws = None
gateway_connected = False
gateway_hello_ok = False
main_session_id = None

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
        "session_id": None,
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
                print(f"[WebSocket] Client disconnected: {e}")
                disconnected.append(client)
        
        for client in disconnected:
            if client in chat_clients:
                chat_clients.remove(client)

# ==================== GATEWAY WEBSOCKET ====================

def send_connect_message(ws):
    """Send connect message to Gateway after receiving challenge"""
    params = {
        "minProtocol": 3,
        "maxProtocol": 3,
        "client": {
            "id": "webchat-ui",
            "displayName": "Mission Control Chat",
            "version": "1.0.0",
            "platform": "linux",
            "mode": "ui"
        },
        "role": "operator",
        "scopes": ["operator.admin"],
        "auth": {
            "token": GATEWAY_TOKEN
        }
    }
    
    print(f"[Gateway] Sending connect message")
    ws.send(json.dumps({
        "type": "req",
        "id": f"connect_{int(time.time() * 1000)}",
        "method": "connect",
        "params": params
    }))

def on_gateway_open(ws):
    """Called when Gateway WebSocket connects"""
    global gateway_connected
    gateway_connected = True
    print("[Gateway] on_gateway_open called - Connected to OpenClaw Gateway")

def on_gateway_message(ws, message):
    """Called when message received from Gateway"""
    global gateway_connected, gateway_hello_ok, main_session_id
    
    try:
        data = json.loads(message)
        msg_type = data.get('type')
        event_type = data.get('event')
        
        print(f"[Gateway] Received: {msg_type} / {event_type}")
        
        if msg_type == 'event' and event_type == 'connect.challenge':
            print(f"[Gateway] Challenge received, sending connect")
            send_connect_message(ws)
        
        elif msg_type == 'res' or msg_type == 'response':
            result = data.get('result', {}) or data.get('payload', {})
            error = data.get('error')
            
            if error:
                print(f"[Gateway] Error response: {error}")
                return
            
            # Check if this is a connect response (hello-ok)
            if result.get('type') == 'hello-ok':
                gateway_hello_ok = True
                main_session_id = "main"  # Use main session
                print("[Gateway] Connected and authenticated (hello-ok)")
                
                # Notify browsers
                broadcast_to_clients({
                    "type": "gateway_status",
                    "connected": True,
                    "session_id": main_session_id
                })
        
        elif msg_type == 'event' and event_type == 'message':
            # Direct message from agent
            payload = data.get('payload', {})
            msg_data = {
                "id": f"koba_{int(time.time() * 1000)}",
                "text": payload.get('message', payload.get('text', '')),
                "from": payload.get('from', 'koba'),
                "timestamp": datetime.now().isoformat()
            }
            
            # Save to history
            history = load_chat_history()
            history["messages"].append(msg_data)
            save_chat_history(history)
            
            # Broadcast to browsers
            broadcast_to_clients({
                "type": "message",
                "message": msg_data
            })
            print(f"[Gateway] Message from Koba: {msg_data['text'][:50]}...")
        
        elif msg_type == 'event' and event_type == 'agent':
            # Agent event (streaming response)
            payload = data.get('payload', {})
            stream = payload.get('stream', '')
            run_id = payload.get('runId', '')
            
            # Only capture the final assistant message when stream ends
            if stream == 'assistant' and 'data' in payload:
                data_field = payload.get('data', {})
                text = data_field.get('text', '')
                
                # Hide typing indicator when response starts
                broadcast_to_clients({
                    "type": "typing",
                    "active": False
                })
                # Check if this is the final message (no delta means complete)
                is_complete = 'delta' not in data_field or not data_field['delta']
                
                if text:
                    history = load_chat_history()
                    
                    # Check if we already have a message for this runId
                    existing_idx = None
                    for i, msg in enumerate(history["messages"]):
                        if msg.get("from") == "koba" and msg.get("run_id") == run_id:
                            existing_idx = i
                            break
                    
                    if existing_idx is not None:
                        # Update existing message
                        history["messages"][existing_idx]["text"] = text
                        if is_complete:
                            history["messages"][existing_idx]["complete"] = True
                            del history["messages"][existing_idx]["run_id"]
                        save_chat_history(history)
                        
                        # Broadcast update
                        broadcast_to_clients({
                            "type": "message_update",
                            "message": history["messages"][existing_idx]
                        })
                    else:
                        # Create new message
                        msg_data = {
                            "id": f"koba_{int(time.time() * 1000)}",
                            "text": text,
                            "from": "koba",
                            "timestamp": datetime.now().isoformat(),
                            "run_id": run_id
                        }
                        if is_complete:
                            msg_data["complete"] = True
                        else:
                            msg_data["streaming"] = True
                        
                        history["messages"].append(msg_data)
                        save_chat_history(history)
                        
                        # Broadcast to browsers
                        broadcast_to_clients({
                            "type": "message",
                            "message": msg_data
                        })
                    
                    if is_complete:
                        print(f"[Gateway] Agent response complete: {text[:50]}...")
        
        elif msg_type == 'event' and event_type == 'tick':
            # Heartbeat from Gateway
            pass
            
    except Exception as e:
        print(f"[Gateway] Error processing message: {e}")
        import traceback
        traceback.print_exc()

def on_gateway_error(ws, error):
    """Called on Gateway WebSocket error"""
    print(f"[Gateway] WebSocket Error: {error}")
    import traceback
    traceback.print_exc()

def on_gateway_close(ws, close_status_code, close_msg):
    """Called when Gateway WebSocket closes"""
    global gateway_connected, gateway_hello_ok, main_session_id
    gateway_connected = False
    gateway_hello_ok = False
    main_session_id = None
    print(f"[Gateway] Connection closed: {close_status_code} - {close_msg}")
    
    # Notify browsers
    broadcast_to_clients({
        "type": "gateway_status",
        "connected": False,
        "session_id": None
    })

def connect_to_gateway():
    """Establish WebSocket connection to OpenClaw Gateway"""
    global gateway_ws
    
    if not WEBSOCKET_AVAILABLE:
        print("[Gateway] websocket-client not available")
        return
    
    print(f"[Gateway] Connecting to {GATEWAY_WS_URL}...")
    
    try:
        gateway_ws = websocket.WebSocketApp(
            GATEWAY_WS_URL,
            on_open=on_gateway_open,
            on_message=on_gateway_message,
            on_error=on_gateway_error,
            on_close=on_gateway_close
        )
        
        # Run in separate thread with ping settings
        print("[Gateway] Starting WebSocket thread...")
        wst = threading.Thread(
            target=lambda: gateway_ws.run_forever(ping_interval=30, ping_timeout=10),
            daemon=True
        )
        wst.start()
        print(f"[Gateway] WebSocket thread started, alive={wst.is_alive()}")
    except Exception as e:
        print(f"[Gateway] Connection error: {e}")
        import traceback
        traceback.print_exc()

def gateway_connection_loop():
    """Maintain Gateway connection with reconnection"""
    if not WEBSOCKET_AVAILABLE:
        print("[Gateway] WebSocket support not available")
        return
        
    reconnect_delay = 1
    
    while True:
        try:
            if not gateway_connected:
                print("[Gateway] Attempting connection...")
                connect_to_gateway()
                reconnect_delay = 1
            
            # Send heartbeat every 30 seconds
            if gateway_connected and gateway_ws and gateway_hello_ok:
                try:
                    gateway_ws.send(json.dumps({
                        "type": "req",
                        "id": f"ping_{int(time.time() * 1000)}",
                        "method": "ping"
                    }))
                except Exception as e:
                    print(f"[Gateway] Heartbeat failed: {e}")
            
            time.sleep(30)
            
        except Exception as e:
            print(f"[Gateway] Connection loop error: {e}")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

def send_to_gateway(message_text):
    """Send a message to the Gateway"""
    if not WEBSOCKET_AVAILABLE:
        return False, "WebSocket support not available"
    
    if not gateway_connected or not gateway_ws or not gateway_hello_ok:
        return False, "Not connected to Gateway"
    
    try:
        # Use the 'chat.send' method with correct params
        # sessionKey for main agent is 'agent:main:main'
        import uuid
        gateway_ws.send(json.dumps({
            "type": "req",
            "id": f"msg_{int(time.time() * 1000)}",
            "method": "chat.send",
            "params": {
                "sessionKey": "agent:main:main",
                "message": message_text,
                "idempotencyKey": str(uuid.uuid4())
            }
        }))
        return True, "Sent"
    except Exception as e:
        return False, str(e)

# Gateway thread will be started in main block

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
        "session_id": main_session_id,
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
    
    # Send to Gateway
    success, result = send_to_gateway(text)
    
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
        "connected": gateway_connected and gateway_hello_ok,
        "session_id": main_session_id,
        "websocket_available": WEBSOCKET_AVAILABLE
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
            "gateway_connected": gateway_connected and gateway_hello_ok,
            "session_id": main_session_id or history.get('session_id'),
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
                    
                    # Broadcast user message to all connected browsers
                    broadcast_to_clients({
                        "type": "message",
                        "message": message
                    })
                    
                    # Show typing indicator while processing
                    broadcast_to_clients({
                        "type": "typing",
                        "active": True
                    })
                    
                    # Send to Gateway
                    success, result = send_to_gateway(text)
                    
                    if not success:
                        # Hide typing indicator on error
                        broadcast_to_clients({
                            "type": "typing",
                            "active": False
                        })
                        ws.send(json.dumps({
                            "type": "error",
                            "error": result
                        }))
            
            elif msg_type == 'ping':
                ws.send(json.dumps({"type": "pong"}))
            
            elif msg_type == 'clear':
                history = {
                    "messages": [],
                    "session_id": main_session_id,
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
    # Ensure chat history file exists
    if not os.path.exists(CHAT_HISTORY_FILE):
        save_chat_history({"messages": [], "session_id": None})
    
    print("="*60)
    print("[Mission Control] Starting server")
    print("="*60)
    print(f"[Mission Control] URL: http://0.0.0.0:8080")
    print(f"[Mission Control] Chat WebSocket: ws://localhost:8080/ws/chat")
    print(f"[Mission Control] Gateway: {GATEWAY_WS_URL}")
    print(f"[Mission Control] WebSocket Available: {WEBSOCKET_AVAILABLE}")
    print("="*60)
    
    # Start Gateway connection in background (before Flask starts)
    if WEBSOCKET_AVAILABLE:
        print("[Mission Control] Starting Gateway connection thread...")
        gateway_thread = threading.Thread(target=gateway_connection_loop, daemon=True)
        gateway_thread.start()
    
    # Run with threading support for WebSocket
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
