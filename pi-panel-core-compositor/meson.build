project('pi-panel-compositor', 'c',
  version: '1.0.0',
  license: 'MIT',
  default_options: [
    'c_std=gnu11',
    'warning_level=2',
    'werror=false',
  ],
)

cc = meson.get_compiler('c')

# wlroots 0.19 is the version shipped by the Raspberry Pi OS (trixie) archive
# and already installed on the target Pi, so it is what we build against.
wlroots    = dependency('wlroots-0.19', required: true)
wayland_sv = dependency('wayland-server', required: true)
xkbcommon  = dependency('xkbcommon', required: true)
pixman     = dependency('pixman-1', required: true)

add_project_arguments(
  '-DWLR_USE_UNSTABLE',
  '-D_GNU_SOURCE',   # accept4() and friends
  language: 'c',
)

# ---------------------------------------------------------------------------
# Generated Wayland protocol headers
#
# wlr/types/wlr_xdg_shell.h includes "xdg-shell-protocol.h", which is not
# shipped by any package — it must be generated from the protocol XML with
# wayland-scanner at build time.
# ---------------------------------------------------------------------------
wl_protos     = dependency('wayland-protocols', version: '>=1.14')
wl_proto_dir  = wl_protos.get_variable('pkgdatadir')

wl_scanner_dep = dependency('wayland-scanner', native: true)
wayland_scanner = find_program(
  wl_scanner_dep.get_variable('wayland_scanner'),
  native: true,
)

xdg_shell_h = custom_target('xdg-shell-protocol.h',
  input:   wl_proto_dir / 'stable' / 'xdg-shell' / 'xdg-shell.xml',
  output:  'xdg-shell-protocol.h',
  command: [wayland_scanner, 'server-header', '@INPUT@', '@OUTPUT@'],
)

src = files(
  'src/main.c',
  'src/server.c',
  'src/view.c',
  'src/input.c',
  'src/ipc.c',
  'src/transition.c',
)

executable('pi-panel-compositor',
  sources: [src, xdg_shell_h],
  dependencies: [wlroots, wayland_sv, xkbcommon, pixman],
  include_directories: include_directories('include'),
  install: true,
  install_dir: get_option('bindir'),
)
