#include <stdlib.h>

#include <wlr/types/wlr_scene.h>
#include <wlr/util/log.h>

#include "server.h"
#include "ipc.h"
#include "view.h"
#include "transition.h"

/* ---------------------------------------------------------------------------
 * Timer callback — drives the fade animation
 * ---------------------------------------------------------------------------*/

static int transition_tick(void *data) {
    struct transition_state *ts     = data;
    struct server           *server = ts->server;

    if (!ts->active) {
        return 0;
    }

    if (ts->phase == TRANSITION_FADE_OUT) {
        ts->step++;
        float alpha = (float)ts->step / (float)ts->total_steps;
        float color[4] = {0.0f, 0.0f, 0.0f, alpha};
        wlr_scene_rect_set_color(server->fade_rect, color);

        if (ts->step >= ts->total_steps) {
            /* Black is now fully opaque — switch the active view */
            struct view *target = ts->target;
            if (target && target->mapped) {
                view_activate(target);
            }
            ts->target = NULL;
            ts->phase  = TRANSITION_FADE_IN;
        }
        wl_event_source_timer_update(ts->timer, TRANSITION_FRAME_MS);

    } else if (ts->phase == TRANSITION_FADE_IN) {
        ts->step--;
        float alpha = (float)ts->step / (float)ts->total_steps;
        float color[4] = {0.0f, 0.0f, 0.0f, alpha};
        wlr_scene_rect_set_color(server->fade_rect, color);

        if (ts->step <= 0) {
            /* Fully transparent — transition complete */
            wlr_scene_node_set_enabled(&server->fade_rect->node, false);
            ts->active = false;
            ts->phase  = TRANSITION_IDLE;
            ipc_event(server, "transition_finished");

            /* If a switch was queued while we were running, start it now */
            if (ts->pending) {
                struct view *next = ts->pending;
                ts->pending = NULL;
                transition_begin(ts, next);
            }
            return 0; /* Don't reschedule */
        }
        wl_event_source_timer_update(ts->timer, TRANSITION_FRAME_MS);
    }

    return 0;
}

/* ---------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------------*/

void transition_init(struct transition_state *ts, struct server *server) {
    ts->server      = server;
    ts->phase       = TRANSITION_IDLE;
    ts->active      = false;
    ts->target      = NULL;
    ts->pending     = NULL;
    ts->total_steps = TRANSITION_STEPS;
    ts->step        = 0;

    ts->timer = wl_event_loop_add_timer(server->event_loop,
                                         transition_tick, ts);
    if (!ts->timer) {
        wlr_log(WLR_ERROR, "Failed to create transition timer");
    }
}

void transition_finish(struct transition_state *ts) {
    if (ts->timer) {
        wl_event_source_remove(ts->timer);
        ts->timer = NULL;
    }
    ts->active  = false;
    ts->phase   = TRANSITION_IDLE;
    ts->target  = NULL;
    ts->pending = NULL;
}

void transition_cut(struct transition_state *ts, struct view *target) {
    struct server *server = ts->server;

    if (ts->active) {
        /* Abandon the fade in progress: the cut wins, and nothing queued
         * behind it should fire afterwards. */
        wl_event_source_timer_update(ts->timer, 0);
        if (server->fade_rect) {
            wlr_scene_node_set_enabled(&server->fade_rect->node, false);
        }
        ts->active  = false;
        ts->phase   = TRANSITION_IDLE;
        ts->target  = NULL;
        ts->pending = NULL;
        ipc_event(server, "transition_finished");
    }
    view_activate(target);
}

void transition_begin(struct transition_state *ts, struct view *target) {
    struct server *server = ts->server;

    /* No-op if already showing this view */
    if (!ts->active && target == server->active_view) {
        return;
    }

    if (ts->active) {
        /* Queue the request — it fires when the current transition ends */
        ts->pending = target;
        wlr_log(WLR_DEBUG, "Transition queued for view id=%d", target->id);
        return;
    }

    if (!server->fade_rect) {
        /* Output not yet configured — just switch directly */
        view_activate(target);
        return;
    }

    ts->active      = true;
    ts->phase       = TRANSITION_FADE_OUT;
    ts->step        = 0;
    ts->total_steps = TRANSITION_STEPS;
    ts->target      = target;
    ts->pending     = NULL;

    /* Make the overlay visible and fully transparent to start */
    float color[4] = {0.0f, 0.0f, 0.0f, 0.0f};
    wlr_scene_rect_set_color(server->fade_rect, color);
    wlr_scene_node_set_enabled(&server->fade_rect->node, true);
    /* Keep fade_rect above the apps and below the overlays */
    wlr_scene_node_place_below(&server->fade_rect->node,
                               &server->overlay_layer->node);

    /* Kick off the timer */
    wl_event_source_timer_update(ts->timer, TRANSITION_FRAME_MS);
    ipc_event(server, "transition_started");

    char buf[32];
    wlr_log(WLR_DEBUG, "Transition started → view id=%d name='%s'",
        target->id, view_name(target, buf, sizeof(buf)));
}
