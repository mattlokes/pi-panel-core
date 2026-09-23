#ifndef IPC_H
#define IPC_H

#include <stdbool.h>
#include <stddef.h>
#include <wayland-server-core.h>

/* Varlink service io.pipanel.Compositor, over AF_UNIX SOCK_STREAM.
 *
 * Wire format: JSON objects, each terminated by one NUL byte.  The interface
 * itself lives in src/io.pipanel.Compositor.varlink and is compiled in, so
 * `varlinkctl introspect` always shows exactly what this build implements. */

#define IPC_MAX_CLIENTS  16
#define IPC_IN_MAX       (64 * 1024)   /* one message; larger drops the client */
#define IPC_OUT_MAX      (256 * 1024)  /* queued replies; a subscriber this far
                                          behind is dropped, never waited on */

#ifndef IPC_DEFAULT_PATH
#define IPC_DEFAULT_PATH "/run/pi-panel/compositor/io.pipanel.Compositor"
#endif

struct server;
struct slot;
struct view;

struct ipc_client {
    struct server          *server;
    int                     fd;
    struct wl_event_source *source;
    bool                    subscribed;  /* inside a Subscribe call */
    bool                    doomed;      /* closed from an idle callback, never
                                            from under a handler */
    bool                    more;        /* flags of the call being handled */
    bool                    oneway;

    char                   *in;
    size_t                  in_len, in_cap;
    char                   *out;
    size_t                  out_len, out_cap;
};

struct ipc_server {
    struct server          *server;
    int                     sock_fd;
    struct wl_event_source *source;      /* accept loop */
    char                   *socket_path;
    struct ipc_client      *clients[IPC_MAX_CLIENTS];
    bool                    reap_scheduled;
};

bool ipc_init(struct ipc_server *ipc, struct server *server, const char *path);
void ipc_finish(struct ipc_server *ipc);

/* Events for subscribers.  |kind| is one of the Event.kind enum values in the
 * interface.  The payload is built immediately, so call these *before*
 * changing state that would alter what they describe (e.g. before a window
 * leaves its anon slot). */
void ipc_event(struct server *server, const char *kind);
void ipc_event_slot(struct server *server, const char *kind, struct slot *slot);
void ipc_event_view(struct server *server, const char *kind, struct view *view);

#endif /* IPC_H */
