#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <errno.h>

#include <wlr/util/log.h>

#include "server.h"
#include "view.h"

/* ---------------------------------------------------------------------------
 * Config file loader
 *
 * Format (one view per non-comment line):
 *   <name>  <yes|no>  <command...>
 *
 * Example:
 *   dashboard  yes  python3 /home/pi/apps/dashboard.py
 *   browser    no   chromium-browser --kiosk http://localhost
 * ---------------------------------------------------------------------------*/
static void load_config(struct server *server, const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) {
        wlr_log(WLR_INFO,
            "Config file not found: %s  (no pre-configured views; use IPC to launch)",
            path);
        return;
    }

    char line[1024];
    int  lineno = 0;

    while (fgets(line, sizeof(line), f)) {
        lineno++;

        /* Strip trailing newline / CR */
        size_t len = strlen(line);
        while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r')) {
            line[--len] = '\0';
        }

        /* Skip blank lines and comments */
        if (len == 0 || line[0] == '#') {
            continue;
        }

        char name[128], ar_str[8], command[768];
        if (sscanf(line, "%127s %7s %767[^\n]", name, ar_str, command) != 3) {
            wlr_log(WLR_ERROR,
                "%s:%d: invalid format — expected: name yes|no command",
                path, lineno);
            continue;
        }

        bool auto_restart = (strcmp(ar_str, "yes") == 0 ||
                             strcmp(ar_str, "1")   == 0 ||
                             strcmp(ar_str, "true") == 0);

        struct view *view =
            view_create_managed(server, name, command, auto_restart);
        if (!view) {
            wlr_log(WLR_ERROR, "%s:%d: failed to create view slot '%s'",
                path, lineno, name);
            continue;
        }

        wlr_log(WLR_INFO, "Launching view '%s': %s", name, command);
        if (!view_launch(view)) {
            wlr_log(WLR_ERROR, "%s:%d: failed to launch '%s'", path, lineno, name);
        }
    }

    fclose(f);
}

/* ---------------------------------------------------------------------------
 * journald logger
 *
 * wlroots writes every level to stderr, so under systemd-cat the whole log
 * would otherwise be filed as "info" and `journalctl -p err` would find
 * nothing.  Prefixing each line with a syslog level in <N> form lets journald
 * record real priorities (systemd-cat --level-prefix=true).
 * ---------------------------------------------------------------------------*/
static void log_journald(enum wlr_log_importance importance,
                          const char *fmt, va_list args) {
    int priority;
    switch (importance) {
    case WLR_ERROR: priority = 3; break;  /* LOG_ERR */
    case WLR_INFO:  priority = 6; break;  /* LOG_INFO */
    default:        priority = 7; break;  /* LOG_DEBUG */
    }
    fprintf(stderr, "<%d>", priority);
    vfprintf(stderr, fmt, args);
    fputc('\n', stderr);
}

static void print_usage(const char *prog) {
    fprintf(stderr,
        "Usage: %s [OPTIONS]\n"
        "\n"
        "Options:\n"
        "  --config FILE         View config file\n"
        "                        (default: /etc/pi-panel/compositor.conf)\n"
        "  --ipc-socket PATH     IPC Unix socket path\n"
        "                        (default: " IPC_DEFAULT_PATH ")\n"
        "  --wayland-socket NAME Wayland display socket name (default: auto)\n"
        "  --debug               Enable verbose wlroots logging\n"
        "  --help                Show this message\n"
        "\n"
        "Environment variables:\n"
        "  WLR_BACKENDS          Force backend: x11, wayland, drm, headless\n"
        "  WLR_DRM_DEVICES       Colon-separated DRM node paths\n"
        "  WLR_RENDERER          Force renderer: gles2, vulkan, pixman\n"
        "  WLR_NO_HARDWARE_CURSORS=1  Disable hardware cursor plane\n",
        prog);
}

int main(int argc, char *argv[]) {
    const char *config_file    = "/etc/pi-panel/compositor.conf";
    const char *ipc_socket     = IPC_DEFAULT_PATH;
    const char *wayland_socket = NULL;
    bool        debug          = false;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--debug") == 0) {
            debug = true;
        } else if (strcmp(argv[i], "--help") == 0) {
            print_usage(argv[0]);
            return 0;
        } else if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) {
            config_file = argv[++i];
        } else if (strcmp(argv[i], "--ipc-socket") == 0 && i + 1 < argc) {
            ipc_socket = argv[++i];
        } else if (strcmp(argv[i], "--wayland-socket") == 0 && i + 1 < argc) {
            wayland_socket = argv[++i];
        } else {
            fprintf(stderr, "Unknown option: %s\n\n", argv[i]);
            print_usage(argv[0]);
            return 1;
        }
    }

    /* systemd sets JOURNAL_STREAM when stderr is a journal stream (systemd-cat
     * does too).  In a plain terminal, stick with the default wlroots logger —
     * it is friendlier to read than bare <N> prefixes. */
    wlr_log_init(debug ? WLR_DEBUG : WLR_INFO,
                 getenv("JOURNAL_STREAM") ? log_journald : NULL);

    struct server_config cfg = {
        .wayland_socket = wayland_socket,
        .ipc_socket     = ipc_socket,
        .log_debug      = debug,
    };

    struct server server;
    memset(&server, 0, sizeof(server));

    if (!server_init(&server, &cfg)) {
        wlr_log(WLR_ERROR, "Failed to initialise compositor");
        return 1;
    }

    wlr_log(WLR_INFO, "Compositor running — Wayland display: %s",
        server.wayland_socket);
    wlr_log(WLR_INFO, "IPC socket: %s", server.ipc.socket_path);

    /* Launch pre-configured views */
    load_config(&server, config_file);

    /* Enter the Wayland event loop — blocks until wl_display_terminate() */
    wl_display_run(server.display);

    server_finish(&server);
    wlr_log(WLR_INFO, "Compositor exited cleanly");
    return 0;
}
