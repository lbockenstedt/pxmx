"""Console/shell relay for the unified pxmx agent.

Free-function extraction of ``ProxmoxAgent``'s VNC console + interactive
host-shell (xterm) relay (the pattern ``cs_commands``/``usb_provision`` use:
each function takes the ``agent`` instance as its first argument). The
``ProxmoxAgent`` keeps thin wrapper methods delegating here so the
``_connect_once`` dispatch chain (``self.<method>``) is untouched.

The hub→spoke→agent VNC_START opens a Proxmox ``vncwebsocket`` HERE (local
root-authed API token) and relays frames both ways over the existing
agent↔spoke WS; SHELL_START spawns a PTY ``bash`` on this node and relays it the
same way (SHELL_OUT = VNC_FRAME_UP, SHELL_IN = VNC_FRAME_DOWN).
"""

import asyncio
import base64
import os
import time
import uuid
from typing import Any, Dict

from .security_utils import encode_frame
from . import pve_cmds

import logging

logger = logging.getLogger("PxmxAgent")


async def send_vnc_event(agent, event_type: str, data: Dict[str, Any]):
    """Emit a VNC_* frame up to the spoke for relay to the hub's browser WS.

    ``event_type`` is one of VNC_FRAME_UP / VNC_READY / VNC_ERROR /
    VNC_DISCONNECT. Best-effort and never raises — a dropped up-frame is
    tolerable (the browser RFB reconnects or times out); the Proxmox→hub
    relay task must not die on a transient socket blip. Mirrors
    ``send_cs_event`` but does not inject hostname/agent_id (the spoke
    already keys the relay by the connected agent_id)."""
    try:
        msg = {
            "header": {
                "message_id":    str(uuid.uuid4()),
                "timestamp":     time.time(),
                "sender_id":     agent.agent_id,
                "destination_id": "pxmx-spoke",
            },
            "payload": {"type": event_type, "data": data},
        }
        await agent.websocket.send(encode_frame(agent.signer, msg))
    except Exception:
        pass


# ── VNC console session orchestration ─────────────────────────────────────
# The hub→spoke→agent VNC_START opens a Proxmox vncwebsocket HERE (local
# root-authed API token) and relays frames both ways over the existing
# agent↔spoke WS. See the plan in .claude/plans/purring-singing-breeze.md.

async def ensure_console_token(agent) -> str:
    """Provision (once, cached) the root@pam!lm-vnc Proxmox API token used
    to create the vncproxy AND authenticate the vncwebsocket. Proxmox never
    reveals a token secret after creation, so we delete+create to get a
    fresh secret on first use and cache it in memory for the agent's
    lifetime. The secret is never logged (only its existence)."""
    if agent._console_token:
        return agent._console_token
    TOKEN_ID = "lm-vnc"
    USER = "root@pam"
    try:
        await agent._pvesh_action(
            "delete", f"/access/users/{USER}/token/{TOKEN_ID}",
            json_out=False, timeout=10)
    except Exception:
        pass  # token may not exist yet — expected
    data = await agent._pvesh_action(
        "create", f"/access/users/{USER}/token/{TOKEN_ID}",
        "--privsep", "0", timeout=20)
    secret = str((data or {}).get("value") or "").strip() if isinstance(data, dict) else ""
    if not secret:
        raise RuntimeError("pvesh returned no token value for root@pam!lm-vnc")
    agent._console_token = f"{USER}!{TOKEN_ID}={secret}"
    logger.info("Proxmox console token root@pam!lm-vnc provisioned (value not logged)")
    return agent._console_token


async def start_vnc_session(agent, session_id: str, vmid: Any,
                            node: str, kind: str) -> str:
    """Open the Proxmox WSS for a session and spawn the relay tasks.

    Awaited synchronously by the VNC_START handler so the Proxmox ``ticket``
    (which doubles as the RFB VNC password the browser's noVNC must present
    during the security handshake) is returned to the hub in the VNC_START
    response — without it, noVNC authenticates with an empty password and
    Proxmox drops the RFB session ("Security failure" / blank console).
    The vncproxy POST + WSS open is ~1-2s (one-shot, user-initiated), an
    acceptable block of the dispatch loop — the high-volume frame relay
    stays non-blocking. Emits VNC_READY on success or VNC_ERROR on failure.
    Down-frames buffered in the session's down_q are drained to the WSS
    once it's open. Returns the ticket string; raises on failure."""
    sess = agent._vnc_sessions.get(session_id)
    if not sess:
        raise RuntimeError(f"no VNC session record for {session_id}")
    k = (kind or "").lower()
    if k not in ("qemu", "lxc"):
        k = await pve_cmds.detect_guest_type(int(vmid))
    token = await ensure_console_token(agent)
    px_ws, ticket, _port = await pve_cmds.open_vnc_ws(vmid, node, k, token)
    sess["px_ws"] = px_ws
    sess["ticket"] = ticket
    up_task = asyncio.create_task(vnc_proxmox_to_hub(agent, session_id, px_ws))
    drain_task = asyncio.create_task(vnc_drain_down(agent, session_id, px_ws, sess["down_q"]))
    sess["tasks"] = [up_task, drain_task]
    await send_vnc_event(agent, "VNC_READY", {"session_id": session_id})
    logger.info(f"VNC session {session_id} ready (vmid={vmid} node={node} kind={k})")
    return ticket


async def vnc_proxmox_to_hub(agent, session_id: str, px_ws) -> None:
    """Relay Proxmox→browser frames. When the Proxmox WSS closes (VM
    stopped, ticket expired, admin disconnect), the loop exits and we tear
    the session down + tell the hub (VNC_DISCONNECT) so the browser WS closes."""
    try:
        async for raw in px_ws:
            if isinstance(raw, str):
                raw = raw.encode()
            await send_vnc_event(
                agent, "VNC_FRAME_UP",
                {"session_id": session_id,
                 "data": base64.b64encode(raw).decode()})
    except Exception:
        pass
    finally:
        await vnc_teardown(agent, session_id, send_disconnect=True)


async def vnc_drain_down(agent, session_id: str, px_ws, down_q: asyncio.Queue) -> None:
    """Forward buffered browser→Proxmox frames to the WSS. A ``None`` sentinel
    (put by teardown) breaks the loop so the task exits cleanly."""
    try:
        while True:
            raw = await down_q.get()
            if raw is None:
                break
            await px_ws.send(raw)
    except Exception:
        pass


async def vnc_teardown(agent, session_id: str, send_disconnect: bool) -> None:
    """Close the Proxmox WSS, cancel the relay tasks, drop the session.
    ``send_disconnect`` is False when the hub initiated the close (it
    already knows) and True when the Proxmox side closed (the hub needs the
    signal to close the browser WS)."""
    sess = agent._vnc_sessions.pop(session_id, None)
    if not sess:
        return
    down_q = sess.get("down_q")
    if down_q is not None:
        try:
            down_q.put_nowait(None)
        except Exception:
            pass
    for task in sess.get("tasks", []):
        if not task.done():
            task.cancel()
    px_ws = sess.get("px_ws")
    if px_ws is not None:
        try:
            await px_ws.close()
        except Exception:
            pass
    if send_disconnect:
        await send_vnc_event(agent, "VNC_DISCONNECT", {"session_id": session_id})


# ── Interactive host-shell (xterm terminal) session orchestration ─────────
# SHELL_START spawns a PTY `bash` on THIS Proxmox node and relays it to the
# browser over the same agent↔spoke WS, exactly like the VNC console (SHELL_OUT
# = VNC_FRAME_UP, SHELL_IN = VNC_FRAME_DOWN). Runs as the agent's user (root),
# so the hub gates it to Global/Tenant admins + an opt-in toggle + audit.
async def send_shell_event(agent, event_type: str, data: Dict[str, Any]):
    """Emit a SHELL_* frame up to the spoke → hub browser WS. Mirrors
    send_vnc_event; best-effort (a dropped frame must not kill the relay)."""
    try:
        msg = {
            "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                       "sender_id": agent.agent_id, "destination_id": "pxmx-spoke"},
            "payload": {"type": event_type, "data": data},
        }
        await agent.websocket.send(encode_frame(agent.signer, msg))
    except Exception:
        pass


async def start_shell_session(agent, session_id: str) -> None:
    """Spawn a login PTY bash on this node and start the read relay. Raises on
    failure (the SHELL_START handler surfaces it)."""
    import pty
    sess = agent._shell_sessions.get(session_id)
    if not sess:
        raise RuntimeError(f"no shell session record for {session_id}")
    master_fd, slave_fd = pty.openpty()
    env = {**os.environ, "TERM": "xterm-256color"}
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-il", stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        start_new_session=True, env=env)
    os.close(slave_fd)
    sess["master_fd"] = master_fd
    sess["proc"] = proc
    up_task = asyncio.create_task(shell_pty_to_hub(agent, session_id, master_fd))
    wait_task = asyncio.create_task(shell_wait(agent, session_id, proc))
    sess["tasks"] = [up_task, wait_task]
    await send_shell_event(agent, "SHELL_READY", {"session_id": session_id})
    logger.info("shell session %s ready (pid=%s)", session_id, proc.pid)


def blocking_read(fd: int) -> bytes:
    try:
        return os.read(fd, 65536)
    except OSError:
        return b""


async def shell_pty_to_hub(agent, session_id: str, master_fd: int) -> None:
    """Relay PTY→browser bytes (blocking os.read in an executor so the loop
    stays responsive). EOF / read error → tear the session down."""
    loop = asyncio.get_event_loop()
    try:
        while True:
            data = await loop.run_in_executor(None, blocking_read, master_fd)
            if not data:
                break
            await send_shell_event(
                agent, "SHELL_OUT", {"session_id": session_id,
                                     "data": base64.b64encode(data).decode()})
    except Exception:
        pass
    finally:
        await shell_teardown(agent, session_id, send_disconnect=True)


async def shell_wait(agent, session_id: str, proc) -> None:
    """When bash exits (user typed `exit`), tear the session down."""
    try:
        await proc.wait()
    except Exception:
        pass
    finally:
        await shell_teardown(agent, session_id, send_disconnect=True)


def shell_write(agent, session_id: str, data: bytes) -> None:
    sess = agent._shell_sessions.get(session_id)
    fd = sess.get("master_fd") if sess else None
    if fd is not None:
        try:
            os.write(fd, data)
        except OSError:
            pass


def shell_resize(agent, session_id: str, rows: int, cols: int) -> None:
    import fcntl as _fcntl, termios as _termios, struct as _struct
    sess = agent._shell_sessions.get(session_id)
    fd = sess.get("master_fd") if sess else None
    if fd is not None:
        try:
            _fcntl.ioctl(fd, _termios.TIOCSWINSZ,
                         _struct.pack("HHHH", int(rows) or 24, int(cols) or 80, 0, 0))
        except OSError:
            pass


async def shell_teardown(agent, session_id: str, send_disconnect: bool) -> None:
    """Kill the bash proc, cancel the relay tasks, drop the session."""
    sess = agent._shell_sessions.pop(session_id, None)
    if not sess:
        return
    for task in sess.get("tasks", []):
        if not task.done():
            task.cancel()
    proc = sess.get("proc")
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    fd = sess.get("master_fd")
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    if send_disconnect:
        await send_shell_event(agent, "SHELL_DISCONNECT", {"session_id": session_id})
