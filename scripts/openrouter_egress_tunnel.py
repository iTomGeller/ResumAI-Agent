#!/usr/bin/env python3
"""Maintain an SSH remote-forward tunnel so ECS can reach OpenRouter via this host.

ECS cannot complete TLS to openrouter.ai (ClientHello hangs / RST). This machine
can. We remote-forward ECS:8443 -> openrouter.ai:443; containers resolve
openrouter.ai to the docker host and call https://openrouter.ai:8443.

Usage:
  python scripts/openrouter_egress_tunnel.py          # block & keep alive
  python scripts/openrouter_egress_tunnel.py --once    # open, print, exit check
"""
from __future__ import annotations

import argparse
import os
import select
import socket
import sys
import threading
import time

try:
    import paramiko
except ImportError as exc:
    raise SystemExit("pip install paramiko") from exc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE_BIND_PORT = 8443
TARGET_HOST = "openrouter.ai"
TARGET_PORT = 443


def load_env() -> dict:
    env = {}
    path = os.path.join(ROOT, ".deploy.local.env")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def resolve_target() -> tuple[str, int]:
    infos = socket.getaddrinfo(TARGET_HOST, TARGET_PORT, type=socket.SOCK_STREAM)
    for fam, _, _, _, sa in infos:
        if fam == socket.AF_INET:
            return sa[0], sa[1]
    raise RuntimeError("no IPv4 for " + TARGET_HOST)


def handle_channel(chan: paramiko.Channel, target_ip: str) -> None:
    try:
        sock = socket.create_connection((target_ip, TARGET_PORT), timeout=20)
    except Exception as e:
        try:
            chan.close()
        except Exception:
            pass
        return
    chan.setblocking(0)
    sock.setblocking(0)
    try:
        while True:
            r, _, x = select.select([chan, sock], [], [chan, sock], 60)
            if x:
                break
            if not r:
                continue
            if chan in r:
                data = chan.recv(65536)
                if not data:
                    break
                sock.sendall(data)
            if sock in r:
                data = sock.recv(65536)
                if not data:
                    break
                chan.sendall(data)
    except Exception:
        pass
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    env = load_env()
    host = env["ALIYUN_HOST"]
    user = env.get("ALIYUN_USER", "root")
    password = env.get("ALIYUN_PASSWORD")
    target_ip, _ = resolve_target()
    print("[tunnel] local -> openrouter %s:%s" % (target_ip, TARGET_PORT))
    print("[tunnel] remote bind 0.0.0.0:%s on %s" % (REMOTE_BIND_PORT, host))

    while True:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                host,
                username=user,
                password=password,
                look_for_keys=False,
                allow_agent=False,
                timeout=30,
                banner_timeout=30,
            )
            # Allow binding non-localhost on server side.
            ssh.get_transport().set_keepalive(30)
            transport = ssh.get_transport()
            assert transport is not None

            def acceptors() -> None:
                while True:
                    try:
                        chan = transport.accept(timeout=1)
                    except Exception:
                        break
                    if chan is None:
                        if not transport.is_active():
                            break
                        continue
                    threading.Thread(target=handle_channel, args=(chan, target_ip), daemon=True).start()

            # request_port_forward is reverse forward
            try:
                transport.request_port_forward("0.0.0.0", REMOTE_BIND_PORT)
            except Exception:
                # fallback localhost only
                transport.request_port_forward("", REMOTE_BIND_PORT)

            t = threading.Thread(target=acceptors, daemon=True)
            t.start()
            print("[tunnel] UP on ECS :%s" % REMOTE_BIND_PORT)

            # smoke from ECS through the tunnel
            _stdin, stdout, stderr = ssh.exec_command(
                "curl -4 -sS -m 25 --resolve openrouter.ai:%s:127.0.0.1 "
                "-o /tmp/or_via_tunnel.json -w '%%{http_code}' "
                "-H 'Authorization: Bearer %s' "
                "https://openrouter.ai:%s/api/v1/models" % (
                    REMOTE_BIND_PORT,
                    (env.get("EMBEDDING_API_KEY") or env.get("OPENROUTER_API_KEY") or ""),
                    REMOTE_BIND_PORT,
                ),
                timeout=40,
            )
            code = stdout.read().decode().strip()
            err = stderr.read().decode("utf-8", "replace")
            print("[tunnel] smoke HTTP", code, err[:200] if err else "")
            if args.once:
                ssh.close()
                return 0 if code.startswith("2") else 1

            while transport.is_active():
                time.sleep(5)
        except KeyboardInterrupt:
            print("[tunnel] stop")
            return 0
        except Exception as e:
            print("[tunnel] error, retry in 5s:", type(e).__name__, e)
            time.sleep(5)
        finally:
            try:
                ssh.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
