project('pi-panel-core-compositor', 'c',
  version: '2.0.0',
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
libm       = cc.find_library('m', required: true)

add_project_arguments(
  '-DWLR_USE_UNSTABLE',
  '-D_GNU_SOURCE',   # accept4() and friends
  '-DPI_PANEL_VERSION="@0@"'.format(meson.project_version()),
  '-DIPC_DEFAULT_PATH="@0@"'.format(get_option('default_ipc_socket')),
  '-DCLOCK_DEFAULT_FONT="@0@"'.format(get_option('clock_font')),
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

# ---------------------------------------------------------------------------
# The Varlink interface description, compiled in so that what
# `varlinkctl introspect` shows is always exactly what this build implements.
# ---------------------------------------------------------------------------
python = find_program('python3')
interface_h = custom_target('io.pipanel.Compositor.varlink.h',
  input:   'src/io.pipanel.Compositor.varlink',
  output:  'io.pipanel.Compositor.varlink.h',
  command: [python, files('tools/embed.py'), '@INPUT@', '@OUTPUT@',
            'ipc_interface_description'],
)

# cJSON (MIT, v1.7.18) is vendored: the Pi has no package for it installed,
# and adding one needs root.  Built as its own library so our warning level
# does not apply to third-party code.
cjson = static_library('cjson', 'src/vendor/cJSON.c',
  c_args: ['-w'],
)

# stb_truetype (public domain / MIT, v1.26) is vendored the same way: it
# rasterizes the clock overlay's text from a TTF, and the Pi has no
# freetype/cairo development packages.
stb_truetype = static_library('stb_truetype', 'src/vendor/stb_truetype.c',
  c_args: ['-w'],
)

src = files(
  'src/main.c',
  'src/server.c',
  'src/slot.c',
  'src/view.c',
  'src/input.c',
  'src/ipc.c',
  'src/notify.c',
  'src/transition.c',
  'src/clock.c',
  'src/clock_render.c',
)

executable('pi-panel-compositor',
  sources: [src, xdg_shell_h, interface_h],
  dependencies: [wlroots, wayland_sv, xkbcommon, pixman, libm],
  link_with: [cjson, stb_truetype],
  include_directories: include_directories('include', 'src/vendor'),
  install: true,
  install_dir: get_option('bindir'),
)

# Renders the clock text to an image: tools/clock_preview.c says how.
executable('clock-preview',
  sources: ['tools/clock_preview.c', 'src/clock_render.c'],
  dependencies: [libm],
  link_with: stb_truetype,
  include_directories: include_directories('include', 'src/vendor'),
  install: false,
)
