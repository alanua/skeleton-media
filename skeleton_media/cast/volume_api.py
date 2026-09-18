#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

HOME = Path(os.environ.get('SKELETON_MEDIA_HOME', str(Path.home()))).expanduser()
HOST = os.environ.get('SKELETON_MEDIA_VOLUME_HOST', '127.0.0.1')
PORT = 8101
POLICY = os.environ.get('SKELETON_MEDIA_VOLUME_POLICY', str(HOME / '.local/bin/home-edge-volume-policy'))
SYSTEMCTL = '/usr/bin/systemctl'
REGISTRY = Path(os.environ.get('SKELETON_MEDIA_DEVICE_REGISTRY', str(HOME / '.config/skeleton/device-registry/confirmed.yaml'))).expanduser()
STATE = Path(os.environ.get('SKELETON_MEDIA_VOLUME_STATE', str(HOME / '.local/state/skeleton/volume-policy.json'))).expanduser()
ALLOWED_ORIGINS = {item.strip() for item in os.environ.get('SKELETON_MEDIA_ALLOWED_ORIGINS', 'http://127.0.0.1:8100').split(',') if item.strip()}


def phone_ip() -> str:
    try:
        data = yaml.safe_load(REGISTRY.read_text(encoding='utf-8')) or {}
        return str(data['devices'][os.environ.get('SKELETON_MEDIA_PHONE_DEVICE_ID', 'primary_phone')]['identifiers'].get('ipv4') or os.environ.get('SKELETON_MEDIA_PHONE_IP', '127.0.0.1'))
    except Exception:
        return os.environ.get('SKELETON_MEDIA_PHONE_IP', '127.0.0.1')


def trusted(ip: str) -> bool:
    return ip in {'127.0.0.1', '::1'}


def status() -> dict:
    result = subprocess.run([POLICY, 'status'], text=True, capture_output=True, timeout=10, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'volume status failed').strip()[-500:])
    data = json.loads(result.stdout)
    try:
        saved = json.loads(STATE.read_text(encoding='utf-8'))
        data['last_nonzero'] = int(saved.get('last_nonzero') or 25)
    except Exception:
        data['last_nonzero'] = int(data.get('master') or 25) or 60
    return data


def set_level(level: int) -> dict:
    if not 0 <= level <= 100:
        raise ValueError('Дозволено 0–100%.')
    result = subprocess.run([POLICY, 'set', str(level)], text=True, capture_output=True, timeout=6, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'volume set failed').strip()[-500:])
    try:
        saved = json.loads(STATE.read_text(encoding='utf-8'))
    except Exception:
        saved = {'master': level, 'last_nonzero': level or 25}
    policy_status = subprocess.run([POLICY, 'status'], text=True, capture_output=True, timeout=4, check=False)
    try:
        observed = json.loads(policy_status.stdout or '{}') if policy_status.returncode == 0 else {}
    except Exception:
        observed = {}
    current = {
        'master': level,
        'last_nonzero': int(saved.get('last_nonzero') or level or 25),
        'range': [0, 100],
        'mode': str(observed.get('mode') or 'unknown'),
        'pipewire': int(observed.get('pipewire') if observed.get('pipewire') is not None else level),
        'pipewire_muted': bool(observed.get('pipewire_muted', level == 0)),
    }
    return current


class Handler(BaseHTTPRequestHandler):
    server_version = 'SkeletonVolume/1.0'

    def log_message(self, fmt: str, *args) -> None:
        return

    def cors(self) -> None:
        origin = self.headers.get('Origin')
        if origin in ALLOWED_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')

    def reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def allowed(self) -> bool:
        if not trusted(self.client_address[0]):
            self.reply(403, {'error': 'Доступ дозволено лише локальному Skeleton Cast.'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in ALLOWED_ORIGINS:
            self.reply(403, {'error': 'Недозволене джерело запиту.'})
            return False
        return True

    def do_OPTIONS(self) -> None:
        if not self.allowed():
            return
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self) -> None:
        if not self.allowed():
            return
        if self.path != '/api/volume':
            self.reply(404, {'error': 'not found'})
            return
        try:
            self.reply(200, status())
        except Exception as exc:
            self.reply(503, {'error': str(exc)})

    def do_POST(self) -> None:
        if not self.allowed():
            return
        if self.path != '/api/volume':
            self.reply(404, {'error': 'not found'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length < 1 or length > 1024:
                raise ValueError('Некоректний запит.')
            data = json.loads(self.rfile.read(length))
            level = int(data.get('level'))
            self.reply(200, {'status': 'ok', **set_level(level)})
        except ValueError as exc:
            self.reply(400, {'error': str(exc)})
        except Exception as exc:
            self.reply(503, {'error': str(exc)})


if __name__ == '__main__':
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
