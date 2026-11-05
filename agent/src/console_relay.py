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

# asyncio runs the relay tasks and the bash subprocess.
import asyncio
# base64 wraps binary VNC/PTY bytes so they can travel inside JSON frames.
import base64
# os handles PTY file descriptors and the shell's environment.
import os
# time and uuid fill in each outgoing frame's header (timestamp, unique message id).
import time
import uuid
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict

# encode_frame signs a message into the <sig>.<body> wire format.
from .security_utils import encode_frame
# pve_cmds holds the Proxmox helpers (guest-type detection, opening the VNC websocket).
from . import pve_cmds

# logging reports session start-up and token provisioning.
import logging

# Share the main agent logger so console messages appear with other agent output.
logger = logging.getLogger("PxmxAgent")


async def send_vnc_event(agent, event_type: str, data: Dict[str, Any]):
    """Emit a VNC_* frame up to the spoke for relay to the hub's browser WS.

    ``event_type`` is one of VNC_FRAME_UP / VNC_READY / VNC_ERROR /
    VNC_DISCONNECT. Best-effort and never raises — a dropped up-frame is
    tolerable (the browser RFB reconnects or times out); the Proxmox→hub
    relay task must not die on a transient socket blip. Mirrors
    ``send_cs_event`` but does not inject hostname/agent_id (the spoke
    already keys the relay by the connected agent_id)."""
    # Sending can fail if the spoke link is mid-reconnect; a lost frame is acceptable.
    try:
        # Build the standard signed envelope addressed to the pxmx spoke.
        msg = {
            "header": {
                "message_id":    str(uuid.uuid4()),
                "timestamp":     time.time(),
                "sender_id":     agent.agent_id,
                "destination_id": "pxmx-spoke",
            },
            "payload": {"type": event_type, "data": data},
        }
        # Sign and send it over the agent's spoke websocket.
        await agent.websocket.send(encode_frame(agent.signer, msg))
    # Swallow send errors so the relay task keeps running.
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
    # Already provisioned during this agent's lifetime: reuse it.
    if agent._console_token:
        return agent._console_token
    # Token name and owning user; the token is root-level so it can open any guest's console.
    TOKEN_ID = "lm-vnc"
    USER = "root@pam"
    # Delete any old token first, because Proxmox will not show an existing token's secret again.
    try:
        await agent._pvesh_action(
            "delete", f"/access/users/{USER}/token/{TOKEN_ID}",
            json_out=False, timeout=10)
    except Exception:
        pass  # token may not exist yet — expected
    # Create a fresh token without privilege separation (it inherits root's rights).
    data = await agent._pvesh_action(
        "create", f"/access/users/{USER}/token/{TOKEN_ID}",
        "--privsep", "0", timeout=20)
    # Pull the secret out of pvesh's JSON reply; anything unexpected yields "".
    secret = str((data or {}).get("value") or "").strip() if isinstance(data, dict) else ""
    # No secret means the token is unusable; fail loudly.
    if not secret:
        raise RuntimeError("pvesh returned no token value for root@pam!lm-vnc")
    # Cache the full API token string (user!id=secret) for later sessions.
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
    # The VNC_START handler must have created the session record first.
    sess = agent._vnc_sessions.get(session_id)
    if not sess:
        raise RuntimeError(f"no VNC session record for {session_id}")
    # Accept the caller's guest type when valid, otherwise ask Proxmox whether it is a VM or
    # container.
    k = (kind or "").lower()
    if k not in ("qemu", "lxc"):
        k = await pve_cmds.detect_guest_type(int(vmid))
    # Get (or create) the API token, then open the Proxmox VNC websocket for this guest.
    token = await ensure_console_token(agent)
    px_ws, ticket, _port = await pve_cmds.open_vnc_ws(vmid, node, k, token)
    # Keep the socket and ticket on the session record.
    sess["px_ws"] = px_ws
    sess["ticket"] = ticket
    # Start the two relay directions: Proxmox to hub, and buffered hub frames to Proxmox.
    up_task = asyncio.create_task(vnc_proxmox_to_hub(agent, session_id, px_ws))
    drain_task = asyncio.create_task(vnc_drain_down(agent, session_id, px_ws, sess["down_q"]))
    sess["tasks"] = [up_task, drain_task]
    # Tell the hub the console is ready, log it, and return the ticket (the VNC password).
    await send_vnc_event(agent, "VNC_READY", {"session_id": session_id})
    logger.info(f"VNC session {session_id} ready (vmid={vmid} node={node} kind={k})")
    return ticket


async def vnc_proxmox_to_hub(agent, session_id: str, px_ws) -> None:
    """Relay Proxmox→browser frames. When the Proxmox WSS closes (VM
    stopped, ticket expired, admin disconnect), the loop exits and we tear
    the session down + tell the hub (VNC_DISCONNECT) so the browser WS closes."""
    # Any socket error simply ends the relay; teardown happens in finally.
    try:
        # Forward every frame Proxmox sends.
        async for raw in px_ws:
            # Text frames are converted to bytes so everything is base64-encoded the same way.
            if isinstance(raw, str):
                raw = raw.encode()
            # Send the frame up as base64 inside a VNC_FRAME_UP event.
            await send_vnc_event(
                agent, "VNC_FRAME_UP",
                {"session_id": session_id,
                 "data": base64.b64encode(raw).decode()})
    # Socket closed or errored: stop relaying.
    except Exception:
        pass
    # Always tear the session down and notify the hub when the Proxmox side ends.
    finally:
        await vnc_teardown(agent, session_id, send_disconnect=True)


async def vnc_drain_down(agent, session_id: str, px_ws, down_q: asyncio.Queue) -> None:
    """Forward buffered browser→Proxmox frames to the WSS. A ``None`` sentinel
    (put by teardown) breaks the loop so the task exits cleanly."""
    # Any send error ends the drain loop.
    try:
        # Take browser frames off the queue in order and send them to Proxmox.
        while True:
            raw = await down_q.get()
            # None is the stop signal from teardown.
            if raw is None:
                break
            await px_ws.send(raw)
    # Proxmox socket closed: just stop.
    except Exception:
        pass


async def vnc_teardown(agent, session_id: str, send_disconnect: bool) -> None:
    """Close the Proxmox WSS, cancel the relay tasks, drop the session.
    ``send_disconnect`` is False when the hub initiated the close (it
    already knows) and True when the Proxmox side closed (the hub needs the
    signal to close the browser WS)."""
    # Remove the session; if it is already gone, another path has torn it down.
    sess = agent._vnc_sessions.pop(session_id, None)
    if not sess:
        return
    # Wake the drain task with the None stop signal.
    down_q = sess.get("down_q")
    if down_q is not None:
        try:
            down_q.put_nowait(None)
        except Exception:
            pass
    # Cancel any relay task that is still running.
    for task in sess.get("tasks", []):
        if not task.done():
            task.cancel()
    # Close the Proxmox websocket, ignoring errors if it is already closed.
    px_ws = sess.get("px_ws")
    if px_ws is not None:
        try:
            await px_ws.close()
        except Exception:
            pass
    # Only notify the hub when it did not start the close itself.
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
    # Same best-effort pattern as send_vnc_event.
    try:
        # Signed envelope addressed to the pxmx spoke.
        msg = {
            "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                       "sender_id": agent.agent_id, "destination_id": "pxmx-spoke"},
            "payload": {"type": event_type, "data": data},
        }
        # Sign and send over the spoke websocket.
        await agent.websocket.send(encode_frame(agent.signer, msg))
    # A dropped frame is tolerated; never kill the relay.
    except Exception:
        pass


async def start_shell_session(agent, session_id: str) -> None:
    """Spawn a login PTY bash on this node and start the read relay. Raises on
    failure (the SHELL_START handler surfaces it)."""
    # pty creates the pseudo-terminal the shell runs in; imported only when a shell is opened.
    import pty
    # The SHELL_START handler must have created the session record first.
    sess = agent._shell_sessions.get(session_id)
    if not sess:
        raise RuntimeError(f"no shell session record for {session_id}")
    # Create a PTY pair: we keep the master end, bash gets the slave end as its terminal.
    master_fd, slave_fd = pty.openpty()
    # Advertise a 256-colour xterm so programs in the shell render properly in the browser.
    env = {**os.environ, "TERM": "xterm-256color"}
    # Start an interactive login bash in its own session, attached to the PTY.
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-il", stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        start_new_session=True, env=env)
    # Our copy of the slave end is no longer needed (bash has it).
    os.close(slave_fd)
    # Remember the PTY and process on the session record.
    sess["master_fd"] = master_fd
    sess["proc"] = proc
    # Start the output relay and the "bash exited" watcher.
    up_task = asyncio.create_task(shell_pty_to_hub(agent, session_id, master_fd))
    wait_task = asyncio.create_task(shell_wait(agent, session_id, proc))
    sess["tasks"] = [up_task, wait_task]
    # Tell the hub the shell is ready and log the bash process id.
    await send_shell_event(agent, "SHELL_READY", {"session_id": session_id})
    logger.info("shell session %s ready (pid=%s)", session_id, proc.pid)


def blocking_read(fd: int) -> bytes:
    """Read up to 64 KiB from a file descriptor, returning b"" on error (treated as EOF)."""
    # This blocks, so callers run it in a thread executor.
    try:
        return os.read(fd, 65536)
    # A closed/broken PTY reads as end of file.
    except OSError:
        return b""


async def shell_pty_to_hub(agent, session_id: str, master_fd: int) -> None:
    """Relay PTY→browser bytes (blocking os.read in an executor so the loop
    stays responsive). EOF / read error → tear the session down."""
    # Event loop used to run the blocking read in a thread.
    loop = asyncio.get_event_loop()
    # Any error ends the relay; teardown happens in finally.
    try:
        # Keep reading PTY output until EOF.
        while True:
            data = await loop.run_in_executor(None, blocking_read, master_fd)
            # Empty read means the shell closed the terminal.
            if not data:
                break
            # Send the output up as base64 inside a SHELL_OUT event.
            await send_shell_event(
                agent, "SHELL_OUT", {"session_id": session_id,
                                     "data": base64.b64encode(data).decode()})
    # Relay error: stop.
    except Exception:
        pass
    # Always tear down and notify the hub.
    finally:
        await shell_teardown(agent, session_id, send_disconnect=True)


async def shell_wait(agent, session_id: str, proc) -> None:
    """When bash exits (user typed `exit`), tear the session down."""
    # Wait for the bash process to exit.
    try:
        await proc.wait()
    # Errors while waiting are ignored; teardown still runs.
    except Exception:
        pass
    # Clean up and tell the hub the shell is gone.
    finally:
        await shell_teardown(agent, session_id, send_disconnect=True)


def shell_write(agent, session_id: str, data: bytes) -> None:
    """Write browser keystrokes (SHELL_IN) into the session's PTY."""
    # Look up the session's PTY master end, if the session still exists.
    sess = agent._shell_sessions.get(session_id)
    fd = sess.get("master_fd") if sess else None
    # Write the bytes; a closed PTY is ignored (the session is going away).
    if fd is not None:
        try:
            os.write(fd, data)
        except OSError:
            pass


def shell_resize(agent, session_id: str, rows: int, cols: int) -> None:
    """Resize the session's PTY to the browser terminal's rows and columns."""
    # fcntl/termios/struct issue the TIOCSWINSZ window-size ioctl; imported only when needed.
    import fcntl as _fcntl, termios as _termios, struct as _struct
    # Look up the session's PTY master end, if the session still exists.
    sess = agent._shell_sessions.get(session_id)
    fd = sess.get("master_fd") if sess else None
    # Set the new window size (0 values fall back to 24x80); errors are ignored.
    if fd is not None:
        try:
            _fcntl.ioctl(fd, _termios.TIOCSWINSZ,
                         _struct.pack("HHHH", int(rows) or 24, int(cols) or 80, 0, 0))
        except OSError:
            pass


async def shell_teardown(agent, session_id: str, send_disconnect: bool) -> None:
    """Kill the bash proc, cancel the relay tasks, drop the session."""
    # Remove the session; if it is already gone, another path has torn it down.
    sess = agent._shell_sessions.pop(session_id, None)
    if not sess:
        return
    # Cancel any relay task that is still running.
    for task in sess.get("tasks", []):
        if not task.done():
            task.cancel()
    # Kill bash if it is still running; it may have exited on its own already.
    proc = sess.get("proc")
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    # Close our end of the PTY, ignoring an already-closed descriptor.
    fd = sess.get("master_fd")
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    # Only notify the hub when it did not start the close itself.
    if send_disconnect:
        await send_shell_event(agent, "SHELL_DISCONNECT", {"session_id": session_id})
