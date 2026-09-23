#ifndef SLOT_H
#define SLOT_H

#include <stdbool.h>
#include <wayland-server-core.h>

struct server;
struct view;

/* A named place for a view, registered over IPC by the controller.
 *
 * Slots and windows are separate on purpose: a slot exists before its app has
 * started and survives the app restarting, while a window (struct view) comes
 * and goes with the client.  Attaching a window to a slot is just a pointer,
 * so "adopting" a window that mapped before its slot was registered is free. */
struct slot {
    struct server  *server;
    struct wl_list  link;          /* server->slots, in registration order */

    int    id;                     /* shares one counter with views, never reused */
    char  *name;
    char  *match_unit;             /* systemd unit whose windows belong here */
    char  *match_app_id;           /* fallback: Wayland app_id */

    struct view *view;             /* attached window, or NULL */
};

/* [a-z0-9][a-z0-9_-]*, at most 63 chars, and not the reserved "anon-" prefix. */
bool slot_name_valid(const char *name);

/* Create a slot, or update the matchers of the existing one.
 * A NULL match_unit means the default, "pi-panel-app@<name>.service".
 * Sets *created accordingly. Returns NULL only on allocation failure. */
struct slot *slot_register(struct server *server, const char *name,
                           const char *match_unit, const char *match_app_id,
                           bool *created);

/* Forget a slot; its window, if any, becomes unregistered. */
void slot_unregister(struct slot *slot);

struct slot *slot_for_name(struct server *server, const char *name);
struct slot *slot_for_id(struct server *server, int id);

/* The first free slot claiming a window from |unit| with |app_id|: a unit match
 * beats an app_id match.  Either argument may be NULL. */
struct slot *slot_find_free_match(struct server *server,
                                  const char *unit, const char *app_id);

/* Attach every unregistered window that matches |slot|, if it has none yet. */
void slot_adopt_unregistered(struct slot *slot);

void slot_free_all(struct server *server);

#endif /* SLOT_H */
