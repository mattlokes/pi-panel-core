"""
pi_panel_client.py — IPC client for pi-panel-compositor.

Usage as a library:
    from pi_panel_client import PiPanelClient
    c = PiPanelClient()
    c.switch(0)
    c.switch_name("dashboard")
    print(c.list_views())

Usage from the command line:
    python3 pi_panel_client.py list
    python3 pi_panel_client.py status
    python3 pi_panel_client.py switch 0
    python3 pi_panel_client.py switch-name dashboard
    python3 pi_panel_client.py switch-app com.example.app
    python3 pi_panel_client.py launch myapp "python3 /opt/apps/myapp.py"
    python3 pi_panel_client.py close myapp
    python3 pi_panel_client.py restart myapp
    python3 pi_panel_client.py version
"""

import socket
import sys
import os

DEFAULT_SOCKET = os.environ.get("PI_PANEL_SOCK", "/tmp/pi-panel.sock")


class IpcError(Exception):
    pass


class PiPanelClient:
    def __init__(self, socket_path: str = DEFAULT_SOCKET):
        self.socket_path = socket_path

    def _send(self, command: str) -> list[str]:
        """Send one command and return all response lines."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            try:
                s.connect(self.socket_path)
            except FileNotFoundError:
                raise IpcError(
                    f"Compositor IPC socket not found: {self.socket_path}"
                )
            except ConnectionRefusedError:
                raise IpcError(
                    f"Compositor not running (connection refused): {self.socket_path}"
                )

            s.sendall((command + "\n").encode())

            # Read until we see a terminal line
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                lines = data.decode().splitlines()
                if not lines:
                    continue
                last = lines[-1]
                # Terminal conditions:
                #  - Single-line response: starts with OK or ERROR
                #  - Multi-line response: last line is END
                if last.startswith("OK") or last.startswith("ERROR") or last == "END":
                    break

        return data.decode().splitlines()

    def _ok(self, command: str) -> str:
        """Send command, assert OK, return rest of OK line."""
        lines = self._send(command)
        if not lines:
            raise IpcError("Empty response from compositor")
        first = lines[0]
        if first.startswith("ERROR"):
            raise IpcError(first[len("ERROR "):].strip())
        if not first.startswith("OK"):
            raise IpcError(f"Unexpected response: {first}")
        return first[2:].strip()  # rest of "OK ..." line

    # ------------------------------------------------------------------
    # View switching
    # ------------------------------------------------------------------

    def switch(self, view_id: int) -> None:
        """Switch to the view with the given numeric id."""
        self._ok(f"switch {view_id}")

    def switch_name(self, name: str) -> None:
        """Switch to the view with the given name."""
        self._ok(f"switch-name {name}")

    def switch_app(self, app_id: str) -> None:
        """Switch to the view whose Wayland app_id matches."""
        self._ok(f"switch-app {app_id}")

    # ------------------------------------------------------------------
    # View lifecycle
    # ------------------------------------------------------------------

    def launch(self, name: str, command: str) -> int:
        """Launch a new view.  Returns the assigned view id."""
        result = self._ok(f"launch {name} {command}")
        # result is like "id=3"
        for part in result.split():
            if part.startswith("id="):
                return int(part[3:])
        return -1

    def close(self, name_or_id) -> None:
        """Close a view (SIGTERM the process or send xdg_close)."""
        self._ok(f"close {name_or_id}")

    def restart(self, name_or_id) -> int:
        """Restart the process for a managed view.  Returns new pid."""
        result = self._ok(f"restart {name_or_id}")
        for part in result.split():
            if part.startswith("pid="):
                return int(part[4:])
        return -1

    # ------------------------------------------------------------------
    # Info queries
    # ------------------------------------------------------------------

    def list_views(self) -> list[dict]:
        """Return a list of view dicts (id, name, app_id, title, active, mapped, pid)."""
        lines = self._send("list")
        if not lines or lines[0].startswith("ERROR"):
            raise IpcError(lines[0] if lines else "empty response")

        views = []
        for line in lines[1:]:  # skip "DATA N" header
            if line == "END":
                break
            view = {}
            for part in line.split():
                if "=" in part:
                    k, _, v = part.partition("=")
                    view[k] = v
            if view:
                # Convert numeric / bool fields
                view["id"]     = int(view.get("id", -1))
                view["pid"]    = int(view.get("pid", 0))
                view["active"] = view.get("active") == "true"
                view["mapped"] = view.get("mapped") == "true"
                views.append(view)
        return views

    def status(self) -> dict:
        """Return compositor status dict."""
        result = self._ok("status")
        info = {}
        for part in result.split():
            if "=" in part:
                k, _, v = part.partition("=")
                info[k] = v
        if "active_id" in info:
            info["active_id"] = int(info["active_id"])
        if "view_count" in info:
            info["view_count"] = int(info["view_count"])
        if "transitioning" in info:
            info["transitioning"] = info["transitioning"] == "true"
        return info

    def version(self) -> str:
        """Return the compositor version string."""
        return self._ok("version")


# ----------------------------------------------------------------------
# Command-line interface
# ----------------------------------------------------------------------

def _usage():
    print(__doc__)
    sys.exit(1)


def main():
    args = sys.argv[1:]
    if not args:
        _usage()

    sock = DEFAULT_SOCKET
    # Allow --socket <path> anywhere before the command
    if len(args) >= 2 and args[0] == "--socket":
        sock = args[1]
        args = args[2:]

    if not args:
        _usage()

    c = PiPanelClient(sock)
    cmd = args[0]

    try:
        if cmd == "list":
            views = c.list_views()
            if not views:
                print("(no views)")
            for v in views:
                marker = "*" if v["active"] else " "
                print(f"  {marker} id={v['id']:3d}  name={v['name']:<20s}  "
                      f"app_id={v.get('app_id','(none)'):<30s}  "
                      f"mapped={v['mapped']}  pid={v['pid']}")

        elif cmd == "status":
            s = c.status()
            print(f"active_id={s.get('active_id')}  "
                  f"active_name={s.get('active_name')}  "
                  f"view_count={s.get('view_count')}  "
                  f"transitioning={s.get('transitioning')}")

        elif cmd == "switch":
            if len(args) < 2:
                print("Usage: switch <id>"); sys.exit(1)
            c.switch(int(args[1]))
            print("OK")

        elif cmd == "switch-name":
            if len(args) < 2:
                print("Usage: switch-name <name>"); sys.exit(1)
            c.switch_name(args[1])
            print("OK")

        elif cmd == "switch-app":
            if len(args) < 2:
                print("Usage: switch-app <app_id>"); sys.exit(1)
            c.switch_app(args[1])
            print("OK")

        elif cmd == "launch":
            if len(args) < 3:
                print("Usage: launch <name> <command>"); sys.exit(1)
            command = " ".join(args[2:])
            vid = c.launch(args[1], command)
            print(f"OK id={vid}")

        elif cmd == "close":
            if len(args) < 2:
                print("Usage: close <name|id>"); sys.exit(1)
            c.close(args[1])
            print("OK")

        elif cmd == "restart":
            if len(args) < 2:
                print("Usage: restart <name|id>"); sys.exit(1)
            pid = c.restart(args[1])
            print(f"OK pid={pid}")

        elif cmd == "version":
            print(c.version())

        else:
            print(f"Unknown command: {cmd}")
            _usage()

    except IpcError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
