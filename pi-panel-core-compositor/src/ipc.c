#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <fcntl.h>

#include <wlr/util/log.h>

#include "server.h"
#include "view.h"
#include "ipc.h"

/* ---------------------------------------------------------------------------
 * Write helpers
 * ---------------------------------------------------------------------------*/

void ipc_client_writef(struct ipc_client *client, const char *fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);

    if (n <= 0) return;

    /* vsnprintf() returns the length it *would* have written, which for an
     * over-long line exceeds the buffer.  Passing that straight to write()
     * reads past the end of buf — reachable through cmd_list(), whose rows
     * embed client-supplied title/app_id strings of unbounded length.  Clamp
     * to what the buffer actually holds. */
    size_t len = (size_t)n < sizeof(buf) ? (size_t)n : sizeof(buf) - 1;

    /* The client fd is non-blocking, so a short write is possible against a
     * slow reader.  Retry rather than silently dropping the tail. */
    size_t off = 0;
    while (off < len) {
        ssize_t written = write(client->fd, buf + off, len - off);
        if (written > 0) {
            off += (size_t)written;
            continue;
        }
        if (written < 0 && errno == EINTR) {
            continue;
        }
        wlr_log(WLR_DEBUG, "IPC write error after %zu/%zu bytes: %s",
            off, len, strerror(errno));
        break;
    }
}

/* ---------------------------------------------------------------------------
 * Command dispatcher
 * ---------------------------------------------------------------------------*/

static void cmd_list(struct ipc_client *client) {
    struct server *server = client->server;
    int count = server->view_count;

    ipc_client_writef(client, "DATA %d\n", count);

    struct view *v;
    wl_list_for_each(v, &server->views, link) {
        /* name/app_id/title come from the Wayland client and have no length
         * limit, so bound them here: a row must stay well inside the write
         * buffer, and truncating a field is far better than truncating the
         * row and losing its trailing key=value pairs. */
        ipc_client_writef(client,
            "id=%d name=%.128s app_id=%.128s title=%.256s "
            "active=%s mapped=%s pid=%d\n",
            v->id,
            v->name    ? v->name    : "(none)",
            v->app_id  ? v->app_id  : "(none)",
            v->title   ? v->title   : "(none)",
            v->active  ? "true"     : "false",
            v->mapped  ? "true"     : "false",
            (int)v->pid);
    }
    ipc_client_writef(client, "END\n");
}

static void cmd_status(struct ipc_client *client) {
    struct server     *server = client->server;
    struct view       *av     = server->active_view;
    struct wlr_output *out    = server->primary_output;

    /* refresh is in mHz; report Hz to two decimals, 0 if unknown (the
     * headless and nested backends do not always report one). */
    int refresh_mhz = out ? out->refresh : 0;

    ipc_client_writef(client,
        "OK active_id=%d active_name=%s view_count=%d transitioning=%s "
        "output=%s output_width=%d output_height=%d refresh=%d.%03d\n",
        av ? av->id   : -1,
        av && av->name ? av->name : "(none)",
        server->view_count,
        server->transition.active ? "true" : "false",
        out && out->name ? out->name : "(none)",
        server->output_width,
        server->output_height,
        refresh_mhz / 1000, refresh_mhz % 1000);
}

static void cmd_switch(struct ipc_client *client, const char *arg) {
    struct server *server = client->server;
    char *end;
    long id = strtol(arg, &end, 10);
    if (*end != '\0' || end == arg) {
        ipc_client_writef(client, "ERROR invalid id '%s'\n", arg);
        return;
    }
    struct view *view = view_for_id(server, (int)id);
    if (!view) {
        ipc_client_writef(client, "ERROR no view with id %ld\n", id);
        return;
    }
    if (!view->mapped) {
        ipc_client_writef(client, "ERROR view %ld is not mapped\n", id);
        return;
    }
    transition_begin(&server->transition, view);
    ipc_client_writef(client, "OK\n");
}

static void cmd_switch_name(struct ipc_client *client, const char *arg) {
    struct server *server = client->server;
    struct view *view = view_for_name(server, arg);
    if (!view) {
        ipc_client_writef(client, "ERROR no view with name '%s'\n", arg);
        return;
    }
    if (!view->mapped) {
        ipc_client_writef(client, "ERROR view '%s' is not mapped\n", arg);
        return;
    }
    transition_begin(&server->transition, view);
    ipc_client_writef(client, "OK\n");
}

static void cmd_switch_app(struct ipc_client *client, const char *arg) {
    struct server *server = client->server;
    struct view *view = view_for_app_id(server, arg);
    if (!view) {
        ipc_client_writef(client, "ERROR no view with app_id '%s'\n", arg);
        return;
    }
    if (!view->mapped) {
        ipc_client_writef(client, "ERROR view '%s' is not mapped\n", arg);
        return;
    }
    transition_begin(&server->transition, view);
    ipc_client_writef(client, "OK\n");
}

static void cmd_launch(struct ipc_client *client, const char *arg) {
    /* Syntax: launch <name> <command...> */
    struct server *server = client->server;
    char name[128], command[768];

    if (sscanf(arg, "%127s %767[^\n]", name, command) != 2) {
        ipc_client_writef(client,
            "ERROR usage: launch <name> <command>\n");
        return;
    }

    /* Reject duplicate names */
    if (view_for_name(server, name)) {
        ipc_client_writef(client,
            "ERROR view with name '%s' already exists\n", name);
        return;
    }

    struct view *view = view_create_managed(server, name, command, false);
    if (!view) {
        ipc_client_writef(client, "ERROR out of memory\n");
        return;
    }
    if (!view_launch(view)) {
        view_free(view);
        ipc_client_writef(client, "ERROR failed to launch command\n");
        return;
    }
    ipc_client_writef(client, "OK id=%d\n", view->id);
}

static void cmd_close(struct ipc_client *client, const char *arg) {
    struct server *server = client->server;
    struct view   *view   = NULL;

    /* Accept either numeric id or name */
    char *end;
    long id = strtol(arg, &end, 10);
    if (*end == '\0' && end != arg) {
        view = view_for_id(server, (int)id);
    } else {
        view = view_for_name(server, arg);
    }

    if (!view) {
        ipc_client_writef(client, "ERROR no view '%s'\n", arg);
        return;
    }
    if (view->pid > 0) {
        view_terminate(view);
        ipc_client_writef(client, "OK\n");
    } else if (view->xdg_toplevel) {
        /* App connected externally; ask it to close */
        wlr_xdg_toplevel_send_close(view->xdg_toplevel);
        ipc_client_writef(client, "OK\n");
    } else {
        ipc_client_writef(client, "ERROR view '%s' has no running process\n", arg);
    }
}

static void cmd_restart(struct ipc_client *client, const char *arg) {
    struct server *server = client->server;
    struct view   *view   = NULL;

    char *end;
    long id = strtol(arg, &end, 10);
    if (*end == '\0' && end != arg) {
        view = view_for_id(server, (int)id);
    } else {
        view = view_for_name(server, arg);
    }

    if (!view) {
        ipc_client_writef(client, "ERROR no view '%s'\n", arg);
        return;
    }
    if (!view->command) {
        ipc_client_writef(client,
            "ERROR view '%s' was not launched by the compositor\n", arg);
        return;
    }

    /* Kill the old process group if still running */
    if (view->pid > 0) {
        view_terminate(view);
        view->pid = 0;
    }
    if (!view_launch(view)) {
        ipc_client_writef(client, "ERROR failed to re-launch\n");
        return;
    }
    ipc_client_writef(client, "OK pid=%d\n", (int)view->pid);
}

static void cmd_quit(struct ipc_client *client) {
    ipc_client_writef(client, "OK\n");
    wlr_log(WLR_INFO, "Shutdown requested over IPC");
    wl_display_terminate(client->server->display);
}

static void ipc_dispatch(struct ipc_client *client, const char *line) {
    wlr_log(WLR_DEBUG, "IPC command: '%s'", line);

    if (strcmp(line, "list") == 0) {
        cmd_list(client);
    } else if (strcmp(line, "status") == 0) {
        cmd_status(client);
    } else if (strcmp(line, "quit") == 0) {
        cmd_quit(client);
    } else if (strcmp(line, "version") == 0) {
        ipc_client_writef(client, "OK pi-panel-compositor/1.0 protocol/1\n");
    } else if (strncmp(line, "switch ", 7) == 0) {
        cmd_switch(client, line + 7);
    } else if (strncmp(line, "switch-name ", 12) == 0) {
        cmd_switch_name(client, line + 12);
    } else if (strncmp(line, "switch-app ", 11) == 0) {
        cmd_switch_app(client, line + 11);
    } else if (strncmp(line, "launch ", 7) == 0) {
        cmd_launch(client, line + 7);
    } else if (strncmp(line, "close ", 6) == 0) {
        cmd_close(client, line + 6);
    } else if (strncmp(line, "restart ", 8) == 0) {
        cmd_restart(client, line + 8);
    } else {
        ipc_client_writef(client, "ERROR unknown command: %s\n", line);
    }
}

/* ---------------------------------------------------------------------------
 * Client I/O
 * ---------------------------------------------------------------------------*/

static void ipc_client_close(struct ipc_client *client) {
    struct ipc_server *ipc = &client->server->ipc;

    wl_event_source_remove(client->readable);
    close(client->fd);

    for (int i = 0; i < IPC_MAX_CLIENTS; i++) {
        if (ipc->clients[i] == client) {
            ipc->clients[i] = NULL;
            break;
        }
    }
    free(client);
}

static int ipc_client_readable(int fd, uint32_t mask, void *data) {
    struct ipc_client *client = data;

    if (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR)) {
        ipc_client_close(client);
        return 0;
    }

    /* Read available data into the buffer */
    ssize_t n = read(fd,
        client->buf + client->buf_len,
        IPC_BUF_SIZE - client->buf_len - 1);

    if (n <= 0) {
        ipc_client_close(client);
        return 0;
    }
    client->buf_len += (size_t)n;
    client->buf[client->buf_len] = '\0';

    /* Process complete lines */
    char *start = client->buf;
    char *nl;
    while ((nl = memchr(start, '\n', client->buf_len - (size_t)(start - client->buf)))) {
        *nl = '\0';
        /* Strip trailing CR */
        size_t len = strlen(start);
        if (len > 0 && start[len-1] == '\r') start[--len] = '\0';

        if (len > 0) {
            ipc_dispatch(client, start);
        }
        start = nl + 1;
    }

    /* Shift unconsumed data to the front */
    size_t remaining = client->buf_len - (size_t)(start - client->buf);
    if (remaining > 0) {
        memmove(client->buf, start, remaining);
    }
    client->buf_len = remaining;

    return 0;
}

/* ---------------------------------------------------------------------------
 * Accept new connections
 * ---------------------------------------------------------------------------*/

static int ipc_accept(int fd, uint32_t mask, void *data) {
    (void)mask;
    struct ipc_server *ipc = data;

    int client_fd = accept4(fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
    if (client_fd < 0) {
        wlr_log(WLR_ERROR, "accept4() failed: %s", strerror(errno));
        return 0;
    }

    /* Find a free slot */
    int slot = -1;
    for (int i = 0; i < IPC_MAX_CLIENTS; i++) {
        if (!ipc->clients[i]) { slot = i; break; }
    }
    if (slot < 0) {
        wlr_log(WLR_ERROR, "IPC: too many clients, rejecting connection");
        close(client_fd);
        return 0;
    }

    struct ipc_client *client = calloc(1, sizeof(struct ipc_client));
    if (!client) {
        close(client_fd);
        return 0;
    }
    client->fd     = client_fd;
    client->server = ipc->server;
    client->readable = wl_event_loop_add_fd(
        ipc->server->event_loop, client_fd,
        WL_EVENT_READABLE, ipc_client_readable, client);

    ipc->clients[slot] = client;
    wlr_log(WLR_DEBUG, "IPC: client connected (slot %d)", slot);
    return 0;
}

/* ---------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------------*/

bool ipc_init(struct ipc_server *ipc, struct server *server, const char *path) {
    ipc->server = server;
    ipc->socket_path = strdup(path);

    /* Remove stale socket */
    unlink(path);

    ipc->sock_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (ipc->sock_fd < 0) {
        wlr_log(WLR_ERROR, "socket() failed: %s", strerror(errno));
        return false;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    if (bind(ipc->sock_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        wlr_log(WLR_ERROR, "bind() failed on '%s': %s", path, strerror(errno));
        close(ipc->sock_fd);
        return false;
    }

    if (listen(ipc->sock_fd, IPC_MAX_CLIENTS) < 0) {
        wlr_log(WLR_ERROR, "listen() failed: %s", strerror(errno));
        close(ipc->sock_fd);
        return false;
    }

    ipc->readable = wl_event_loop_add_fd(
        server->event_loop, ipc->sock_fd,
        WL_EVENT_READABLE, ipc_accept, ipc);

    wlr_log(WLR_INFO, "IPC listening on %s", path);
    return true;
}

void ipc_finish(struct ipc_server *ipc) {
    if (ipc->readable) {
        wl_event_source_remove(ipc->readable);
        ipc->readable = NULL;
    }

    for (int i = 0; i < IPC_MAX_CLIENTS; i++) {
        if (ipc->clients[i]) {
            wl_event_source_remove(ipc->clients[i]->readable);
            close(ipc->clients[i]->fd);
            free(ipc->clients[i]);
            ipc->clients[i] = NULL;
        }
    }

    if (ipc->sock_fd >= 0) {
        close(ipc->sock_fd);
        ipc->sock_fd = -1;
    }
    if (ipc->socket_path) {
        unlink(ipc->socket_path);
        free(ipc->socket_path);
        ipc->socket_path = NULL;
    }
}
