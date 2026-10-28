"""The equalizer editor (DR-016): the curve you drag, and hear.

Layout is the "Adaptive" direction of the user's design
(~/Downloads/pwctl-eq-designs.html): a Response card (graph or list, bands
as chips, Compare and the curve's actions) and one inspector for the
selected band.  Since the Equalizer page became a list beside the selected
equalizer (`enhance_page`, the user's "Split View" template), the editor
is a widget on that page rather than a dialog: the page owns the session,
Save and Revert, and the device's own settings (name, output, layout).

The model is `backend.eqsession.EqSession`: edits go there, and a running
equalizer plays them as they are made.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from ..backend import enhance, eqpresets, eqsession  # noqa: E402
from ..backend import eqcurve as E  # noqa: E402
from ..backend.surround import LAYOUTS as _SPEAKER_LAYOUTS  # noqa: E402
from .eq_graph import EqGraph, round_freq  # noqa: E402
from .widgets import async_call, esc, icon_button, pick_file  # noqa: E402

LAYOUTS = [('mono', 'Mono 1.0', ['MONO'])] + list(_SPEAKER_LAYOUTS)
STEREO = next(i for i, l in enumerate(LAYOUTS) if l[0] == 'stereo')
MAIN_TYPES = ('PK', 'LSC', 'HSC')
CUT_TYPES = ('HPQ', 'LPQ', 'NO')
GROUP_IDLE_MS = 600            # a slider or a scroll is one undo step


class _Field:
    """A labelled value: a spin button you can type in, a slider under it."""

    def __init__(self, title, unit, lower, upper, digits, step, log=False):
        self.lower, self.upper, self.log = lower, upper, log
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=2, hexpand=True)
        self.widget.add_css_class('eq-field')
        top = Gtk.Box(spacing=6)
        self.title = Gtk.Label(label=title, xalign=0, hexpand=True)
        self.title.add_css_class('eq-field-label')
        self.sub = Gtk.Label(xalign=1)
        self.sub.add_css_class('eq-field-sub')
        top.append(self.title)
        top.append(self.sub)
        row = Gtk.Box(spacing=6)
        self.spin = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(lower=lower, upper=upper,
                                      step_increment=step,
                                      page_increment=step * 10),
            digits=digits, numeric=True, valign=Gtk.Align.CENTER,
            width_chars=7, climb_rate=1)
        self.spin.add_css_class('eq-value')
        self.unit = Gtk.Label(label=unit, xalign=0, hexpand=True)
        self.unit.add_css_class('eq-field-unit')
        row.append(self.spin)
        row.append(self.unit)
        if log:
            self.scale = Gtk.Scale(adjustment=Gtk.Adjustment(
                lower=0, upper=1000, step_increment=5, page_increment=50),
                draw_value=False, hexpand=True)
        else:
            self.scale = Gtk.Scale(adjustment=self.spin.get_adjustment(),
                                   draw_value=False, hexpand=True)
        self.scale.add_css_class('eq-scale')
        self.widget.append(top)
        self.widget.append(row)
        self.widget.append(self.scale)
        self.on_change = None
        self._updating = False
        self.spin.connect('value-changed', self._spun)
        if log:
            self.scale.connect('value-changed', self._slid)

    def _to_pos(self, v):
        import math
        v = min(max(v, self.lower), self.upper)
        return 1000 * math.log(v / self.lower) / math.log(self.upper /
                                                          self.lower)

    def _from_pos(self, p):
        return self.lower * (self.upper / self.lower) ** (p / 1000)

    def set(self, value):
        self._updating = True
        self.spin.set_value(value)
        if self.log:
            self.scale.set_value(self._to_pos(value))
        self._updating = False

    def _spun(self, spin):
        if self._updating:
            return
        if self.log:
            self._updating = True
            self.scale.set_value(self._to_pos(spin.get_value()))
            self._updating = False
        if self.on_change:
            self.on_change(spin.get_value())

    def _slid(self, scale):
        if self._updating:
            return
        v = self._from_pos(scale.get_value())
        v = round_freq(v) if self.unit.get_label() == 'Hz' else round(v, 2)
        self._updating = True
        self.spin.set_value(v)
        self._updating = False
        if self.on_change:
            self.on_change(v)




class EqEditor(Gtk.Box):
    """The curve and its bands, as one widget the Equalizer page embeds.

    The owner makes the session (and so decides what an equalizer is being
    edited, when it is saved and when the edit is dropped), installs
    `add_breakpoints()` on the bin around it, and listens:

    - `on_changed()` after every render: the curve, the selection or what
      Compare plays changed, so the owner repaints what it shows of them;
    - `on_loaded(kind, name)` when a whole curve was loaded ('file',
      'preset', 'paste'), so a new equalizer can say where it came from;
    - `on_save()` for Ctrl+S.
    """

    def __init__(self, window, session, others=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add_css_class('eq-editor')
        self.window = window
        self.session = session
        self._others = others or (lambda: [])
        self._updating = False
        self._group_timer = 0
        self._chip_widgets = []
        self._list_rows = []
        self._add_chip = None
        self.on_changed = None
        self.on_loaded = None
        self.on_save = None
        self.sel = None

        self.append(self._response_card())
        self.notes = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType
                                  .SLIDE_DOWN, visible=False)
        # hidden once closed, so the box's spacing does not stay behind
        self.notes.connect('notify::child-revealed', lambda r, _p:
                           r.set_visible(r.get_reveal_child()
                                         or r.get_child_revealed()))
        self.append(self.notes)
        self.inspector = self._inspector_card()
        self._actions()
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect('drop', self._on_drop)
        self.add_controller(drop)
        self.connect('destroy', lambda *_a: self._stop_group_timer())

    def add_breakpoints(self, narrow, narrowest):
        """Setters for the owner's two breakpoints (only one applies at a
        time, so the narrower one repeats the wider one's)."""
        for bp in (narrow, narrowest):
            bp.add_setter(self.graph.area, 'content-height', 170)
            bp.add_setter(self.hint, 'visible', False)
        narrowest.add_setter(self.response_title, 'visible', False)
        narrowest.add_setter(self._type_toggles['LSC'], 'label', 'Low')
        narrowest.add_setter(self._type_toggles['HSC'], 'label', 'High')
        narrowest.add_setter(self._listen_toggles[eqsession.EDIT], 'label',
                             'EQ')
        narrowest.add_setter(self._listen_toggles[eqsession.BYPASS],
                             'label', 'Off')

    # ============================================================ build ==
    def _card(self, title):
        # (the title label is returned so a narrow layout can hide it)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class('eq-card')
        head = Gtk.Box(spacing=8)
        head.add_css_class('eq-card-head')
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class('eq-card-title')
        head.append(label)
        card.append(head)
        return card, head, label

    def _response_card(self):
        card, head, self.response_title = self._card('Response')
        head.append(Gtk.Box(hexpand=True))

        self.views = Adw.ViewStack(vhomogeneous=False)
        self.graph = EqGraph()
        self.graph.on_select = self._select
        self.graph.on_edit = self._graph_edit
        self.graph.on_edit_end = self._end_group
        self.graph.on_add = lambda f, g: self._add_band(f, g)
        self.graph.on_menu = self._band_menu
        self.graph.on_remove = self._remove
        self.graph.on_toggle = self._toggle
        self.views.add_titled(self.graph, 'graph', 'Graph')
        self.band_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.band_list.add_css_class('boxed-list')
        self.views.add_titled(self.band_list, 'list', 'List')
        self.undo_btn = icon_button('edit-undo-symbolic', 'Undo (Ctrl+Z)',
                                    lambda *_: self._undo())
        self.redo_btn = icon_button('edit-redo-symbolic',
                                    'Redo (Ctrl+Shift+Z)',
                                    lambda *_: self._redo())
        more = Gtk.MenuButton(icon_name='view-more-symbolic',
                              tooltip_text='Import, export and more',
                              menu_model=self._more_menu(),
                              valign=Gtk.Align.CENTER)
        more.add_css_class('flat')
        for w in (self.undo_btn, self.redo_btn, more):
            head.append(w)
        switcher = Adw.InlineViewSwitcher(stack=self.views,
                                          valign=Gtk.Align.CENTER,
                                          margin_start=4)
        head.append(switcher)
        # the list is its own band picker: the chips would say it twice
        self.views.connect('notify::visible-child-name', lambda *_:
                           self.chips.set_visible(
                               self.views.get_visible_child_name()
                               == 'graph'))
        card.append(self.views)

        self.chips = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self.chips.add_css_class('eq-chips')
        card.append(self.chips)

        bar = Gtk.Box(spacing=6)
        bar.add_css_class('eq-actions')
        left = Adw.WrapBox(child_spacing=6, line_spacing=6, hexpand=True)
        # Compare: what the running equalizer plays.  Where the editor's
        # Bypass button was; the page's Listen section holds its loudness
        # switch.
        self.listen = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.listen.add_css_class('eq-listen')
        self._listen_toggles = {}
        for mode, label, tip in (
                (eqsession.EDIT, 'Equalized', 'Hear the curve as drawn'),
                (eqsession.SAVED, 'Saved', 'Hear the saved curve, to '
                 'compare your changes with it'),
                (eqsession.BYPASS, 'Bypassed', 'Hear it without the '
                 'equalizer, while it keeps running (B)')):
            toggle = Adw.Toggle(name=mode, label=label, tooltip=tip)
            self.listen.add(toggle)
            self._listen_toggles[mode] = toggle
        self.listen.set_active_name(eqsession.EDIT)
        self.listen.connect('notify::active-name', self._listen_picked)
        left.append(self.listen)
        self.presets_btn = Gtk.MenuButton(
            child=Adw.ButtonContent(icon_name='pwctl-eq-symbolic',
                                    label='Presets', can_shrink=True),
            tooltip_text='Load a ready-made curve, one of yours, or the '
                         'curve of another equalizer')
        self.presets_btn.set_create_popup_func(self._fill_presets)
        left.append(self.presets_btn)
        for label, tip, fn in (
                ('Auto gain', 'Lower the preamp just enough that nothing '
                              'clips', self._auto_gain),
                ('Flat', 'Every band to 0 dB; the bands stay to drag',
                 self._flat)):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect('clicked', lambda *_, f=fn: f())
            left.append(b)
        bar.append(left)
        card.append(bar)

        self.hint = Gtk.Label(
            label='Drag a point to move it, scroll on it to change its '
                  'width, double-click the graph to add a band. Shift for '
                  'fine steps, B to compare with it bypassed.', xalign=0,
            wrap=True)
        self.hint.add_css_class('eq-hint')
        card.append(self.hint)
        return card

    def _inspector_card(self):
        card, head, self.band_title = self._card('Band')
        head.append(Gtk.Box(hexpand=True))
        self.on_switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                                    tooltip_text='Band on (Space on the '
                                                 'graph)')
        self.on_switch.connect('notify::active', self._switched)
        head.append(self.on_switch)
        self.remove_btn = icon_button('user-trash-symbolic',
                                      'Remove this band (Delete)',
                                      lambda *_: self._remove(self.sel))
        head.append(self.remove_btn)

        types = Gtk.Box(spacing=6)
        self.types = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.types.add_css_class('eq-types')
        self._type_toggles = {}
        for code in MAIN_TYPES:
            toggle = Adw.Toggle(name=code, label=E.TYPES[code].label,
                                tooltip=E.TYPES[code].long)
            self.types.add(toggle)
            self._type_toggles[code] = toggle
        self.types.connect('notify::active-name', self._type_picked)
        types.append(self.types)
        self.cut_btn = Gtk.MenuButton(
            label='Cut', always_show_arrow=True,
            tooltip_text='Low cut, high cut and notch filters',
            menu_model=self._cut_menu(), valign=Gtk.Align.CENTER)
        self.cut_btn.add_css_class('eq-cut')
        types.append(self.cut_btn)
        self.type_row = types
        card.append(types)

        self.fields = Gtk.FlowBox(homogeneous=True, min_children_per_line=1,
                                  max_children_per_line=2,
                                  selection_mode=Gtk.SelectionMode.NONE,
                                  column_spacing=18, row_spacing=10)
        self.f_freq = _Field('Frequency', 'Hz', E.FREQ_MIN, E.FREQ_MAX, 0,
                             1, log=True)
        self.f_gain = _Field('Gain', 'dB', E.GAIN_MIN, E.GAIN_MAX, 1, 0.1)
        self.f_q = _Field('Q · width', '', E.Q_MIN, E.Q_MAX, 2, 0.01,
                          log=True)
        self.f_pre = _Field('Preamp · all bands', 'dB', E.PREAMP_MIN,
                            E.PREAMP_MAX, 1, 0.1)
        self.f_gain.scale.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.f_pre.scale.add_mark(0, Gtk.PositionType.BOTTOM, None)
        for key, f in (('freq', self.f_freq), ('gain', self.f_gain),
                       ('q', self.f_q)):
            f.on_change = lambda v, k=key: self._field_edit(k, v)
        self.f_pre.on_change = self._preamp_edit
        for f in (self.f_freq, self.f_gain, self.f_q, self.f_pre):
            child = Gtk.FlowBoxChild(child=f.widget, focusable=False)
            self.fields.append(child)
        card.append(self.fields)
        self.empty = Gtk.Label(
            label='Pick a point on the curve, or a band below it, to '
                  'change it. The preamp lowers every band at once.',
            xalign=0, wrap=True)
        self.empty.add_css_class('eq-hint')
        card.append(self.empty)
        return card

    # =========================================================== menus ==
    def _actions(self):
        group = Gio.SimpleActionGroup()
        for name, fn in (('import', self._import_file),
                         ('paste', self._paste),
                         ('copy', self._copy),
                         ('export', self._export),
                         ('save-preset', self._save_preset),
                         ('sort', self._sort),
                         ('revert', self.revert),
                         ('toggle', lambda: self._toggle(self.sel)),
                         ('remove', lambda: self._remove(self.sel))):
            a = Gio.SimpleAction.new(name, None)
            a.connect('activate', lambda *_a, f=fn: f())
            group.add_action(a)
        self._type_action = Gio.SimpleAction.new_stateful(
            'type', GLib.VariantType.new('s'), GLib.Variant('s', 'PK'))
        self._type_action.connect('change-state', self._type_state)
        group.add_action(self._type_action)
        # on the editor and on the inspector, which the page places apart
        self.insert_action_group('eq', group)
        self.inspector.insert_action_group('eq', group)
        self._group = group

    def _more_menu(self):
        m = Gio.Menu()
        files = Gio.Menu()
        files.append('Import a file…', 'eq.import')
        files.append('Paste from the clipboard', 'eq.paste')
        files.append('Copy as text', 'eq.copy')
        files.append('Export a file…', 'eq.export')
        m.append_section(None, files)
        tidy = Gio.Menu()
        tidy.append('Save as a preset…', 'eq.save-preset')
        tidy.append('Sort bands by frequency', 'eq.sort')
        tidy.append('Revert to saved', 'eq.revert')
        m.append_section(None, tidy)
        return m

    def _cut_menu(self):
        m = Gio.Menu()
        for code in CUT_TYPES:
            m.append(E.TYPES[code].long, f"eq.type::{code}")
        return m

    def _band_menu(self, i, x, y):
        m = Gio.Menu()
        types = Gio.Menu()
        for code in MAIN_TYPES + CUT_TYPES:
            types.append(E.TYPES[code].label, f"eq.type::{code}")
        m.append_section(None, types)
        band = self.session.curve.bands[i]
        acts = Gio.Menu()
        acts.append('Turn off' if band.on else 'Turn on', 'eq.toggle')
        acts.append('Remove', 'eq.remove')
        m.append_section(None, acts)
        pop = Gtk.PopoverMenu.new_from_model(m)
        pop.set_parent(self.graph.area)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect('closed', lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _fill_presets(self, button):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6, margin_start=6,
                      margin_end=6)
        pop = Gtk.Popover(child=Gtk.ScrolledWindow(
            child=box, hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True, max_content_height=460,
            min_content_width=260))
        pop.add_css_class('eq-presets')

        def item(title, sub, curve, kind, extra=None):
            row = Gtk.Box(spacing=4)
            b = Gtk.Button(hexpand=True)
            b.add_css_class('flat')
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            t = Gtk.Label(label=title, xalign=0,
                          ellipsize=Pango.EllipsizeMode.END)
            col.append(t)
            if sub:
                s = Gtk.Label(label=sub, xalign=0,
                              ellipsize=Pango.EllipsizeMode.END)
                s.add_css_class('eq-preset-sub')
                col.append(s)
            b.set_child(col)
            b.connect('clicked', lambda *_: (
                pop.popdown(), self._load_curve(curve, title),
                self._loaded(kind, title)))
            row.append(b)
            if extra:
                row.append(extra)
            box.append(row)

        def heading(text):
            lab = Gtk.Label(label=text.upper(), xalign=0, margin_start=8,
                            margin_top=8)
            lab.add_css_class('micro-heading')
            box.append(lab)

        heading('Ready-made')
        for p in eqpresets.BUILTIN:
            item(p.name, p.blurb, p.curve, 'preset')
        mine = eqpresets.list_user()
        if mine:
            heading('Yours')
            for p in mine:
                rm = icon_button('user-trash-symbolic', 'Delete this preset',
                                 lambda *_, pp=p: (pop.popdown(),
                                                   self._delete_preset(pp)))
                item(p.name, f'{len(p.curve.bands)} bands', p.curve,
                     'preset', rm)
        others = list(self._others())
        if others:
            heading('From your equalizers')
            for e in others:
                c = enhance.curve_of(e).clamped()
                item(e.name, f'{len(c.bands)} bands', c, 'copy')
        save = Gtk.Button(label='Save this curve as a preset…',
                          margin_top=6)
        save.connect('clicked', lambda *_: (pop.popdown(),
                                            self._save_preset()))
        box.append(save)
        button.set_popover(pop)

    def shortcuts_for(self, widget):
        """Undo, redo, save, paste and B on `widget` (the page's pane), so
        they work wherever focus is in it, not only on the graph."""
        ctl = Gtk.ShortcutController()
        for accel, fn in (('<Control>z', self._undo),
                          ('<Control><Shift>z', self._redo),
                          ('<Control>y', self._redo),
                          ('<Control>s', lambda: self.on_save
                           and self.on_save()),
                          ('<Control>v', self._paste),
                          ('b', self._toggle_bypass)):
            ctl.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(accel),
                Gtk.CallbackAction.new(
                    lambda w, a, f=fn: (f(), True)[1])))
        widget.add_controller(ctl)

    # ========================================================== render ==
    def render(self):
        """Push the session's curve into every view."""
        self._updating = True
        s = self.session
        curve = s.curve
        n = len(curve.bands)
        if self.sel is not None and self.sel >= n:
            self.sel = n - 1 if n else None
        self.graph.set_curve(curve, self.sel, s.bypassed,
                             ghost=s.saved.clamped() if s.dirty else None,
                             hearing_saved=s.listen == eqsession.SAVED)
        self._render_chips(curve)
        self._render_list(curve)
        self._render_inspector(curve)
        self._render_listen()
        self.undo_btn.set_sensitive(s.can_undo)
        self.redo_btn.set_sensitive(s.can_redo)
        self._updating = False
        if self.on_changed:
            self.on_changed()

    def _render_listen(self):
        s = self.session
        self.listen.set_visible(s.enh is not None)
        self.listen.set_sensitive(s.can_hear)
        self.listen.set_tooltip_text(
            None if s.can_hear else 'Switch the equalizer on to compare')
        saved_ok = s.dirty
        self._listen_toggles[eqsession.SAVED].set_enabled(saved_ok)
        mode = s.listen
        if mode == eqsession.SAVED and not saved_ok:
            mode = eqsession.EDIT
        if self.listen.get_active_name() != mode:
            self.listen.set_active_name(mode)

    def _render_chips(self, curve):
        n = len(curve.bands)
        if len(self._chip_widgets) != n or self._add_chip is None:
            for w in self._chip_widgets:
                self.chips.remove(w)
            if self._add_chip is not None:
                self.chips.remove(self._add_chip)
            self._chip_widgets = []
            for i in range(n):
                b = Gtk.Button()
                b.add_css_class('eq-chip')
                box = Gtk.Box(spacing=8)
                dot = Gtk.Box(valign=Gtk.Align.CENTER)
                dot.add_css_class('eq-chip-dot')
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                f = Gtk.Label(xalign=0)
                f.add_css_class('eq-chip-freq')
                s = Gtk.Label(xalign=0)
                s.add_css_class('eq-chip-sub')
                col.append(f)
                col.append(s)
                box.append(dot)
                box.append(col)
                b.set_child(box)
                b._labels = (f, s)
                b.connect('clicked', lambda *_, k=i: self._select(k))
                self.chips.append(b)
                self._chip_widgets.append(b)
            self._add_chip = Gtk.Button(
                child=Adw.ButtonContent(icon_name='list-add-symbolic',
                                        label='Add band'),
                tooltip_text='Add a band (or double-click the graph)')
            self._add_chip.add_css_class('eq-chip')
            self._add_chip.add_css_class('add')
            self._add_chip.connect('clicked', lambda *_: self._add_band())
            self.chips.append(self._add_chip)
        for i, (w, b) in enumerate(zip(self._chip_widgets, curve.bands)):
            f, s = w._labels
            f.set_label(E.format_hz(b.freq))
            s.set_label(b.kind.label + (f' · {b.gain:+.1f} dB'
                                        if b.kind.has_gain else ''))
            for cls, on in (('selected', i == self.sel), ('off', not b.on)):
                (w.add_css_class if on else w.remove_css_class)(cls)
            w.set_tooltip_text(b.describe())
        self._add_chip.set_sensitive(n < E.MAX_BANDS)

    def _render_list(self, curve):
        n = len(curve.bands)
        if len(self._list_rows) != n:
            self.band_list.remove_all()
            self._list_rows = []
            for i in range(n):
                row = Adw.ActionRow(activatable=True, title_lines=1,
                                    subtitle_lines=1)
                sw = Gtk.Switch(valign=Gtk.Align.CENTER)
                sw.connect('notify::active',
                           lambda s, _p, k=i: self._list_switch(k, s))
                row.add_prefix(sw)
                row.add_suffix(icon_button('user-trash-symbolic',
                                           'Remove this band',
                                           lambda *_, k=i: self._remove(k)))
                row.connect('activated', lambda *_, k=i: self._select(k))
                row._switch = sw
                self.band_list.append(row)
                self._list_rows.append(row)
            if not n:
                empty = Adw.ActionRow(title='No bands yet')
                empty.set_sensitive(False)
                self.band_list.append(empty)
        for i, (row, b) in enumerate(zip(self._list_rows, curve.bands)):
            row.set_title(esc(f'{i + 1}. {b.kind.label} · '
                              f'{E.format_hz(b.freq)}'))
            bits = [f'{b.gain:+.1f} dB'] if b.kind.has_gain else []
            bits.append(f'Q {b.q:.2f}')
            row.set_subtitle(' · '.join(bits))
            row._switch.set_active(b.on)
            (row.add_css_class if i == self.sel else
             row.remove_css_class)('eq-row-selected')

    def _render_inspector(self, curve):
        has = self.sel is not None and curve.bands
        for w in (self.type_row, self.on_switch, self.remove_btn):
            w.set_visible(bool(has))
        self.empty.set_visible(not has)
        if not curve.bands:
            self.empty.set_label('No bands yet. Add one above, or load a '
                                 'preset.')
        else:
            self.empty.set_label('Pick a point on the curve, or a band '
                                 'above, to change it. The preamp lowers '
                                 'every band at once.')
        for f in (self.f_freq, self.f_gain, self.f_q):
            f.widget.get_parent().set_visible(bool(has))
        self.f_pre.set(curve.preamp)
        self.f_pre.sub.set_label('')
        if not has:
            self.band_title.set_label('Bands')
            return
        b = curve.bands[self.sel]
        self.band_title.set_label(f'Band {self.sel + 1}')
        self.on_switch.set_active(b.on)
        if b.type in MAIN_TYPES:
            self.types.set_active_name(b.type)
            self.cut_btn.remove_css_class('active')
            self.cut_btn.set_label('Cut')
        else:
            self.types.set_active(Gtk.INVALID_LIST_POSITION)
            self.cut_btn.add_css_class('active')
            self.cut_btn.set_label(b.kind.label)
        self._type_action.set_state(GLib.Variant('s', b.type))
        self.f_freq.set(b.freq)
        self.f_gain.set(b.gain if b.kind.has_gain else 0.0)
        self.f_gain.widget.set_sensitive(b.kind.has_gain)
        self.f_gain.sub.set_label('' if b.kind.has_gain
                                  else 'none for this type')
        self.f_q.set(b.q)
        self.f_q.sub.set_label(f'≈ {E.q_to_octaves(b.q):.2f} oct'
                               if b.type in ('PK', 'NO', 'BP') else '')

    # =========================================================== edits ==
    def _edit(self, curve, group=None, select=None):
        if select is not None:
            self.sel = select
        self.session.set(curve, group)
        if group is not None:
            self._arm_group_end()
        self.render()

    def _arm_group_end(self):
        self._stop_group_timer()

        def end():
            self._group_timer = 0
            self.session.end_group()
            return False
        self._group_timer = GLib.timeout_add(GROUP_IDLE_MS, end)

    def _stop_group_timer(self):
        if self._group_timer:
            GLib.source_remove(self._group_timer)
            self._group_timer = 0

    def _end_group(self):
        self._stop_group_timer()
        self.session.end_group()

    def _select(self, i):
        if i is not None and not (0 <= i < len(self.session.curve.bands)):
            return
        self.sel = i
        self.render()

    def _graph_edit(self, i, changes, group):
        self._edit(self.session.curve.with_band(i, **changes), group, i)

    def _field_edit(self, key, value):
        if self._updating or self.sel is None:
            return
        self._edit(self.session.curve.with_band(self.sel, **{key: value}),
                   f'{key}-{self.sel}')

    def _preamp_edit(self, value):
        if self._updating:
            return
        self._edit(self.session.curve.with_preamp(round(value, 1)),
                   'preamp')

    def _switched(self, sw, _p):
        if self._updating or self.sel is None:
            return
        self._edit(self.session.curve.with_band(self.sel,
                                                on=sw.get_active()))

    def _list_switch(self, i, sw):
        if self._updating:
            return
        self._edit(self.session.curve.with_band(i, on=sw.get_active()))

    def _type_picked(self, group, _p):
        if self._updating or self.sel is None:
            return
        code = group.get_active_name()
        if code:
            self._set_type(code)

    def _type_state(self, action, value):
        if self._updating:
            return
        self._set_type(value.get_string())

    def _set_type(self, code):
        if self.sel is None or code not in E.TYPES:
            return
        b = self.session.curve.bands[self.sel]
        if b.type == code:
            return
        changes = {'type': code}
        if code in ('LSC', 'HSC', 'HPQ', 'LPQ') and b.type == 'PK' and \
                abs(b.q - 1.0) < 1e-9:
            changes['q'] = 0.71          # the usual slope for a shelf/cut
        self._edit(self.session.curve.with_band(self.sel, **changes))

    def _toggle(self, i):
        if i is None or i >= len(self.session.curve.bands):
            return
        b = self.session.curve.bands[i]
        self._edit(self.session.curve.with_band(i, on=not b.on), select=i)

    def _remove(self, i):
        if i is None or i >= len(self.session.curve.bands):
            return
        self._edit(self.session.curve.removed(i),
                   select=min(i, len(self.session.curve.bands) - 2)
                   if len(self.session.curve.bands) > 1 else None)

    def _add_band(self, freq=None, gain=0.0):
        curve = self.session.curve
        if len(curve.bands) >= E.MAX_BANDS:
            self._toast(f'An equalizer holds at most {E.MAX_BANDS} bands')
            return
        if freq is None:
            # the widest gap on the log axis between existing bands
            import math
            fs = sorted([E.FREQ_MIN] + [b.freq for b in curve.bands]
                        + [E.FREQ_MAX])
            gaps = [(math.log(b / a), math.sqrt(a * b))
                    for a, b in zip(fs, fs[1:])]
            freq = round_freq(max(gaps)[1]) if gaps else 1000.0
        band = E.new_band_at(curve, freq, gain)
        self._edit(curve.added(band), select=len(curve.bands))

    def auto_gain(self):
        self._edit(self.session.curve.auto_preamp())
        self._toast(f'Preamp set to {self.session.curve.preamp:+.1f} dB')

    _auto_gain = auto_gain

    def _flat(self):
        self._edit(self.session.curve.flat())

    def _sort(self):
        curve = self.session.curve
        cur = curve.bands[self.sel] if self.sel is not None else None
        new = curve.sorted()
        sel = new.bands.index(cur) if cur in new.bands else self.sel
        self._edit(new, select=sel)

    def revert(self):
        """Back to the saved curve, as one step you can undo."""
        saved = self.session.saved.clamped()
        sel = self.sel if self.sel is not None and \
            self.sel < len(saved.bands) else None
        self._edit(saved, select=sel)

    _revert = revert

    def _undo(self):
        self.session.undo()
        self.render()

    def _redo(self):
        self.session.redo()
        self.render()

    def _listen_picked(self, group, _p):
        if self._updating:
            return
        mode = group.get_active_name()
        if mode:
            self.session.set_listen(mode)
            self.render()

    def _toggle_bypass(self):
        s = self.session
        if not s.can_hear:
            return
        s.set_listen(eqsession.EDIT if s.bypassed else eqsession.BYPASS)
        self.render()

    def _load_curve(self, curve, title=''):
        self._edit(curve.clamped(), select=0 if curve.bands else None)
        if title:
            self._toast(f'Loaded “{title}” — Undo to go back')

    def _loaded(self, kind, name):
        if self.on_loaded:
            self.on_loaded(kind, name)

    # ===================================================== files, text ==
    def _parsed(self, text, source):
        parsed = E.parse_text(text)
        if not parsed.curve.bands:
            self._toast(f'No equalizer filters found in {source}')
            return False
        self._load_curve(parsed.curve)
        self.show_notes(parsed.notes)
        return True

    def _import_file(self):
        pick_file(self.window, 'Import an AutoEQ / APO file',
                  self._read_file,
                  filters=[('Parametric EQ', ['*.txt']),
                           ('All files', ['*'])])

    def _read_file(self, path):
        if not path:
            return
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                text = f.read()
        except OSError:
            self._toast('Could not read that file')
            return
        if self._parsed(text, 'that file'):
            self._loaded('file', GLib.path_get_basename(path))

    def _on_drop(self, _t, value, _x, _y):
        files = value.get_files() if value else []
        if not files:
            return False
        self._read_file(files[0].get_path())
        return True

    def _paste(self):
        def done(clip, res):
            try:
                text = clip.read_text_finish(res)
            except GLib.Error:
                text = None
            if text:
                if self._parsed(text, 'the clipboard'):
                    self._loaded('paste', '')
            else:
                self._toast('The clipboard holds no text')
        self.get_clipboard().read_text_async(None, done)

    def _copy(self):
        self.get_clipboard().set(E.to_text(self.session.curve))
        self._toast('Copied as AutoEQ / APO text')

    def _export(self):
        name = self.session.enh.name if self.session.enh else 'equalizer'
        dlg = Gtk.FileDialog(title='Export the curve',
                             initial_name=f'{name}.txt')

        def done(d, res):
            try:
                gfile = d.save_finish(res)
            except GLib.Error:
                return
            try:
                with open(gfile.get_path(), 'w', encoding='utf-8') as f:
                    f.write(E.to_text(self.session.curve))
                self._toast('Exported')
            except OSError as e:
                self._toast(f'Could not write it: {e.strerror}')
        dlg.save(self.window, None, done)

    def _save_preset(self):
        dlg = Adw.AlertDialog(heading='Save as a preset',
                              body='It will be listed under Presets in '
                                   'every equalizer.')
        name = self.session.enh.name if self.session.enh else ''
        entry = Gtk.Entry(text=name, activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response('cancel', 'Cancel')
        dlg.add_response('ok', 'Save')
        dlg.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response('ok')

        def resp(_d, r):
            name = entry.get_text().strip()
            if r != 'ok' or not name:
                return
            curve = self.session.curve
            async_call(lambda: eqpresets.save_user(name, curve),
                       lambda p, e: self._toast(
                           f'Saved “{name}”' if p else f'Failed: {e}'))
        dlg.connect('response', resp)
        dlg.present(self)

    def _delete_preset(self, preset):
        if eqpresets.delete_user(preset):
            self._toast(f'Deleted “{preset.name}”')

    def show_notes(self, notes):
        if not notes:
            self.notes.set_reveal_child(False)
            return
        self.notes.set_visible(True)
        box = Gtk.Box(spacing=10)
        box.add_css_class('eq-notes')
        text = Gtk.Label(label='\n'.join(f'• {n}' for n in notes),
                         xalign=0, wrap=True, hexpand=True)
        box.append(Gtk.Image(icon_name='dialog-information-symbolic',
                             valign=Gtk.Align.START))
        box.append(text)
        box.append(icon_button('window-close-symbolic', 'Dismiss',
                               lambda *_: self.notes.set_reveal_child(False)))
        self.notes.set_child(box)
        self.notes.set_reveal_child(True)

    def _toast(self, text):
        # Window.toast escapes the text itself (it is Pango markup)
        toast = getattr(self.window, 'toast', None)
        if toast is not None:
            toast(text)
