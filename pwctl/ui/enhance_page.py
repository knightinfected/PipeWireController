"""Equalizer page: equalizers and clean microphones, as a list beside the
selected one (the user's "Split View" template, 2026-10-28).

Left, the list: Equalizers or Mic, each running one above the ones that are
not, with its curve (or a microphone), the default output's star, where its
sound goes, and its switch.  A file dropped anywhere on it becomes a new
equalizer.  Right, the selected one in full (`eq_detail`): for an
equalizer, the editor that used to be a dialog, inline, with Save and
Revert above it.

Layout: an `Adw.NavigationSplitView` in the page's own `Adw.BreakpointBin`,
as on Devices.  Below `COLLAPSE_SP` of page width it shows one pane at a
time; picking a row slides its detail in, with a back button.  Each detail
has a bin of its own, so the editor reflows to the width it gets.

Leaving an equalizer with changes not saved asks first (Save, Discard,
Keep editing): here, on another row, on the other tab, and when the window
closes (`has_unsaved`, `app.Window`).  Leaving one that only Compare
changed puts its saved curve back without asking.

Live state comes from `livestate` while the page is mapped (the 3 s poll
only where it cannot listen); each delivery is collected off the main loop
(unit states, files) and applied in order, in place.  The list is rebuilt
only when the set of rows changes, and a row that changes group (switched
on or off) moves once the pointer leaves the list, never under it.
"""

from __future__ import annotations

from dataclasses import asdict

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..backend import chains, enhance, livestate, prefs, pw, rules  # noqa: E402
from ..backend import eqcurve as E  # noqa: E402
from .eq_detail import DRAFT, EqDetail, MicDetail, _label, apps_into, \
    raw_mic, route_of  # noqa: E402
from .eq_graph import CurveThumb  # noqa: E402
from .widgets import async_call, confirm, micro, pick_file  # noqa: E402

COLLAPSE_SP = 700          # below this page width, one pane at a time
MIN_PAGE_WIDTH = 352       # under the window's 360, as Devices
TABS = (('eq', 'Equalizers'), ('mic', 'Mic'))
GROUPS = {0: 'New', 1: 'Running', 2: 'Not running'}
NOUN = {'eq': 'equalizer', 'mic': 'clean microphone'}


def collect(dump):
    """Everything one delivery needs (runs off the main loop)."""
    items = enhance.list_enhancements()
    states = {e.id: enhance.status(e) for e in items}
    nodes = pw.list_audio_nodes(dump)
    streams = pw.list_streams(dump)
    edges = chains.target_edges(chains.list_chains())
    # which app is pinned where (rules.stream_is_bound reads the file per
    # call; once per delivery here)
    app_rules = [r for r in rules.app_rules() if rules.rule_enabled(r)]
    bound = {}
    for s in streams:
        if not (s.is_playback and s.is_app):
            continue
        where = {(r.get('props') or {}).get('target.object')
                 for r in app_rules if rules.rule_matches(r, s.props or {})}
        where.discard(None)
        if where:
            bound[s.id] = where
    return {'items': items, 'states': states, 'nodes': nodes,
            'streams': streams, 'edges': edges, 'bound': bound}


# ================================================================ list ==

class _Entry:
    """One row: an equalizer, a clean microphone, or the one being made."""

    def __init__(self, page, kind, enh=None):
        self.page = page
        self.kind = kind
        self.enh = enh
        self.key = enh.id if enh is not None else DRAFT
        self.rank = 0 if enh is None else (1 if enh.enabled else 2)
        self.want_rank = self.rank
        self._updating = False

        row = Gtk.ListBoxRow()
        row.entry = self
        row.add_css_class('eqp-row')
        box = Gtk.Box(spacing=10, margin_top=5, margin_bottom=5)
        if kind == 'eq':
            self.thumb = CurveThumb(enhance.curve_of(enh) if enh is not None
                                    else E.default_curve(), width=52,
                                    height=30)
            box.append(self.thumb)
        else:
            self.thumb = None
            icon = Gtk.Image.new_from_icon_name('pwctl-source-mic-symbolic')
            icon.set_pixel_size(18)
            icon.add_css_class('eqp-mic-icon')
            icon.set_valign(Gtk.Align.CENTER)
            box.append(icon)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                      hexpand=True, valign=Gtk.Align.CENTER)
        head = Gtk.Box(spacing=5)
        self.title = _label(css=('eqp-row-title',))
        head.append(self.title)
        self.star = Gtk.Image.new_from_icon_name('starred-symbolic')
        self.star.set_pixel_size(12)
        self.star.add_css_class('eqp-star')
        self.star.set_visible(False)
        head.append(self.star)
        self.dot = Gtk.Box(valign=Gtk.Align.CENTER, visible=False,
                           tooltip_text='Changes not saved')
        self.dot.add_css_class('eqp-dirty')
        head.append(self.dot)
        col.append(head)
        self.sub = _label(css=('caption', 'dim-label'))
        col.append(self.sub)
        box.append(col)
        self.switch = None
        if enh is not None:
            self.switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                                     active=enh.enabled,
                                     tooltip_text='Running')
            self.switch.connect('notify::active', self._switched)
            box.append(self.switch)
        row.set_child(box)
        self.row = row

    @property
    def name(self):
        return self.enh.name if self.enh is not None else ''

    def _switched(self, sw, _p):
        if self._updating or self.enh is None:
            return
        on = sw.get_active()
        if on != self.enh.enabled:
            self.page.set_running(self.enh, on)

    def paint(self, data, detail):
        mine = detail if detail is not None and detail.key == self.key \
            else None
        if self.enh is None:
            self.title.set_label(mine.title if mine else 'New')
            self.sub.set_label('Not created yet')
            if self.thumb is not None and mine is not None:
                self._thumb(mine.session.curve)
            return
        enh = self.enh
        self.title.set_label(enh.name)
        self.dot.set_visible(mine is not None and mine.dirty)
        if self.thumb is not None:
            self._thumb(mine.session.curve if mine is not None
                        and mine.session.dirty else enhance.curve_of(enh))
        if data is None:
            return                  # live state comes with the first read
        node = next((n for n in data['nodes'] if n.name == enh.node_name),
                    None)
        state = (data or {}).get('states', {}).get(enh.id, '')
        sub = self._eq_sub(enh, node, state, data) if self.kind == 'eq' \
            else self._mic_sub(enh, node, state, data)
        self.sub.set_label(sub)
        self.sub.set_tooltip_text(sub)
        default = node is not None and node.is_default
        self.star.set_visible(default)
        self.star.set_tooltip_text('Default output' if self.kind == 'eq'
                                   else 'Default input')
        if self.switch.get_active() != enh.enabled:
            self._updating = True
            self.switch.set_active(enh.enabled)
            self._updating = False
        (self.row.remove_css_class if enh.enabled
         else self.row.add_css_class)('off')
        self.want_rank = 1 if enh.enabled else 2

    def _thumb(self, curve):
        if self.thumb.curve != curve:
            self.thumb.curve = curve
            self.thumb.queue_draw()

    @staticmethod
    def _eq_sub(enh, node, state, data):
        if enh.enabled and node is not None:
            n = len(apps_into(node, data))
            apps = f'{n} app{"s" if n != 1 else ""}' if n else 'No apps'
            if node.is_default:
                return 'Default output' + (f' · {apps}' if n else '')
            path = route_of(enh.node_name, data['nodes'], data['streams'])
            out = (path[-1].description or path[-1].name) if path \
                else 'not connected'
            return f'{apps} → {out}'
        if enh.enabled:
            return 'Failed to start' if state == 'failed' else 'Starting…'
        n = len(enhance.curve_of(enh).bands)
        bands = f'{n} band{"s" if n != 1 else ""}'
        origin = enhance.origin_of(enh)
        return f'{origin[0]} · {bands}' if origin else bands

    @staticmethod
    def _mic_sub(enh, node, state, data):
        if enh.enabled and node is not None:
            src = raw_mic(enh, data)
            frm = f'from {src.description or src.name}' if src else \
                'from the default microphone'
            return ('Default input · ' + frm) if node.is_default \
                else frm[0].upper() + frm[1:]
        if enh.enabled:
            return 'Failed to start' if state == 'failed' else 'Starting…'
        bits = (['noise'] if enh.params.get('noise_suppression', True)
                else []) + ['echo']
        return 'Removes ' + ' and '.join(bits)


class EnhList:
    """The pane: the two tabs, Import and New, the rows, the drop zone."""

    def __init__(self, page):
        self.page = page
        self.entries = []
        self.current = None
        self._pointer_in = False
        self._regroup_due = False
        self._updating = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class('eqp-list')
        tools = Gtk.Box(spacing=6, margin_top=10, margin_bottom=6,
                        margin_start=10, margin_end=10)
        self.tabs = Adw.ToggleGroup(hexpand=True, can_shrink=True)
        for name, label in TABS:
            self.tabs.add(Adw.Toggle(name=name, label=label))
        self.tabs.set_active_name('eq')
        self.tabs.connect('notify::active-name', self._tab_picked)
        tools.append(self.tabs)
        self.import_btn = Gtk.Button(icon_name='document-open-symbolic',
                                     valign=Gtk.Align.CENTER,
                                     tooltip_text='Import an AutoEQ / APO '
                                                  'file')
        self.import_btn.add_css_class('flat')
        self.import_btn.connect('clicked', lambda _b: page.import_file())
        tools.append(self.import_btn)
        self.add_btn = Gtk.Button(icon_name='list-add-symbolic',
                                  valign=Gtk.Align.CENTER)
        self.add_btn.add_css_class('flat')
        self.add_btn.connect('clicked', lambda _b: page.new())
        tools.append(self.add_btn)
        box.append(tools)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class('navigation-sidebar')
        self.listbox.add_css_class('eqp-listbox')
        self.listbox.set_sort_func(self._sort)
        self.listbox.set_header_func(self._header)
        self.placeholder = Gtk.Label(margin_top=24, margin_bottom=24,
                                     wrap=True, justify=Gtk.Justification
                                     .CENTER)
        self.placeholder.add_css_class('dim-label')
        self.listbox.set_placeholder(self.placeholder)
        self.listbox.connect('row-activated', self._on_activate)
        keys = Gtk.EventControllerKey(
            propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self._on_key)
        self.listbox.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect('enter', lambda *_a: setattr(self, '_pointer_in',
                                                    True))
        motion.connect('leave', self._pointer_left)
        self.listbox.add_controller(motion)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vexpand=True, child=self.listbox)
        box.append(sw)

        self.drop_hint = Gtk.Box(spacing=8)
        self.drop_hint.add_css_class('eqp-drop')
        icon = Gtk.Image.new_from_icon_name('document-open-symbolic')
        icon.set_valign(Gtk.Align.START)
        self.drop_hint.append(icon)
        self.drop_hint.append(_label('Drop a ParametricEQ.txt anywhere on '
                                     'this list to add it.', wrap=True))
        box.append(self.drop_hint)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect('drop', self._on_drop)
        drop.connect('enter', lambda *_a: (box.add_css_class('drop-ready'),
                                           Gdk.DragAction.COPY)[1])
        drop.connect('leave', lambda *_a: box.remove_css_class('drop-ready'))
        box.add_controller(drop)
        self.widget = box
        self.set_tab('eq')

    # -- tabs -------------------------------------------------------------
    def set_tab(self, tab):
        self._updating = True
        self.tabs.set_active_name(tab)
        self._updating = False
        eq = tab == 'eq'
        self.import_btn.set_visible(eq)
        self.drop_hint.set_visible(eq)
        self.add_btn.set_tooltip_text(f'New {NOUN[tab]}')
        self.placeholder.set_label(
            'No equalizers yet.\nPress + to make one, or drop a file here.'
            if eq else 'No clean microphones yet.\nPress + to make one.')

    def _tab_picked(self, group, _p):
        if self._updating:
            return
        tab = group.get_active_name()
        if tab and tab != self.page.tab:
            self.page.switch_tab(tab)

    # -- rows -------------------------------------------------------------
    def keys(self):
        return [e.key for e in self.entries]

    def rebuild(self, kind, items, draft):
        child = self.listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            if isinstance(child, Gtk.ListBoxRow):
                self.listbox.remove(child)
            child = nxt
        self.entries = []
        self.current = None
        if draft is not None:
            self.entries.append(_Entry(self.page, kind))
        for enh in items:
            self.entries.append(_Entry(self.page, kind, enh))
        for e in self.entries:
            self.listbox.append(e.row)
        self._regroup_due = False
        return self.entries

    def entry(self, key):
        return next((e for e in self.entries if e.key == key), None)

    def ordered(self):
        return sorted(self.entries, key=self._order)

    @staticmethod
    def _order(e):
        return (e.rank, e.name.lower(), e.key)

    def _sort(self, a, b):
        ka, kb = self._order(a.entry), self._order(b.entry)
        return (ka > kb) - (ka < kb)

    def _header(self, row, before):
        entry = row.entry
        prev = before.entry if before is not None else None
        if prev is not None and prev.rank == entry.rank:
            row.set_header(None)
            return
        head = micro(GROUPS[entry.rank])
        head.add_css_class('eqp-group')
        head.set_margin_top(4 if prev is None else 14)
        row.set_header(head)

    def regroup(self, force=False):
        """Move rows whose state changed group -- now, or once the pointer
        leaves the list, so a row never jumps away from under it."""
        moved = [e for e in self.entries if e.want_rank != e.rank]
        if not moved:
            return
        if self._pointer_in and not force:
            self._regroup_due = True
            return
        for e in moved:
            e.rank = e.want_rank
        self._regroup_due = False
        self.listbox.invalidate_sort()
        self.listbox.invalidate_headers()

    def _pointer_left(self, *_a):
        self._pointer_in = False
        if self._regroup_due:
            self.regroup()

    # -- the current row --------------------------------------------------
    def highlight(self, current):
        """The selected look without a selection, as Devices' list does."""
        for e in self.entries:
            on = e is current
            if on:
                e.row.set_state_flags(Gtk.StateFlags.SELECTED, False)
            else:
                e.row.unset_state_flags(Gtk.StateFlags.SELECTED)
            e.row.update_state([Gtk.AccessibleState.SELECTED], [int(on)])
        self.current = current

    def _on_activate(self, _lb, row):
        entry = getattr(row, 'entry', None)
        if entry is not None:
            self.page.select(entry)

    def _on_key(self, _ctl, keyval, _code, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if self.page.collapsed:
            return False
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_KP_Up,
                      Gdk.KEY_KP_Down, Gdk.KEY_Home, Gdk.KEY_End):
            GLib.idle_add(self._follow_focus)
        return False

    def _follow_focus(self):
        row = self.listbox.get_focus_child()
        entry = getattr(row, 'entry', None)
        if entry is not None and entry is not self.current:
            self.page.select(entry, show=False)
        return False

    def _on_drop(self, _t, value, _x, _y):
        self.widget.remove_css_class('drop-ready')
        files = value.get_files() if value else []
        if not files:
            return False
        self.page.import_path(files[0].get_path())
        return True


# ================================================================ page ==

class EnhancePage:
    _POLL_SECONDS = 3       # only where livestate cannot listen

    def __init__(self, window):
        self.window = window
        self.volume_style = prefs.get('volume_style') or 'meter'
        self.tab = 'eq'
        self.items = []
        self.detail = None
        self._draft = None              # the detail of a new one, if open
        self._want = {'eq': None, 'mic': None}
        self._data = None
        self._live = None
        self._poll = None
        self._seq = 0
        self._applied = 0
        self._old = []

        self.list = EnhList(self)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType
                               .CROSSFADE, transition_duration=150,
                               vexpand=True, hhomogeneous=False)
        self.stack.connect('notify::transition-running', self._drop_old)
        self.stack.add_child(self._empty_page())

        self.split = Adw.NavigationSplitView(
            min_sidebar_width=250, max_sidebar_width=340,
            sidebar_width_fraction=0.32)
        self.split.set_sidebar(Adw.NavigationPage(
            title='Equalizers', tag='list', child=self.list.widget))
        self.split.set_content(Adw.NavigationPage(
            title='Equalizer', tag='detail', child=self.stack))
        self.split.connect('notify::collapsed', self._on_collapsed)

        bin_ = Adw.BreakpointBin(width_request=MIN_PAGE_WIDTH,
                                 height_request=200, child=self.split)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(
            f'max-width: {COLLAPSE_SP}sp'))
        bp.add_setter(self.split, 'collapsed', True)
        bin_.add_breakpoint(bp)
        self.widget = bin_
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)
        switch = getattr(window, 'adv_switch', None)
        if isinstance(switch, Gtk.Widget):
            switch.connect('notify::active', lambda *_a: self._on_advanced())

    def _empty_page(self):
        self._empty = Adw.StatusPage(icon_name='pwctl-eq-symbolic')
        box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self._empty_new = Gtk.Button()
        self._empty_new.add_css_class('pill')
        self._empty_new.add_css_class('suggested-action')
        self._empty_new.connect('clicked', lambda _b: self.new())
        self._empty_import = Gtk.Button(label='Import a file…')
        self._empty_import.add_css_class('pill')
        self._empty_import.connect('clicked', lambda _b: self.import_file())
        box.append(self._empty_new)
        box.append(self._empty_import)
        self._empty.set_child(box)
        self._paint_empty()
        return self._empty

    def _paint_empty(self):
        if self.tab == 'eq':
            self._empty.set_icon_name('pwctl-eq-symbolic')
            self._empty.set_title('No equalizers yet')
            self._empty.set_description(
                'An equalizer is an output of its own: whatever plays into '
                'it is shaped by its curve, then sent on to your speakers '
                'or headphones. Make one, or import an AutoEQ / APO file.')
            self._empty_new.set_label('New equalizer')
        else:
            self._empty.set_icon_name('pwctl-source-mic-symbolic')
            self._empty.set_title('No clean microphones yet')
            self._empty.set_description(
                'A clean microphone is your real one with echo and '
                'background noise taken out, for calls and streaming. '
                'Pick it as the input in the app that needs it.')
            self._empty_new.set_label('New clean microphone')
        self._empty_import.set_visible(self.tab == 'eq')

    # ------------------------------------------------------------ layout --
    @property
    def collapsed(self):
        return self.split.get_collapsed()

    def _on_collapsed(self, *_a):
        if self.detail is not None:
            self.detail.set_collapsed(self.collapsed)

    def show_list(self):
        self.split.set_show_content(False)

    def _on_advanced(self):
        if isinstance(self.detail, MicDetail):
            self.detail.set_advanced(bool(getattr(self.window, 'advanced',
                                                  False)))

    def _show(self, detail):
        old = self.stack.get_visible_child()
        child = detail.widget if detail is not None else self._empty
        if child.get_parent() is None:
            self.stack.add_child(child)
        if child is old:
            return
        self.stack.set_visible_child(child)
        if old is not None and old is not self._empty:
            self._old.append(old)
            if not self.stack.get_transition_running():
                self._drop_old()

    def _drop_old(self, *_a):
        if self.stack.get_transition_running():
            return
        for w in self._old:
            if w.get_parent() is self.stack and \
                    w is not self.stack.get_visible_child():
                self.stack.remove(w)
        self._old = []

    # -------------------------------------------------------- live state --
    def _on_map(self, *_a):
        if self._live is None:
            self._live = livestate.subscribe(self._on_graph)
        if self._live is not None:
            return                  # its first delivery arrives by itself
        self._collect()
        if self._poll is None:
            self._poll = GLib.timeout_add_seconds(self._POLL_SECONDS,
                                                  self._tick)

    def _on_unmap(self, *_a):
        if self._live is not None:
            livestate.unsubscribe(self._live)
            self._live = None
        if self._poll is not None:
            GLib.source_remove(self._poll)
            self._poll = None

    def _tick(self):
        self._collect()
        return True

    def _on_graph(self, dump):
        self._collect(dump)

    def _collect(self, dump=None):
        self._seq += 1
        seq = self._seq
        async_call(lambda: collect(dump if dump is not None
                                   else pw.pw_dump()),
                   lambda r, e: self._collected(r, e, seq))

    def _collected(self, data, error, seq):
        # in order: an older read never lands on a newer one
        if error or data is None or seq < self._applied:
            return
        self._applied = seq
        self._apply(data)

    def refresh(self):
        if self._live is not None:
            livestate.refresh()
        else:
            self._collect()

    def _apply(self, data):
        self._data = data
        self.items = data['items']
        mine = sorted((e for e in self.items if e.kind == self.tab),
                      key=lambda e: e.name.lower())
        draft = self._draft if self._draft is not None and \
            self._draft.kind == self.tab else None
        keys = ([DRAFT] if draft is not None else []) + [e.id for e in mine]
        if set(keys) != set(self.list.keys()):
            self.list.rebuild(self.tab, mine, draft)
        else:
            fresh = {e.id: e for e in mine}
            for entry in self.list.entries:
                if entry.enh is not None:
                    entry.enh = fresh[entry.key]
        d = self.detail
        gone = d is not None and not d.draft and (
            d.kind != self.tab or d.key not in keys)
        if gone:
            d.close()
            self.detail = None
        if self.detail is None:
            self._restore_selection()
        else:
            self.list.highlight(self.list.entry(self.detail.key))
            self.detail.paint(data)
        for entry in self.list.entries:
            entry.paint(data, self.detail)
        self.list.regroup()

    def _restore_selection(self):
        entries = self.list.ordered()
        if not entries:
            self.list.highlight(None)
            self._paint_empty()
            self._show(None)
            return
        want = self._want.get(self.tab)
        pick = self.list.entry(want) if want else None
        if pick is None and self._draft is not None:
            pick = self.list.entry(DRAFT)
        if pick is None:
            data = self._data or {}
            defaults = {n.name for n in data.get('nodes', [])
                        if n.is_default}
            pick = next((e for e in entries if e.enh is not None
                         and e.enh.node_name in defaults), entries[0])
        self.select(pick, show=False, ask=False)

    def detail_changed(self, detail):
        """The detail's edit, name or settings changed: its row follows."""
        if detail is not self.detail:
            return
        entry = self.list.entry(detail.key)
        if entry is not None:
            entry.paint(self._data, detail)

    # --------------------------------------------------------- selection --
    def select(self, entry, show=True, ask=True):
        if entry is None:
            return
        d = self.detail
        if d is not None and d.key == entry.key and d.kind == self.tab:
            self.list.highlight(entry)
            if show:
                self.split.set_show_content(True)
            return

        def go():
            self._leave()
            self._open(entry)
            if show:
                self.split.set_show_content(True)
        if ask:
            self.confirm_leave(go)
        else:
            go()

    def _open(self, entry):
        self.list.highlight(entry)
        if entry.key == DRAFT:
            detail = self._draft
        else:
            self._want[self.tab] = entry.key
            enh = entry.enh
            # its own copy: the detail changes it only when it saves
            enh = enhance.Enhancement(**{**asdict(enh),
                                         'params': dict(enh.params)})
            detail = EqDetail(self, enh) if entry.kind == 'eq' \
                else MicDetail(self, enh)
        self.detail = detail
        if self._data is not None:
            detail.paint(self._data)
            entry.paint(self._data, detail)
        self._show(detail)

    def _leave(self):
        """Drop the current detail (already asked): put back what plays,
        and forget a new one."""
        d = self.detail
        if d is None:
            return
        self.detail = None
        if d.draft:
            if self._draft is d:
                self._draft = None
                entry = self.list.entry(DRAFT)
                if entry is not None:
                    self.list.entries.remove(entry)
                    self.list.listbox.remove(entry.row)
        else:
            d.discard()

    @property
    def has_unsaved(self):
        return self.detail is not None and self.detail.dirty

    def confirm_leave(self, then, on_cancel=None):
        """Run `then` once the current detail may go: at once when nothing
        is unsaved, else after asking."""
        d = self.detail
        if d is None or not d.dirty:
            then()
            return
        if d.draft:
            dlg = Adw.AlertDialog(
                heading=f'Discard the new {NOUN[d.kind]}?',
                body='It has not been created yet.')
            dlg.add_response('edit', 'Keep editing')
            dlg.add_response('discard', 'Discard')
            dlg.set_response_appearance('discard',
                                        Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response('edit')
        else:
            body = ('Your changes are playing, but they are not saved. '
                    'Discarding puts the saved curve back.'
                    if d.kind == 'eq' else 'Your changes are not saved.')
            dlg = Adw.AlertDialog(heading=f'Save changes to “{d.title}”?',
                                  body=body)
            dlg.add_response('discard', 'Discard')
            dlg.add_response('edit', 'Keep editing')
            dlg.add_response('save', 'Save')
            dlg.set_response_appearance('discard',
                                        Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_response_appearance('save',
                                        Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response('save')
        dlg.set_close_response('edit')

        def resp(_d, r):
            if r == 'save':
                d.save(then=then)
            elif r == 'discard':
                then()
            elif on_cancel is not None:
                on_cancel()
        dlg.connect('response', resp)
        dlg.present(self.window)

    def switch_tab(self, tab):
        def go():
            self._leave()
            self.tab = tab
            self.list.set_tab(tab)
            self._paint_empty()
            self.list.rebuild(tab, [], None)
            if self._data is not None:
                self._apply(self._data)
            self.refresh()
        self.confirm_leave(go, on_cancel=lambda: self.list.set_tab(self.tab))

    # ------------------------------------------------------------- new --
    def new(self, curve=None, name='', notes=None, origin=None):
        kind = self.tab

        def go():
            self._leave()
            if kind == 'eq':
                self._draft = EqDetail(self, None, curve=curve, name=name,
                                       notes=notes, origin=origin)
            else:
                self._draft = MicDetail(self, None)
            entry = _Entry(self, kind)
            self.list.entries.insert(0, entry)
            self.list.listbox.append(entry.row)
            self._open(entry)
            self.split.set_show_content(True)
            if kind == 'eq' and not name:
                GLib.idle_add(lambda: (self._draft is not None and
                                       self._draft.name_row.grab_focus(),
                                       False)[1])
        self.confirm_leave(go)

    def import_file(self):
        pick_file(self.window, 'Import an AutoEQ / APO file',
                  self.import_path,
                  filters=[('Parametric EQ', ['*.txt']),
                           ('All files', ['*'])])

    def import_path(self, path):
        if not path:
            return
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                parsed = E.parse_text(f.read())
        except OSError:
            self.window.toast('Could not read that file')
            return
        if not parsed.curve.bands:
            self.window.toast('No parametric EQ filters found in that file')
            return
        base = GLib.path_get_basename(path)
        if self.tab != 'eq':
            self.tab = 'eq'
            self.list.set_tab('eq')
            self._paint_empty()
            self.list.rebuild('eq', [], None)
        self.new(curve=parsed.curve, name=base.rsplit('.', 1)[0],
                 notes=parsed.notes, origin={'kind': 'file', 'name': base})

    def discard_draft(self):
        self._leave()
        self._restore_selection()

    def created(self, enh):
        """A new one was created (its unit asked to start)."""
        self._want[enh.kind] = enh.id
        if self.detail is not None and self.detail.draft:
            self._leave()
        self.refresh()

    # --------------------------------------------------------- actions --
    def _own(self, enh):
        """The detail's copy of `enh` when it is the one shown."""
        d = self.detail
        if d is not None and d.enh is not None and d.enh.id == enh.id:
            return d.enh
        return enh

    def set_running(self, enh, on, again=False):
        enh = self._own(enh)
        if on == enh.enabled and not again:
            return

        def done(result, error):
            ok, err = result if result else (False, str(error or ''))
            if ok:
                self.window.toast(f'{enh.name} '
                                  f'{"started" if on else "stopped"}')
            else:
                self.window.toast(f'Failed: {err}' if err else 'Failed')
            d = self.detail
            if d is not None and d.enh is enh and d.kind == 'eq':
                d.session.rebind()
            self.refresh()
        async_call(lambda: enhance.set_enabled(enh, on), done)

    def make_default(self, detail):
        enh = detail.enh
        if detail.kind == 'mic':
            node = detail._node
            if node is None:
                return
            async_call(lambda: pw.set_default(node.id), lambda ok, e: (
                self.window.toast(f'“{enh.name}” is now your input' if ok
                                  else 'Could not set the default input'),
                self.refresh()))
            return
        before = enh.params.get('target', '')

        def done(result, error):
            ok, msg = result if result else (False, str(error or ''))
            if ok:
                where = f' → {msg}' if msg else ''
                self.window.toast(f'“{enh.name}” is now your output{where}. '
                                  'Everything is equalized through it.')
            else:
                self.window.toast(msg or 'Could not set as default output')
            after = enh.params.get('target', '')
            if after != before:
                # it pinned the output to the device it replaced
                detail.saved_settings['target'] = after
                if detail.settings['target'] == before:
                    detail.settings['target'] = after
                detail._targets_sig = None
                detail.session.rebind()
            self.refresh()
        async_call(lambda: enhance.make_default_output(enh), done)

    def delete(self, detail):
        enh = detail.enh

        def go():
            entries = self.list.ordered()
            keys = [e.key for e in entries]
            if enh.id in keys:
                i = keys.index(enh.id)
                rest = keys[:i] + keys[i + 1:]
                self._want[detail.kind] = rest[min(i, len(rest) - 1)] \
                    if rest else None
            if self.detail is detail:
                self.detail = None
            detail.close()
            async_call(lambda: enhance.delete(enh), lambda *_a: (
                self.window.toast(f'Deleted “{enh.name}”'),
                self.refresh()))
        confirm(self.window, f'Delete “{enh.name}”?',
                'It is stopped and its settings removed.', 'Delete', go)

    def duplicate(self, detail, params):
        enh = detail.enh
        source = enhance.Enhancement(**{**asdict(enh), 'params': params})
        with_changes = detail.dirty

        def done(copy, error):
            if error or copy is None:
                self.window.toast(f'Failed: {error}')
                return
            self.window.toast(f'Made “{copy.name}”'
                              + (', with your changes' if with_changes
                                 else ''))
            # the changes live in the copy now: the original goes back to
            # its saved curve without asking
            self._want[enh.kind] = copy.id
            if self.detail is detail:
                self._leave()
            self.refresh()
        async_call(lambda: enhance.duplicate(source), done)

    def rename(self, detail, name):
        enh = detail.enh
        old = enh.name
        enh.name = name

        def done(result, error):
            ok, err = result if result else (False, str(error or ''))
            if not ok:
                enh.name = old
                self.window.toast(f'Failed: {err}' if err else 'Failed')
            else:
                self.window.toast(f'Renamed to “{name}”')
            detail._paint_top()
            if detail.kind == 'eq':
                detail.session.rebind()
            self.refresh()
        async_call(lambda: enhance.apply(enh), done)

    def route(self, enh, node, s):
        def done(ok, _e):
            self.window.toast(f'{s.name} → {enh.name}' if ok
                              else 'Could not send that app here')
            self.refresh()
        async_call(lambda: pw.move_stream(s.id, node.serial), done)

    def unroute(self, enh, s):
        def work():
            dest = enhance.eq_output_node(enh)
            if dest is None:
                return None
            return dest.description if pw.move_stream(s.id, dest.serial) \
                else None

        def done(where, _e):
            self.window.toast(f'{s.name} → {where}' if where
                              else 'Could not move that app')
            self.refresh()
        async_call(work, done)

    def keep_app(self, s, node_name, keep):
        def work():
            if keep:
                return bool(rules.bind_stream_to_node(s, node_name))
            return rules.unbind_stream(s)

        def done(ok, error):
            if error:
                self.window.toast(f'Failed: {error}')
            elif keep:
                self.window.toast(f'{s.name} will come back here' if ok
                                  else 'Could not identify that app')
            else:
                self.window.toast(f'{s.name} is no longer kept here')
            flag = getattr(self.window, 'flag_restart', None)
            if flag is not None:
                flag('pulse')
            self.refresh()
        async_call(work, done)
