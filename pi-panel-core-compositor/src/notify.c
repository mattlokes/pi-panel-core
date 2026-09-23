#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "notify.h"

bool notify_systemd(const char *state) {
    const char *path = getenv("NOTIFY_SOCKET");
    if (!path || !*path) {
        return false;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    size_t len = strlen(path);
    if (len >= sizeof(addr.sun_path)) {
        return false;
    }
    memcpy(addr.sun_path, path, len);
    /* A leading '@' selects the abstract namespace. */
    if (addr.sun_path[0] == '@') {
        addr.sun_path[0] = '\0';
    }
    socklen_t addr_len = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + len);

    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        return false;
    }
    ssize_t n = sendto(fd, state, strlen(state), 0,
                       (struct sockaddr *)&addr, addr_len);
    close(fd);
    return n == (ssize_t)strlen(state);
}
