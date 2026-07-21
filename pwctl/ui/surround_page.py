"""Surround Setup: guided speaker configuration wizard.

Four steps on one page: layout → card profile → upmix defaults → speaker
test. Profile changes apply instantly (wpctl); upmix settings reuse the
schema SettingRows so they land in the same drop-ins as the Streams page.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import chains, prefs, schema, surround
from ..backend.pw import pw_dump
from ..backend.surround import UPMIX_KEYS
from .settings_pages import SettingRow
from .widgets import async_call, confirm, group, page_scroller, pill

VSINK_NAME = 'Virtual 7.1 Headphones'
VSINK_TEMPLATE = 'plain-71-sink'

PRESETS = {
    'stereo': {'channelmix.upmix': False},
    '2.1':    {'channelmix.upmix': True, 'channelmix.upmix-method': 'psd',
               'channelmix.lfe-cutoff': 120},
    'quad':   {'channelmix.upmix': True, 'channelmix.upmix-method': 'psd',
               'channelmix.rear-delay': 12},
    '5.1':    {'channelmix.upmix': True, 'channelmix.upmix-method': 'psd',
               'channelmix.lfe-cutoff': 120, 'channelmix.rear-delay': 12},
    '7.1':    {'channelmix.upmix': True, 'channelmix.upmix-method': 'psd',
               'channelmix.lfe-cutoff': 120, 'channelmix.rear-delay': 12},
}

# 3×3 room grid: (row, column) per speaker position; listener sits at (1, 1)
POS_GRID = {'FL': (0, 0), 'FC': (0, 1), 'FR': (0, 2),
            'SL': (1, 0), 'SR': (1, 2),
            'RL': (2, 0), 'LFE': (2, 1), 'RR': (2, 2)}


class SurroundPage:
    def __init__(self, window):
        self.window = window
        self.updating = False
        self.cards = []
        self.sinks = []              # (node_id, device_id)
        self._profile_indices = []   # combo position -> profile index
        self.layout_key = prefs.get('surround_layout')
        if not any(l[0] == self.layout_key for l in surround.LAYOUTS):
            self.layout_key = '5.1'

        # ---- step 1: layout ------------------------------------------------
        step1 = group('1 · Speaker layout',
                      'What is physically connected to this machine?')
        self.layout_row = Adw.ComboRow(
            title='Layout',
            model=Gtk.StringList.new([l[1] for l in surround.LAYOUTS]))
        self.layout_row.set_selected(
            next(i for i, l in enumerate(surround.LAYOUTS)
                 if l[0] == self.layout_key))
        self.layout_row.connect('notify::selected', self._layout_changed)
        step1.add(self.layout_row)

        # ---- headphones alternative: virtual 7.1 sink ---------------------
        vgroup = group('No surround speakers? Virtual 7.1 sink for headphones',
                       'Creates a VIRTUAL output device — not real hardware. '
                       'Apps that select it see all 8 channels; PipeWire '
                       'folds the signal down to your real output. It is '
                       'never made the default automatically.')
        self.vsink_row = Adw.ActionRow(
            title=VSINK_NAME,
            subtitle='Runs as its own PipeWire process '
                     '(pwctl-chain@… systemd unit) — manage or remove it '
                     'under Filter Chains',
            title_lines=1, subtitle_lines=2)
        self.vsink_row.add_prefix(
            Gtk.Image.new_from_icon_name('application-x-addon-symbolic'))
        self.vsink_pill = pill('not created', 'dim')
        self.vsink_row.add_suffix(self.vsink_pill)
        self.vsink_create = Gtk.Button(label='Create')
        self.vsink_create.add_css_class('suggested-action')
        self.vsink_create.connect('clicked', self._vsink_create)
        self.vsink_remove = Gtk.Button(label='Remove')
        self.vsink_remove.add_css_class('destructive-action')
        self.vsink_remove.connect('clicked', self._vsink_remove)
        self.vsink_manage = Gtk.Button(label='Manage')
        self.vsink_manage.connect(
            'clicked', lambda *_: self.window.goto('chains'))
        for b in (self.vsink_create, self.vsink_remove, self.vsink_manage):
            b.set_valign(Gtk.Align.CENTER)
            self.vsink_row.add_suffix(b)
        self.vsink_remove.set_visible(False)
        self.vsink_manage.set_visible(False)
        vgroup.add(self.vsink_row)
        self._vsink_meta = None

        # ---- step 2: card profile -----------------------------------------
        step2 = group('2 · Sound card profile',
                      'The card profile decides which channels exist. Pick '
                      'the one matching your layout (★ = suggested). '
                      'Applied immediately.')
        self.card_row = Adw.ComboRow(title='Sound card')
        self.card_row.connect('notify::selected', self._card_changed)
        self.profile_row = Adw.ComboRow(title='Profile')
        self.profile_row.connect('notify::selected', self._profile_changed)
        step2.add(self.card_row)
        step2.add(self.profile_row)

        # ---- step 3: upmix ------------------------------------------------
        step3 = group('3 · Upmix and bass management',
                      'How stereo content fills the extra speakers. Saved as '
                      'defaults for every app — restart PipeWire-Pulse and '
                      'reopen apps to hear it.')
        preset_row = Adw.ActionRow(
            title='Recommended settings',
            subtitle='One click applies proven defaults for the chosen layout')
        preset_btn = Gtk.Button(label='Apply preset')
        preset_btn.add_css_class('suggested-action')
        preset_btn.set_valign(Gtk.Align.CENTER)
        preset_btn.connect('clicked', self._apply_preset)
        preset_row.add_suffix(preset_btn)
        step3.add(preset_row)

        self.setting_rows = {}
        by_key = {s.key: s for s in schema.STREAM}
        for key in UPMIX_KEYS:
            s = by_key[key]
            sr = SettingRow(s, window)
            self.setting_rows[key] = sr
            step3.add(sr.row)
            if s.advanced:
                window.register_advanced(sr.row)

        # ---- step 4: speaker test -----------------------------------------
        step4 = group('4 · Speaker test',
                      'Click a speaker to hear a tone from it (subwoofer '
                      'plays 60 Hz). Tones go to the sound card chosen above.')
        self.grid = Gtk.Grid(row_spacing=12, column_spacing=12,
                             halign=Gtk.Align.CENTER,
                             margin_top=12, margin_bottom=6)
        test_all = Gtk.Button(label='Test all speakers in order',
                              halign=Gtk.Align.CENTER, margin_bottom=12)
        test_all.add_css_class('pill')
        test_all.connect('clicked', self._test_all)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        holder.append(self.grid)
        holder.append(test_all)
        test_row = Gtk.ListBox(css_classes=['boxed-list'],
                               selection_mode=Gtk.SelectionMode.NONE)
        wrap = Gtk.ListBoxRow(activatable=False)
        wrap.set_child(holder)
        test_row.append(wrap)
        step4.add(test_row)
        self._build_grid()

        self.widget = page_scroller(step1, vgroup, step2, step3, step4)
        self.widget.connect('map', lambda *_: self.refresh())

    # ------------------------------------------------------------- helpers --
    @property
    def positions(self):
        return surround.layout(self.layout_key)[2]

    def _selected_card(self):
        idx = self.card_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.cards):
            return None
        return self.cards[idx]

    def _target_sink(self):
        """Sink node id belonging to the selected card, else None (default)."""
        card = self._selected_card()
        if card:
            for node_id, device_id, _name, _desc in self.sinks:
                if device_id == card.id:
                    return node_id
        return None

    def _real_target(self):
        """(node.name, description) of the sink the virtual sink should feed."""
        card = self._selected_card()
        if card:
            for _nid, device_id, name, desc in self.sinks:
                if device_id == card.id:
                    return name, desc
        return '', 'the default output'

    # ------------------------------------------------------------- refresh --
    def refresh(self):
        def collect():
            dump = pw_dump()
            cards = surround.list_cards(dump)
            sinks = []
            for obj in dump:
                if obj.get('type') != 'PipeWire:Interface:Node':
                    continue
                props = (obj.get('info') or {}).get('props') or {}
                if props.get('media.class') != 'Audio/Sink':
                    continue
                try:
                    dev = int(props.get('device.id'))
                except (TypeError, ValueError):
                    dev = -1
                sinks.append((obj['id'], dev, props.get('node.name', ''),
                              props.get('node.description', '')))
            vsink = next((m for m in chains.list_chains()
                          if m.template == VSINK_TEMPLATE), None)
            vstate = chains.status(vsink) if vsink else None
            return cards, sinks, vsink, vstate
        async_call(collect, self._apply)

    def _apply(self, data, error):
        if error or not data:
            return
        cards, self.sinks, vsink, vstate = data
        self._update_vsink_row(vsink, vstate)
        old = self._selected_card()
        self.updating = True
        try:
            self.cards = cards
            self.card_row.set_model(
                Gtk.StringList.new([c.description for c in cards]))
            keep = next((i for i, c in enumerate(cards)
                         if old and c.id == old.id), 0)
            if cards:
                self.card_row.set_selected(keep)
            self._fill_profiles()
        finally:
            self.updating = False

    # ------------------------------------------------------- virtual sink --
    def _update_vsink_row(self, vsink, vstate):
        self._vsink_meta = vsink
        exists = vsink is not None
        self.vsink_create.set_visible(not exists)
        self.vsink_remove.set_visible(exists)
        self.vsink_manage.set_visible(exists)
        self.vsink_pill.set_label(vstate if exists else 'not created')
        for c in list(self.vsink_pill.get_css_classes()):
            if c.startswith('pill-'):
                self.vsink_pill.remove_css_class(c)
        self.vsink_pill.add_css_class(
            'pill-success' if vstate == 'active' else 'pill-dim')

    def _vsink_create(self, _b):
        target_name, target_desc = self._real_target()
        body = (f'This adds a virtual output device called '
                f'“{VSINK_NAME}” — it is NOT real hardware.\n\n'
                f'• Runs as its own PipeWire process '
                f'(systemd unit pwctl-chain@….service), started now and on '
                f'every boot\n'
                f'• Apps that use it see 8 channels; the signal is folded '
                f'down and sent to “{target_desc}”\n'
                f'• It will NOT become the default output — pick it per-app '
                f'in the Playback tab, or star it yourself under Output '
                f'Devices\n'
                f'• Downmix quality follows the channel-mix settings in '
                f'step 3\n'
                f'• Remove or disable it anytime (here or in Filter Chains)')

        def create():
            def work():
                meta = chains.new_chain(VSINK_NAME, VSINK_TEMPLATE,
                                        target=target_name)
                meta.enabled = True
                chains.save_meta(meta)
                return chains.apply(meta)

            def done(res, e):
                ok = res and res[0] and not e
                self.window.toast(
                    f'{VSINK_NAME} created — select it in Playback'
                    if ok else f'Failed: {e or (res and res[1])}')
                self.refresh()
            async_call(work, done)
        confirm(self.widget.get_root(), 'Create virtual 7.1 sink?', body,
                'Create sink', create, destructive=False)

    def _vsink_remove(self, _b):
        meta = self._vsink_meta
        if not meta:
            return

        def remove():
            async_call(lambda: chains.delete(meta),
                       lambda r, e: (self.window.toast(
                           f'{VSINK_NAME} removed'
                           if not e else f'Failed: {e}'),
                           self.refresh()))
        confirm(self.widget.get_root(), 'Remove virtual sink?',
                f'Streams currently playing to “{VSINK_NAME}” will move '
                f'back to the default output.',
                'Remove', remove)

    def _fill_profiles(self):
        """Populate the profile combo for the selected card."""
        card = self._selected_card()
        self._profile_indices = []
        if not card:
            self.profile_row.set_model(Gtk.StringList.new([]))
            return
        suggested = surround.suggest_profile(card, self.layout_key)
        labels = []
        select = 0
        for pos, (idx, desc, available) in enumerate(card.profiles):
            label = desc
            if idx == suggested:
                label = '★  ' + label
            if available == 'no':
                label += '  (unavailable)'
            labels.append(label)
            self._profile_indices.append(idx)
            if idx == card.active_profile:
                select = pos
        self.profile_row.set_model(Gtk.StringList.new(labels))
        self.profile_row.set_selected(select)

    # ------------------------------------------------------------ handlers --
    def _layout_changed(self, row, _p):
        self.layout_key = surround.LAYOUTS[row.get_selected()][0]
        prefs.save(surround_layout=self.layout_key)
        self._build_grid()
        if not self.updating:
            was = self.updating
            self.updating = True
            try:
                self._fill_profiles()   # re-rank the ★ suggestion
            finally:
                self.updating = was

    def _card_changed(self, _row, _p):
        if self.updating:
            return
        self.updating = True
        try:
            self._fill_profiles()
        finally:
            self.updating = False

    def _profile_changed(self, row, _p):
        if self.updating:
            return
        card = self._selected_card()
        pos = row.get_selected()
        if not card or pos >= len(self._profile_indices):
            return
        profile_idx = self._profile_indices[pos]
        desc = card.profiles[pos][1]
        async_call(lambda: surround.set_profile(card.id, profile_idx),
                   lambda ok, e: (
                       self.window.toast(f'Profile: {desc}' if ok and not e
                                         else 'Profile change failed'),
                       GLib.timeout_add(1000, lambda: (self.refresh(),
                                                       False)[1])))

    def _apply_preset(self, _b):
        preset = PRESETS.get(self.layout_key, {})
        for key, value in preset.items():
            sr = self.setting_rows[key]
            sr.updating = True
            try:
                sr._set_widget(value)
            finally:
                sr.updating = False
            sr.write(value)
        name = surround.layout(self.layout_key)[1]
        self.window.toast(f'Applied recommended settings for {name}')

    # -------------------------------------------------------- speaker test --
    def _build_grid(self):
        while (child := self.grid.get_first_child()) is not None:
            self.grid.remove(child)
        listener = Gtk.Image.new_from_icon_name('avatar-default-symbolic')
        listener.set_pixel_size(28)
        listener.set_tooltip_text('You')
        self.grid.attach(listener, 1, 1, 1, 1)
        for pos in self.positions:
            r, c = POS_GRID[pos]
            self.grid.attach(self._speaker_button(pos), c, r, 1, 1)

    def _speaker_button(self, pos):
        btn = Gtk.Button()
        btn.set_size_request(130, 56)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      valign=Gtk.Align.CENTER)
        code = Gtk.Label(label=pos)
        code.add_css_class('heading')
        name = Gtk.Label(label=surround.SPEAKER_NAMES[pos])
        name.add_css_class('caption')
        name.add_css_class('dim-label')
        box.append(code)
        box.append(name)
        btn.set_child(box)
        btn.connect('clicked', lambda *_: self._test(pos))
        return btn

    def _test(self, pos):
        positions = self.positions
        idx = positions.index(pos)
        target = self._target_sink()
        async_call(lambda: surround.play_test_tone(target, idx, positions))
        self.window.toast(f'Playing: {surround.SPEAKER_NAMES[pos]}')

    def _test_all(self, _b):
        for i, pos in enumerate(self.positions):
            GLib.timeout_add(int(i * 1400),
                             lambda p=pos: (self._test(p), False)[1])
