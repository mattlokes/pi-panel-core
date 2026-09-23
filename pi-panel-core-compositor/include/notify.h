#ifndef NOTIFY_H
#define NOTIFY_H

#include <stdbool.h>

/* sd_notify(3) without libsystemd: send |state| (e.g. "READY=1") to the
 * datagram socket in $NOTIFY_SOCKET.  Returns false when not run by systemd
 * as Type=notify, which is not an error. */
bool notify_systemd(const char *state);

#endif /* NOTIFY_H */
