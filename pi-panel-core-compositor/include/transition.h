#ifndef TRANSITION_H
#define TRANSITION_H

#include <stdbool.h>
#include <wayland-server-core.h>

#define TRANSITION_FRAME_MS  16    /* ~60 fps tick interval */
#define TRANSITION_STEPS     30    /* frames per phase => ~500 ms per half */

struct server; /* forward declarations */
struct view;

typedef enum {
    TRANSITION_IDLE,
    TRANSITION_FADE_OUT,  /* alpha 0 → 1, black overlay appears */
    TRANSITION_FADE_IN,   /* alpha 1 → 0, black overlay disappears */
} transition_phase;

struct transition_state {
    struct server          *server;
    struct wl_event_source *timer;
    transition_phase        phase;
    int                     step;
    int                     total_steps;
    struct view            *target;   /* view to switch to after fade-out */
    struct view            *pending;  /* queued switch that arrived mid-transition */
    bool                    active;
};

void transition_init(struct transition_state *ts, struct server *server);
void transition_finish(struct transition_state *ts);

/* Begin a fade-to-black → switch → fade-in sequence.
 * If a transition is already running the request is queued; the queued
 * switch fires as soon as the current transition completes. */
void transition_begin(struct transition_state *ts, struct view *target);

/* Switch immediately, abandoning any fade in progress (and anything queued). */
void transition_cut(struct transition_state *ts, struct view *target);

#endif /* TRANSITION_H */
