#ifndef IPC_H
#define IPC_H

#include <stdbool.h>
#include <stddef.h>
#include <wayland-server-core.h>

#define IPC_MAX_CLIENTS  8
#define IPC_BUF_SIZE     4096
#define IPC_DEFAULT_PATH "/tmp/pi-panel.sock"

struct server; /* forward declaration */

struct ipc_client {
    int                     fd;
    struct wl_event_source *readable;
    char                    buf[IPC_BUF_SIZE];
    size_t                  buf_len;
    struct server          *server;
};

struct ipc_server {
    struct server          *server;
    int                     sock_fd;
    struct wl_event_source *readable;   /* accept loop */
    char                   *socket_path;
    struct ipc_client      *clients[IPC_MAX_CLIENTS];
};

bool ipc_init(struct ipc_server *ipc, struct server *server, const char *path);
void ipc_finish(struct ipc_server *ipc);

/* Write a formatted response line to a client (adds newline) */
void ipc_client_writef(struct ipc_client *client, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));

#endif /* IPC_H */
