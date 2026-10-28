"""PipeWire Controller — application shell."""

from __future__ import annotations

import signal
import threading
from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import (Adw, Gdk, Gio, GLib, GObject, Graphene,  # noqa: E402
                           Gsk, Gtk)

from .backend import (graph, levels, livestate, prefs, presets, pw, system,
                      units)
from .ui.chains_page import ChainsPage
from .ui.dashboard import Dashboard
from .ui.devices import DevicesPage
from .ui.effects_page import EffectsPage
from .ui.enhance_page import EnhancePage
from .ui.paths_page import PathsPage
from .ui.graph_page import GraphPage
from .ui.hrir_page import HrirPage
from .ui.monitor_page import MonitorPage
from .ui.policy_page import PolicyPage
from .ui.settings_pages import ServerPage, StreamsPage, WirePlumberPage
from .ui.surround_page import SurroundPage
from .ui.tools_page import ToolsPage
from .ui.virtual_page import VirtualPage
from .ui.widgets import async_call, esc, micro, reduce_motion

# (name, title, icon, section).  Sixteen rows in one flat list read as
# "this is complicated" before a single click, so they are grouped — but every
# page stays present and reachable; nothing is hidden behind Advanced.
# Order within the list *is* the sidebar order; `goto()` and the startup
# restore both look pages up by name, so regrouping is safe.
PAGES = [
    ('dashboard', 'Dashboard', 'pwctl-dashboard-symbolic', 'Mix'),
    ('paths', 'Signal Paths', 'pwctl-paths-symbolic', 'Mix'),
    ('enhance', 'Equalizer', 'pwctl-eq-symbolic', 'Mix'),

    ('graph', 'Patchbay', 'pwctl-patchbay-symbolic', 'Route'),
    ('devices', 'Devices', 'pwctl-devices-symbolic', 'Route'),
    ('virtual', 'Virtual Devices', 'pwctl-virtual-symbolic', 'Route'),
    ('surround', 'Surround Setup', 'pwctl-surround-symbolic', 'Route'),

    ('chains', 'Filter Chains', 'pwctl-chains-symbolic', 'Process'),
    ('effects', 'Effects', 'pwctl-effects-symbolic', 'Process'),
    ('hrir', 'HRIR Library', 'pwctl-hrir-symbolic', 'Process'),

    ('server', 'Server', 'pwctl-server-symbolic', 'Configure'),
    ('streams', 'Streams', 'pwctl-streams-symbolic', 'Configure'),
    ('policy', 'App Policies', 'pwctl-policy-symbolic', 'Configure'),
    ('wireplumber', 'Session & Bluetooth', 'pwctl-session-symbolic',
     'Configure'),

    ('monitor', 'Monitor', 'pwctl-monitor-symbolic', 'System'),
    ('tools', 'Tools', 'pwctl-tools-symbolic', 'System'),
]

RESTART_UNITS = {
    'pipewire': ('PipeWire', system.restart_pipewire),
    'pulse': ('PipeWire-Pulse',
              lambda: system.restart_unit('pipewire-pulse.service')),
    'wireplumber': ('WirePlumber', system.restart_wireplumber),
}

# The services whose failure raises a desktop notification.
WATCHED_SERVICES = ('pipewire.service', 'wireplumber.service',
                    'pipewire-pulse.service')

# The narrowest and shortest the window may be: GNOME's guideline for an app
# that scales down to a phone.  A tiling compositor gives the window its tile
# whatever minimum the app asks for, so this is the width every page has to
# work at, not a request.
MIN_WIDTH, MIN_HEIGHT = 360, 294

# Below this window width the sidebar stops sitting beside the page and slides
# over it instead.  In sp, so it follows the text size.  840 is the widest
# page today (the Patchbay, 603) plus the sidebar, so a page never has to fit
# beside the sidebar in less than it needs.  It comes down to 600 (the
# sidebar plus a 360 page) once every page fits in 360.
SIDEBAR_COLLAPSE_SP = 840

# How long after the window appears its sidebar is put in place without a
# slide.  At start-up in a narrow window the breakpoint applies after the
# first frames, and a sidebar sliding away then is noise, not information.
SETTLE_US = 500_000

# The header's change between labels and icons is a crossfade this long (the
# same as the page crossfade), and only when the header was last drawn within
# this many pixels of its new width.  Across a bigger jump, such as a tiling
# layout change, the old header's ends would fade out in the wrong place, and
# the compositor is animating that change itself.
HEADER_FADE_MS = 200
HEADER_FADE_MAX_JUMP = 64


class _HeaderFit(Gtk.Widget):
    """The content header bar, holding only what fits in it.

    It decides as it is laid out, in two steps: narrower than `need`, the
    labelled controls go to icons; in a narrow window (the sidebar
    collapsed), narrower than `floor` (the header with icons only), the
    Overview/Mixer switcher leaves it too, for a bar along the bottom of the
    Dashboard.  Only then is the header allocated, so the first frame at a
    new width is already right.  An `AdwBreakpointBin` was tried first and
    laid the header out one frame late, which painted the very overlap this
    prevents once at every crossing (measured on the real window).  The
    window works out `need`, `floor`, `least` and `height` in `fit_header`.

    The change itself is a crossfade.  The layout still switches at once, so
    nothing ever overlaps; only the picture eases from the header as it was
    last drawn to the header as it is now.  Fading costs nothing to keep
    ready: the last drawing is the same render node the header was drawn
    with, not a second rendering.
    """

    def __init__(self, child, set_compact, set_tight, loose):
        super().__init__()
        self._child = child
        child.set_parent(self)
        self._set_compact = set_compact
        self._set_tight = set_tight
        # () -> bool: the sidebar is collapsed.  Only then may the switcher
        # go.  Beside the sidebar the header is narrow only for a few frames
        # of its slide, and the switcher would pop out and back.
        self._loose = loose
        self.compact = False
        self.tight = False
        self.need = 0        # narrower than this, the labels go
        self.floor = 0       # the header's minimum width with icons only;
        #                      narrower than this, the switcher goes
        self.least = 0       # ...and its minimum once the switcher has gone
        self.height = 0      # the taller of the layouts
        self._last = None    # the header as it was last drawn: its node,
        self._last_width = 0  # its width, and where its title sat
        self._last_title = (0, 0)
        self._ghost = None   # the same three, for the drawing fading out
        self._ghost_width = 0
        self._ghost_title = (0, 0)
        self._progress = 1.0
        # The old drawing can be wider than the header now; keep it inside.
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._fade = Adw.TimedAnimation.new(
            self, 0.0, 1.0, HEADER_FADE_MS,
            Adw.CallbackAnimationTarget.new(self._on_fade))
        self._fade.set_easing(Adw.Easing.EASE)
        self._fade.connect('done', self._on_fade_done)

    def do_measure(self, orientation, for_size):
        low, nat, _b1, _b2 = self._child.measure(orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL:
            # The labels (and, in a narrow window, the switcher) go before
            # the header gets this narrow, so the smallest layout's minimum
            # is the one that counts.  Beside the sidebar that is the icons'
            # minimum, which is also what decides whether the sidebar's
            # slide has room for the page (`Window._squeezed`).
            least = self.least if self._loose() else self.floor
            low = min(low, least) if least else low
        else:
            low, nat = max(low, self.height), max(nat, self.height)
        return low, nat, -1, -1

    def do_size_allocate(self, width, height, baseline):
        compact = width < self.need
        tight = width < self.floor and self._loose()
        if (compact, tight) != (self.compact, self.tight):
            if compact != self.compact:
                self.compact = compact
                self._set_compact(compact)
            if tight != self.tight:
                self.tight = tight
                self._set_tight(tight)
            self._cross_fade(width)
        self._child.allocate(width, height, baseline, None)

    def _cross_fade(self, width):
        if (self._last is None
                or abs(width - self._last_width) > HEADER_FADE_MAX_JUMP):
            return
        self._ghost = self._last
        self._ghost_width, self._ghost_title = (self._last_width,
                                                self._last_title)
        self._fade.reset()
        self._fade.play()      # finishes at once with animations turned off

    def _on_fade(self, value):
        self._progress = value
        self.queue_draw()

    def _on_fade_done(self, _anim):
        self._ghost = None
        self._progress = 1.0
        self.queue_draw()

    def do_snapshot(self, snapshot):
        drawing = Gtk.Snapshot()
        self.snapshot_child(self._child, drawing)
        node = drawing.to_node()
        if self._ghost is not None and self._progress < 1.0:
            snapshot.push_cross_fade(self._progress)
            self._draw_ghost(snapshot)
            snapshot.pop()
            if node is not None:
                snapshot.append_node(node)
            snapshot.pop()
        elif node is not None:
            snapshot.append_node(node)
        self._last, self._last_width = node, self.get_width()
        self._last_title = self._title_span()

    def _title_span(self):
        """Where the title sits, so the old drawing can be cut around it."""
        title = self._child.get_title_widget()
        if title is not None and title.get_mapped():
            ok, b = title.compute_bounds(self)
            if ok:
                return (b.get_x(), b.get_x() + b.get_width())
        mid = self.get_width() / 2
        return (mid, mid)

    def _draw_ghost(self, snapshot):
        """The old drawing in three pieces, each where its group now is.

        The header was a few pixels wider or narrower when it was drawn.
        Drawn whole, its window buttons would fade out beside the new ones;
        so what was left of the title stays left, the title stays centred,
        and what was right of it (the window buttons) stays right.
        """
        old, height = self._ghost_width, self.get_height()
        left, right = self._ghost_title
        dx = self.get_width() - old
        for x0, x1, shift in ((0, left, 0), (left, right, dx / 2),
                              (right, old, dx)):
            if x1 <= x0:
                continue
            snapshot.save()
            snapshot.translate(Graphene.Point().init(shift, 0))
            snapshot.push_clip(Graphene.Rect().init(x0, 0, x1 - x0, height))
            snapshot.append_node(self._ghost)
            snapshot.pop()
            snapshot.restore()

    def do_dispose(self):
        self._ghost = self._last = None
        if self._child is not None:
            self._child.unparent()
            self._child = None
        Gtk.Widget.do_dispose(self)


class _PageRoom(Gtk.Widget):
    """Holds the pages, so that a page wider than the window can't widen
    the header.

    The header and the page share one column, and a column is as wide as
    its widest part.  So a page wider than the window was laying the header
    out at the page's width too, and the header's end (the window buttons)
    was cut off along with the page.  While the sidebar is collapsed this
    reports no minimum width of its own: the page still gets the width it
    needs and is cut off at the window's edge, but the header keeps the
    window's width.  Beside the sidebar it measures as the page does, so
    the sidebar's slide still never gives a page less room than it needs
    (`Window._squeezed`).
    """

    def __init__(self, child, loose):
        super().__init__()
        self._child = child
        child.set_parent(self)
        self._loose = loose            # () -> bool: claim no minimum width
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def _need(self):
        return self._child.measure(Gtk.Orientation.HORIZONTAL, -1)[0]

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.VERTICAL and for_size >= 0:
            # The height for the width the page is really laid out at.
            for_size = max(for_size, self._need())
        low, nat, _b1, _b2 = self._child.measure(orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL and self._loose():
            low = 0
        return low, nat, -1, -1

    def do_size_allocate(self, width, height, baseline):
        wide = max(width, self._need())
        where = None
        if wide > width and self.get_direction() == Gtk.TextDirection.RTL:
            # Right to left, the page starts at the right: cut off its end.
            where = Gsk.Transform().translate(
                Graphene.Point().init(width - wide, 0))
        self._child.allocate(wide, height, baseline, where)

    def do_dispose(self):
        if self._child is not None:
            self._child.unparent()
            self._child = None
        Gtk.Widget.do_dispose(self)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='PipeWire Controller',
                         default_width=int(prefs.get('win_width') or 1080),
                         default_height=int(prefs.get('win_height') or 760))
        if prefs.get('win_maximized'):
            self.maximize()
        # With a breakpoint on it (the sidebar's, below), the window's
        # minimum is this size request and no longer its content's minimum,
        # so it takes the width it is given.  A page wider than that is cut
        # off at the window's edge instead of being drawn under whatever is
        # tiled next to it.
        self.set_size_request(MIN_WIDTH, MIN_HEIGHT)
        self.connect('close-request', self._save_window_state)
        self._pending_restarts: set[str] = set()
        self.advanced = bool(prefs.get('advanced'))
        self._advanced_widgets: list = []
        self._last_default_sink = None
        self._current_row = None

        self.toaster = Adw.ToastOverlay()
        # Not `hhomogeneous`: a stack otherwise takes the minimum width of its
        # widest page on every page, so the Patchbay's toolbar set the
        # narrowest width of the Dashboard too.  Vertical sizing stays
        # homogeneous: pages are scrollers, so nothing gains from it.
        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            hhomogeneous=False)

        # sidebar
        # Selection is off.  The current page's row is drawn with the
        # selected state instead (`_highlight`), and pages change on
        # activation.  With selection on, hiding and showing the sidebar
        # moved to the next page each time: GtkListBox moves focus to the
        # row after the focused one when it gets focus back, and selection
        # follows focus.
        self.listbox = Gtk.ListBox(css_classes=['navigation-sidebar'],
                                   selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.connect('row-activated', self._on_activate)
        # Up/Down still change the page, as they did when selection followed
        # focus: after a navigation key moves the focus, open the row it
        # landed on.  Only those keys, so focus arriving by any other route
        # (Tab, the sidebar coming back) never changes the page.
        keys = Gtk.EventControllerKey(
            propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self._on_sidebar_key)
        self.listbox.add_controller(keys)
        for name, title, icon, section in PAGES:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10,
                          margin_start=6, margin_end=6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=title, xalign=0))
            row.set_child(box)
            row.page_name = name
            row.section = section
            self.listbox.append(row)
        # Section headings are ListBox *headers*, not rows: they carry no
        # index, so `get_row_at_index()` in goto() keeps matching PAGES, and
        # keyboard navigation skips straight over them.
        self.listbox.set_header_func(self._sidebar_header)

        side_view = Adw.ToolbarView()
        # The sidebar is a slightly lifted surface, the way the Overview's
        # cards are, so the two panes read as foreground and background rather
        # than one flat field.  The tone and the footer hairline are in
        # style.css; this is only where the classes go on.
        side_view.add_css_class('pwctl-sidebar')
        side_header = Adw.HeaderBar()
        side_header.add_css_class('pwctl-sidebar-header')
        side_header.add_css_class('pwctl-header')
        app_title = Adw.WindowTitle(title='PipeWire Controller',
                                    subtitle='audio control center')
        app_title.add_css_class('app-title')
        side_header.set_title_widget(app_title)
        side_view.add_top_bar(side_header)
        # C3 and C14 are one line of chrome, so their hairlines must land on
        # one y.  Both are now drawn by the same libadwaita style rather than
        # by a hand-written border on one side and a toolbar style on the
        # other — that pairing is what put them 6px apart (the sidebar header
        # measured 46, the content header 40).  Equal heights come from
        # `.pwctl-header` in style.css.
        side_view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        side_sw = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_sw.set_child(self.listbox)
        side_view.set_content(side_sw)

        # advanced-settings toggle, bottom-left
        adv_box = Gtk.Box(spacing=8, margin_top=10, margin_bottom=10,
                          margin_start=16, margin_end=16)
        adv_box.append(Gtk.Image.new_from_icon_name(
            'applications-engineering-symbolic'))
        adv_label = Gtk.Label(label='Advanced', xalign=0, hexpand=True)
        adv_box.append(adv_label)
        self.adv_switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                                     active=self.advanced,
                                     tooltip_text='Show advanced settings '
                                                  'throughout the app')
        self.adv_switch.connect('notify::active', self._on_advanced)
        adv_box.append(self.adv_switch)
        adv_box.add_css_class('pwctl-sidebar-footer')
        side_view.add_bottom_bar(adv_box)

        # content
        self.banner = Adw.Banner(revealed=False, button_label='Restart now')
        self.banner.connect('button-clicked', self._restart_pending)
        self.content_title = Adw.WindowTitle(title='Dashboard')
        self.content_title.add_css_class('page-title')
        content_header = Adw.HeaderBar()
        content_header.add_css_class('pwctl-header')
        self.content_header = content_header
        content_header.set_title_widget(self.content_title)
        content_header.pack_end(self._build_presets_button())
        # Below the width where the header's labels fit, its three labelled
        # controls drop to their icons.  Without this the header bar keeps
        # them at full width and slides the page title on top of the
        # Overview/Mixer switcher.  Narrower still, the switcher moves to the
        # bottom of the Dashboard, or the window buttons would be cut off.
        # The widths are measured, never constants (see `fit_header`), so
        # they hold for any font.
        self._header_fit = _HeaderFit(content_header, self._set_header_compact,
                                      self._set_header_tight, self._is_narrow)
        self._switcher_width = None    # see fit_header
        self._switcher_share = 0
        content_view = Adw.ToolbarView()
        content_view.add_top_bar(self._header_fit)
        content_view.add_top_bar(self.banner)
        self._page_room = _PageRoom(self.stack, self._is_narrow)
        content_view.set_content(self._page_room)
        # The hairline under the header bar (and under the restart banner when
        # it is showing).  libadwaita's own top-bar style, so it follows the
        # theme instead of guessing at a border colour.
        content_view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)

        # An overlay split, not a navigation split.  Both collapse, but a
        # collapsed navigation split makes the sidebar and the page separate
        # screens: every page change costs two clicks, and the Advanced
        # switch in the sidebar's footer ends up behind one of them.  An
        # overlay keeps the page on screen and slides the sidebar over it.
        # Pinned: on its own, collapsing hides the sidebar (and uncollapsing
        # shows it) with no animation, so it vanished in a single frame.
        # Pinned, it moves only when told to, and `_on_narrow`/`_on_wide`
        # tell it with libadwaita's own slide.
        self.split = Adw.OverlaySplitView(
            min_sidebar_width=210, max_sidebar_width=240,
            show_sidebar=bool(prefs.get('sidebar_shown')), pin_sidebar=True)
        self.split.set_sidebar(side_view)
        self.split.set_content(content_view)
        self.toaster.set_child(self.split)
        self.set_content(self.toaster)

        # The breakpoint has no setter: crossing it starts the slide, and
        # the split collapses once the sidebar is out of the way.  It still
        # makes the window's minimum its size request (DR-013).
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(
            f'max-width: {SIDEBAR_COLLAPSE_SP}sp'))
        bp.connect('apply', self._on_narrow)
        bp.connect('unapply', self._on_wide)
        self.add_breakpoint(bp)
        self.split.connect('notify::show-sidebar', self._on_sidebar_shown)
        self.split.connect('notify::collapsed', self._on_collapsed)
        self._auto_hiding = False      # sliding out for the breakpoint
        self._collapse_tick = None     # watching that slide
        self._step_tick = None         # waiting for the breakpoint to land
        self._mapped_at = 0
        self.connect('map', self._on_map)

        # Shows and hides the sidebar, at any width.  It is packed before
        # anything a page adds, so it is the first thing in the header on
        # every page, and bound both ways so it also follows a change the
        # breakpoint makes.
        self.sidebar_button = Gtk.ToggleButton(
            icon_name='sidebar-show-symbolic')
        self.split.bind_property(
            'show-sidebar', self.sidebar_button, 'active',
            GObject.BindingFlags.SYNC_CREATE
            | GObject.BindingFlags.BIDIRECTIONAL)
        self.sidebar_button.connect('notify::active', self._sync_sidebar_tip)
        self._sync_sidebar_tip()
        content_header.pack_start(self.sidebar_button)

        # pages
        self.pages = {
            'dashboard': Dashboard(self),
            'graph': GraphPage(self),
            'devices': DevicesPage(self),
            'virtual': VirtualPage(self),
            'surround': SurroundPage(self),
            'server': ServerPage(self),
            'streams': StreamsPage(self),
            'policy': PolicyPage(self),
            'wireplumber': WirePlumberPage(self),
            'chains': ChainsPage(self),
            'effects': EffectsPage(self),
            'enhance': EnhancePage(self),
            'paths': PathsPage(self),
            'hrir': HrirPage(self),
            'monitor': MonitorPage(self),
            'tools': ToolsPage(self),
        }
        for name, page in self.pages.items():
            self.stack.add_named(page.widget, name)

        # Two Dashboard-owned controls that live in the window's header bar.
        # The Overview/Mixer switcher is packed at the start, where the mockup
        # puts it, and stays there on every page: on another page neither
        # segment is lit and clicking one brings you back to the Dashboard
        # showing that view, so the mixer is always one click away.  The
        # volume-style picker is a global preference (it changes the sliders
        # on Devices, the Equalizer and Signal Paths too), so it sits beside
        # Device Presets rather than floating over the Dashboard.
        dash = self.pages['dashboard']
        content_header.pack_start(dash.view_switcher)
        content_header.pack_end(dash.style_button)
        # ...and where the switcher goes when the header is too narrow for
        # it: a bar along the window's bottom, shown on the Dashboard only.
        # The window's and not the page's, so it is the window's width
        # even while the page is wider than that.
        content_view.add_bottom_bar(dash.switcher_bar)
        self.fit_header()
        # The labels' widths follow the font, so a font or text-scale change
        # moves the width where they stop fitting.
        settings = Gtk.Settings.get_default()
        handlers = [settings.connect(f'notify::{key}', self.fit_header)
                    for key in ('gtk-font-name', 'gtk-xft-dpi')]
        self.connect('destroy', lambda *_: [settings.disconnect(h)
                                            for h in handlers])

        start = 0
        import os
        self._debug_page = bool(os.environ.get('PWCTL_PAGE'))
        want = os.environ.get('PWCTL_PAGE') or prefs.get('last_page')
        if want:
            for i, (n, _t, _i2, _s) in enumerate(PAGES):
                if n == want:
                    start = i
        self.goto(PAGES[start][0])
        GLib.timeout_add_seconds(5, self._autoload_tick)

        # link / service watcher for system notifications
        self._last_patch_ts = 0.0
        self._watch_links = None       # set of (out_name, port, in_name, port)
        self._watch_states: dict[str, str] = {}
        self._watch_busy = False
        # Service failures come from systemd's own signals when it is on the
        # session bus: at once, and including a failure that is restarted
        # before a poll would have looked.  The 5 s poll below covers them
        # only when it is not.  Links stay on the poll (DR-004 step 4).
        self._unit_watch = units.watch(WATCHED_SERVICES, self._service_state)
        GLib.timeout_add_seconds(5, self._watch_tick)

    # ------------------------------------------------------------ advanced --
    def register_advanced(self, widget):
        """Track a widget that is only visible while Advanced mode is on."""
        self._advanced_widgets.append(widget)
        widget.set_visible(self.advanced)

    def _on_advanced(self, switch, _p):
        self.advanced = switch.get_active()
        prefs.save(advanced=self.advanced)
        for w in self._advanced_widgets:
            w.set_visible(self.advanced)

    # ------------------------------------------------------ device presets --
    def _build_presets_button(self):
        btn = Gtk.MenuButton(tooltip_text='Device presets')
        self._presets_content = Adw.ButtonContent(
            icon_name='user-bookmarks-symbolic', label='Device Presets')
        btn.set_child(self._presets_content)
        self._presets_popover = Gtk.Popover()
        self._presets_popover.connect('show', self._fill_presets_popover)
        btn.set_popover(self._presets_popover)
        return btn

    def _fill_presets_popover(self, _pop):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12, width_request=340)
        title = Gtk.Label(label='Device presets', xalign=0)
        title.add_css_class('heading')
        box.append(title)
        hint = Gtk.Label(
            label='A preset stores channel-mix settings, volume and card '
                  'profile for one output device.',
            xalign=0, wrap=True, max_width_chars=42)
        hint.add_css_class('caption')
        hint.add_css_class('dim-label')
        box.append(hint)

        auto = Gtk.Box(spacing=8, margin_top=6)
        auto_lbl = Gtk.Label(label='Auto-load when default output changes',
                             xalign=0, hexpand=True, wrap=True)
        sw = Gtk.Switch(valign=Gtk.Align.CENTER,
                        active=bool(prefs.get('autoload_presets')))
        sw.connect('notify::active',
                   lambda s, _p: prefs.save(autoload_presets=s.get_active()))
        auto.append(auto_lbl)
        auto.append(sw)
        box.append(auto)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))

        save_btn = Gtk.Button()
        save_btn.set_child(Adw.ButtonContent(
            icon_name='document-save-symbolic',
            label='Save preset for current output'))
        save_btn.connect('clicked', self._save_preset)
        box.append(save_btn)

        saved = presets.all_presets()
        if saved:
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
            for name, p in sorted(saved.items()):
                row = Gtk.Box(spacing=8)
                labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 hexpand=True)
                t = Gtk.Label(label=p.get('description', name), xalign=0)
                labels.append(t)
                sub = Gtk.Label(label=f'saved {p.get("saved", "?")}',
                                xalign=0)
                sub.add_css_class('caption')
                sub.add_css_class('dim-label')
                labels.append(sub)
                row.append(labels)
                apply_b = Gtk.Button(icon_name='media-playback-start-symbolic',
                                     tooltip_text='Apply now')
                apply_b.add_css_class('flat')
                apply_b.connect('clicked', self._apply_preset, p)
                del_b = Gtk.Button(icon_name='user-trash-symbolic',
                                   tooltip_text='Delete preset')
                del_b.add_css_class('flat')
                del_b.connect('clicked', self._delete_preset, name)
                row.append(apply_b)
                row.append(del_b)
                box.append(row)
        self._presets_popover.set_child(box)

    def _save_preset(self, _b):
        self._presets_popover.popdown()
        async_call(presets.snapshot,
                   lambda p, e: self.toast(
                       f'Preset saved for {p["description"]}' if p and not e
                       else 'Could not save preset'))

    def _apply_preset(self, _b, preset):
        self._presets_popover.popdown()

        def done(actions, e):
            if e or actions is None:
                self.toast('Preset failed')
                return
            if any('channel-mix' in a for a in actions):
                self.flag_restart('pulse')
            self.toast(f'Applied preset for {preset["description"]}: '
                       + (', '.join(actions) if actions else 'nothing to do'))
        async_call(lambda: presets.apply(preset), done)

    def _delete_preset(self, _b, name):
        presets.delete(name)
        self._presets_popover.popdown()
        self.toast('Preset deleted')

    def _autoload_tick(self):
        if not prefs.get('autoload_presets'):
            return True

        def check():
            return pw.read_default_names().get('default.audio.sink')

        def done(name, e):
            if e or not name:
                return
            prev, self._last_default_sink = self._last_default_sink, name
            if prev is None or name == prev:
                return
            preset = presets.preset_for(name)
            if preset:
                self._apply_preset(None, preset)
        async_call(check, done)
        return True

    # ---------------------------------------------------------- navigation --
    def _sidebar_header(self, row, before):
        """One micro heading above the first row of each section."""
        if before is not None and before.section == row.section:
            row.set_header(None)
            return
        lbl = micro(row.section)
        lbl.add_css_class('sidebar-section')
        lbl.set_margin_top(4 if before is None else 14)
        lbl.set_margin_bottom(2)
        lbl.set_margin_start(14)
        row.set_header(lbl)

    def _on_activate(self, _lb, row):
        if row is not None:
            self.goto(row.page_name)

    def _on_sidebar_key(self, _ctl, keyval, _code, state):
        """Up, Down, Home, End, Page Up/Down: follow the focus to its page.

        The list moves the focus itself, after this handler; the page
        follows once it has.
        """
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_KP_Up,
                      Gdk.KEY_KP_Down, Gdk.KEY_Home, Gdk.KEY_End,
                      Gdk.KEY_Page_Up, Gdk.KEY_Page_Down):
            GLib.idle_add(self._follow_sidebar_focus)
        return False

    def _follow_sidebar_focus(self):
        row = self.listbox.get_focus_child()
        if isinstance(row, Gtk.ListBoxRow) and row is not self._current_row:
            self.goto(row.page_name)
        return False

    def _highlight(self, current):
        """Draw one row as the current page, without selecting anything.

        `.navigation-sidebar` styles `row:selected`, which CSS reads from the
        SELECTED state flag, so setting the flag keeps libadwaita's look.
        The flag is only a look, though: a screen reader learns which page
        is current from the accessible state, set beside it.  It takes
        lists, and ints rather than bools (gotchas).
        """
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            on = row is current
            if on:
                row.set_state_flags(Gtk.StateFlags.SELECTED, False)
            else:
                row.unset_state_flags(Gtk.StateFlags.SELECTED)
            row.update_state([Gtk.AccessibleState.SELECTED], [int(on)])
            i += 1
        self._current_row = current

    # ------------------------------------------------------------ sidebar --
    def _sync_sidebar_tip(self, *_a):
        """The tooltip says what a click will do.

        The icon is the same sidebar in both states, and the button is drawn
        pressed while the sidebar shows, as in GNOME's own apps.  Adwaita has
        no separate "hide" icon to pair it with.
        """
        shown = self.sidebar_button.get_active()
        self.sidebar_button.set_tooltip_text(
            'Hide the sidebar' if shown else 'Show the sidebar')

    def _on_map(self, *_a):
        self._mapped_at = GLib.get_monotonic_time()

    def _still(self):
        """Put the sidebar in place without a slide?

        With animations off, when the user asked for reduced motion, and
        while the window is only just appearing.
        """
        return (not self.get_mapped()
                or GLib.get_monotonic_time() - self._mapped_at < SETTLE_US
                or not Adw.get_enable_animations(self)
                or reduce_motion(self))

    def _on_narrow(self, _bp):
        """The window got too narrow for the sidebar beside the page.

        The sidebar slides out, the page widening behind it as it goes (the
        slide the header button makes), and the split collapses once it is
        gone, so a narrow window opens on its page rather than under the
        sidebar.  That slide needs the page to fit beside what is left of the
        sidebar; if it stops fitting, the split collapses at once and the
        slide carries on over the page instead.
        """
        self._stop_watching_slide()
        if not self.split.get_show_sidebar() or self._still():
            self._collapse(at_once=True)
        elif self._squeezed():
            # Collapsed now, so no frame lays the page out beside the
            # sidebar in too little room; pinned, the sidebar stays where it
            # is, over the page, until the slide starts.
            self.split.set_collapsed(True)
            self._next_frame(self._slide_out)
        else:
            self._next_frame(self._slide_out)

    def _next_frame(self, step):
        """Run `step` once the window's content is back on screen.

        libadwaita unmaps the window's content while it applies a breakpoint
        (measured), and an animation started on an unmapped widget is skipped
        to its end: called from the breakpoint itself, the sidebar would
        jump instead of slide.
        """
        def tick(_window, _clock):
            if not self.split.get_mapped():
                return GLib.SOURCE_CONTINUE
            self._step_tick = None
            step()
            return GLib.SOURCE_REMOVE
        self._step_tick = self.add_tick_callback(tick)

    def _slide_out(self):
        split = self.split
        if not split.get_show_sidebar():
            return
        if split.get_collapsed() or self._squeezed():
            # No room for the page beside the sidebar: the sidebar slides
            # out over the page instead.
            split.set_collapsed(True)
            split.set_show_sidebar(False)
            return
        self._auto_hiding = True
        split.set_show_sidebar(False)
        self._collapse_tick = split.add_tick_callback(self._watch_slide)

    def _is_narrow(self):
        """Is the sidebar collapsed, so the page has the window to itself?

        Only then may the header lose its switcher (`_HeaderFit`) and a
        page wider than the window be cut off at its edge (`_PageRoom`).
        Beside the sidebar both measure as they always did, so the slide
        decides as before, and the sidebar collapses before any page would
        have too little room beside it.
        """
        split = getattr(self, 'split', None)
        return split is not None and split.get_collapsed()

    def _on_collapsed(self, *_a):
        self._header_fit.queue_resize()
        self._page_room.queue_resize()

    def _squeezed(self):
        """The page and the sidebar beside it need more than the window has.

        The split then shrinks the sidebar below its own minimum rather than
        the page, and the window lays it all out wider than itself (GTK and
        libadwaita warn, measured), so the pair is compared with the window.
        """
        return (self.split.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
                > self.get_width())

    def _watch_slide(self, split, _clock):
        # The split unmaps the sidebar once it has finished sliding out.  A
        # sidebar shown again meanwhile is the user opening it: collapse, and
        # it stays open over the page.
        if (self._squeezed() or not split.get_sidebar().get_mapped()
                or split.get_show_sidebar()):
            self._collapse_tick = None
            self._collapse()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _stop_watching_slide(self):
        if self._step_tick is not None:
            self.remove_tick_callback(self._step_tick)
            self._step_tick = None
        if self._collapse_tick is not None:
            self.split.remove_tick_callback(self._collapse_tick)
            self._collapse_tick = None

    def _collapse(self, at_once=False):
        self._stop_watching_slide()
        split = self.split
        if at_once and split.get_show_sidebar():
            # Unpinned, collapsing hides the sidebar in the same frame.
            split.set_pin_sidebar(False)
            split.set_collapsed(True)
            split.set_pin_sidebar(True)
        else:
            # Pinned, a slide still under way carries on over the page.
            split.set_collapsed(True)
        self._auto_hiding = False

    def _on_wide(self, _bp):
        """Room for the sidebar again: it comes back as it was last left
        while it had room, sliding in with the page making way."""
        self._stop_watching_slide()
        self._auto_hiding = False
        split = self.split
        want = bool(prefs.get('sidebar_shown'))
        if split.get_collapsed():
            if want and not split.get_show_sidebar() and self._still():
                # Unpinned, uncollapsing shows the sidebar in the same frame.
                split.set_pin_sidebar(False)
                split.set_collapsed(False)
                split.set_pin_sidebar(True)
                return
            split.set_collapsed(False)
        if split.get_show_sidebar() != want:
            self._next_frame(lambda: self.split.set_show_sidebar(
                bool(prefs.get('sidebar_shown'))))

    def _on_sidebar_shown(self, split, _p):
        # Only a choice made while the sidebar has a place of its own is
        # remembered.  Opening it over the page in a narrow window is how
        # you reach another page, not a preference, and the breakpoint's
        # slide is not a choice at all.
        shown = split.get_show_sidebar()
        if (not split.get_collapsed() and not self._auto_hiding
                and shown != bool(prefs.get('sidebar_shown'))):
            prefs.save(sidebar_shown=shown)

    def _set_header_widget(self, page_name, title):
        """A page may own the header bar's title slot.

        Nothing claims it at the moment — the Dashboard's Overview/Mixer
        switcher moved to `pack_start`, so it stays visible on every page —
        but the hook is how a page gets a control into the header bar without
        the window knowing anything about it, and it costs no page height.
        """
        page = self.pages.get(page_name) if hasattr(self, 'pages') else None
        widget = getattr(page, 'header_widget', None)
        if widget is not None:
            self.content_header.set_title_widget(widget)
        else:
            self.content_title.set_title(title)
            self.content_header.set_title_widget(self.content_title)
        self.fit_header()

    # -------------------------------------------------------- header width --
    def _set_header_compact(self, compact):
        """Show the header's labelled controls as icons only, or labelled."""
        self._presets_content.set_label('' if compact else 'Device Presets')
        dash = getattr(self, 'pages', {}).get('dashboard')
        if dash is not None:
            dash.set_header_compact(compact)

    def _set_header_tight(self, tight):
        """Move the Overview/Mixer switcher out of the header, or back.

        Below the header's width with icons only, keeping it would cut off
        the window buttons.  The Dashboard then shows the same choice as a
        bar along its bottom; from other pages the sidebar's Dashboard row
        gets there.
        """
        dash = getattr(self, 'pages', {}).get('dashboard')
        if dash is not None:
            dash.set_header_tight(tight)

    def fit_header(self, *_a):
        """Work out the widths below which the labels, then the switcher, go.

        Measured with the labels showing: the header bar's own minimum, plus
        what each labelled control wants beyond its minimum.  The title may
        still shrink to its minimum, but the controls may not shrink below
        their natural width, because the header bar then slides the title
        over the switcher instead of squeezing them.  The switcher goes
        below the minimum with icons only (`floor`), and `least` is the
        minimum without it.  The widths depend on the font, the page title
        and the current slider style's name, so they are measured again when
        any of those changes.
        """
        dash = getattr(self, 'pages', {}).get('dashboard')
        if dash is None or dash.style_button.get_parent() is None:
            return                     # still being built
        fit = self._header_fit
        switcher = dash.view_switcher
        labelled = (switcher, dash.style_button,
                    self._presets_content.get_parent())
        horizontal, vertical = (Gtk.Orientation.HORIZONTAL,
                                Gtk.Orientation.VERTICAL)
        switcher.set_visible(True)
        self._set_header_compact(False)
        need = self.content_header.measure(horizontal, -1)[0]
        for w in labelled:
            low, nat, _b1, _b2 = w.measure(horizontal, -1)
            need += nat - low
        # The tallest of the layouts, so the header keeps one height: with a
        # large font the labelled switcher is a pixel taller than the icons,
        # and C19 has to stay level with the sidebar's line.
        height = self.content_header.measure(vertical, -1)[0]
        self._set_header_compact(True)
        floor = self.content_header.measure(horizontal, -1)[0]
        height = max(height, self.content_header.measure(vertical, -1)[0])
        # What the switcher takes up in the header, its spacing included.
        # Measured by taking it out, which costs it the keyboard focus, so
        # only when its own width has changed (a font or theme change) and
        # never on a page change: the switcher's own click changes the page.
        # Taking it out can only make the header lower, so `height` stands.
        width = switcher.measure(horizontal, -1)[0]
        if width != self._switcher_width:
            switcher.set_visible(False)
            self._switcher_share = (
                floor - self.content_header.measure(horizontal, -1)[0])
            self._switcher_width = width
        switcher.set_visible(not fit.tight)
        self._set_header_compact(fit.compact)
        fit.need, fit.floor, fit.height = need, floor, height
        fit.least = floor - self._switcher_share
        fit.queue_resize()

    def _save_window_state(self, *_a):
        prefs.save(win_width=self.get_width() or 1080,
                   win_height=self.get_height() or 760,
                   win_maximized=self.is_maximized())
        return False

    def goto(self, name):
        """Show a page.  Every way of changing page comes through here."""
        index = next((i for i, (n, _t, _i2, _s) in enumerate(PAGES)
                      if n == name), None)
        if index is None:
            return
        row = self.listbox.get_row_at_index(index)
        self._highlight(row)
        self.stack.set_visible_child_name(name)
        self._set_header_widget(name, PAGES[index][1])
        dash = getattr(self, 'pages', {}).get('dashboard')
        if dash is not None:
            dash.set_on_page(name == 'dashboard')
        if not getattr(self, '_debug_page', False):
            prefs.save(last_page=name)
        # In a narrow window the sidebar lies over the page: once a page is
        # picked, get out of its way.
        if self.split.get_collapsed() and self.split.get_show_sidebar():
            self.split.set_show_sidebar(False)

    # ------------------------------------------------------- notifications --
    def notify_user(self, title: str, body: str):
        """Desktop notification (used for broken links, failures, xruns)."""
        app = self.get_application()
        if not app:
            return
        note = Gio.Notification.new(title)
        note.set_body(body)
        note.set_priority(Gio.NotificationPriority.NORMAL)
        app.send_notification(None, note)

    def mark_user_patch(self):
        """Patch changes we made ourselves shouldn't raise link warnings."""
        import time
        self._last_patch_ts = time.monotonic()

    def _service_state(self, unit, state):
        """A service's state, from the signal or from the poll."""
        prev = self._watch_states.get(unit)
        self._watch_states[unit] = state
        if (state == 'failed' and prev not in (None, 'failed')
                and prefs.get('notify_services')):
            self.notify_user('Audio service failed',
                             f'{unit} entered the failed state — '
                             'check the Monitor page.')

    def _watch_tick(self):
        import time
        want_links = bool(prefs.get('notify_links'))
        want_services = (bool(prefs.get('notify_services'))
                         and self._unit_watch is None)
        if (not want_links and not want_services) or self._watch_busy:
            return True
        self._watch_busy = True

        def collect():
            states = {}
            if want_services:
                for unit in WATCHED_SERVICES:
                    states[unit] = system.unit_state(unit)
            links, names = None, {}
            if want_links:
                g = graph.snapshot()
                ports = {p.id: p for n in g.nodes.values()
                         for p in n.inputs + n.outputs}
                links = set()
                for link in g.links:
                    op, ip = ports.get(link.out_port), ports.get(link.in_port)
                    if not op or not ip:
                        continue
                    key = (g.nodes[link.out_node].name, op.name,
                           g.nodes[link.in_node].name, ip.name)
                    links.add(key)
                names = {n.name: n.label for n in g.nodes.values()}
            return states, links, names

        def done(result, error):
            self._watch_busy = False
            if error or result is None:
                return
            states, links, names = result
            for unit, state in states.items():
                self._service_state(unit, state)
            if links is not None:
                prev_links = self._watch_links
                self._watch_links = links
                recent_patch = (time.monotonic() - self._last_patch_ts) < 15
                if prev_links is not None and not recent_patch:
                    gone = prev_links - links
                    # only links whose endpoints still exist: a closed app
                    # takes its node away and is not a broken link
                    broken = [(o, i) for o, _op, i, _ip in gone
                              if o in names and i in names]
                    if broken:
                        o, i = broken[0]
                        extra = (f' (and {len(broken) - 1} more)'
                                 if len(broken) > 1 else '')
                        self.notify_user(
                            'Audio link disconnected',
                            f'{names.get(o, o)} → {names.get(i, i)} was '
                            f'disconnected{extra}.')
            return
        async_call(collect, done)
        return True

    # ------------------------------------------------------------ feedback --
    def toast(self, message, timeout=3):
        """Show `message` as plain text.

        A toast's title is Pango markup, so a bare '&' or '<' (a device
        called "Game & Chat", an error from PipeWire) would make the whole
        title fail to parse and the toast show up blank.  Escaped here, once,
        so callers never pass markup.
        """
        self.toaster.add_toast(Adw.Toast(title=esc(message), timeout=timeout))

    def flag_restart(self, which: str):
        self._pending_restarts.add(which)
        labels = ' + '.join(RESTART_UNITS[w][0]
                            for w in ('pipewire', 'pulse', 'wireplumber')
                            if w in self._pending_restarts)
        self.banner.set_title(f'Saved. Restart {labels} to apply.')
        self.banner.set_revealed(True)

    def _restart_pending(self, _b):
        pending = set(self._pending_restarts)
        if 'pipewire' in pending:      # full stack restart covers the others
            pending = {'pipewire'}

        def work():
            for w in pending:
                RESTART_UNITS[w][1]()
            return True
        self.mark_user_patch()      # don't report the restart as broken links
        self.banner.set_revealed(False)
        self._pending_restarts.clear()
        self.toast('Restarting audio services…')
        async_call(work, lambda r, e: self.toast(
            'Audio services restarted' if not e else f'Restart failed: {e}'))


class App(Adw.Application):
    def __init__(self):
        # Matches the desktop entry filename, so the shell can associate the
        # window with its launcher icon, and is the id the Flatpak will use.
        super().__init__(
            application_id='io.github.knightinfected.PipeWireControlCenter',
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Meters are child processes and outlive a crash or a kill, so clear
        # out anything an earlier run left behind before starting new ones.
        # The same goes for livestate's two listeners.
        levels.reap_orphans()
        livestate.reap_orphans()
        # An equalizer left playing an unsaved preview or a bypass by a run
        # that crashed goes back to its saved curve (DR-016).  Off the UI
        # thread: it may restart a unit.
        from .backend import enhance, eqlive
        eqlive.reap_orphans()
        threading.Thread(target=enhance.reconcile_all, daemon=True).start()
        # ...and make sure this run leaves nothing behind either: a plain
        # `kill` (or Ctrl-C) would otherwise skip every Gtk shutdown path.
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
        css = Gtk.CssProvider()
        css_file = Path(__file__).parent / 'style.css'
        if css_file.is_file():
            css.load_from_path(str(css_file))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Our own icons.  Every audio/routing/DSP noun is shipped with the app
        # as `pwctl-*-symbolic`, because the system theme cannot be relied on:
        # Adwaita and Breeze share only ~305 symbolic names and that set has no
        # equalizer, monitor, bluetooth or waveform glyph, so nine names were
        # rendering as `image-missing` on this machine alone.  Freedesktop
        # *verbs* (list-add, go-next, user-trash...) stay stock on purpose --
        # those should follow the user's desktop.  `resolve()` for the same
        # reason the launcher uses realpath: the package is reached through a
        # symlink in /usr/bin.
        icons = Path(__file__).resolve().parent / 'icons'
        if icons.is_dir():
            Gtk.IconTheme.get_for_display(
                Gdk.Display.get_default()).add_search_path(str(icons))

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()

    def _on_signal(self):
        self.quit()     # runs do_shutdown: stops the meters and listeners
        return GLib.SOURCE_REMOVE

    def do_shutdown(self):
        levels.stop_all()
        livestate.stop()
        # nothing unsaved keeps playing once the app is gone: an open
        # editor's preview, a bypass from the page
        from .backend import enhance, eqlive
        eqlive.stop_all()
        enhance.reconcile_all()
        Adw.Application.do_shutdown(self)


def main():
    import sys

    from .compat import require
    require()   # says so and exits if libadwaita is too old, or cairo can't draw

    app = App()
    return app.run(sys.argv)
