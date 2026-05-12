#!/usr/bin/env python3
import sys
import json
import threading
from flask import Flask, request
from datetime import datetime
import subprocess
import os

app = Flask(__name__)
handler_module = None

def load_handler(handler_path):
    """핸들러 모듈 로드"""
    global handler_module
    import importlib.util
    spec = importlib.util.spec_from_file_location("handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler_module)
    handler_module.send_message("🚀 두삼이 관제 시스템 가동 시작")

@app.route('/api/1/vehicles/<vehicle_id>/telemetry', methods=['POST'])
def receive_telemetry(vehicle_id):
    """테슬라 Telemetry 데이터 수신"""
    try:
        data = request.get_json()
        if data and handler_module:
            handler_module.process_data(data)
        return {'status': 'ok'}, 200
    except Exception as e:
        print(f"Error: {e}")
        return {'error': str(e)}, 400

@app.route('/health', methods=['GET'])
def health():
    return {'status': 'healthy'}, 200

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python telemetry_server.py <handler_path>")
        sys.exit(1)
    
    handler_path = sys.argv[1]
    load_handler(handler_path)
    
    print("🌐 Server listening on 0.0.0.0:8080")
    app.run(host='0.0.0.0', port=8080, debug=False)
