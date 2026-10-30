"""Search the whole app: the header's search button, and Ctrl+K.

One dialog finds the pages, the rows they draw (settings, tools, actions),
the devices and apps that are live right now, what you have made (filter
chains, virtual devices, equalizers, Signal Paths strips, HRIR files, app
rules, device presets) and Preferences.  Picking a result opens its page
and shows the thing itself: a row is scrolled into view, lit for a moment
and given the keyboard focus, so Enter or Space acts on it; a device is
selected on Devices; an app's row is shown on the Mixer.

Fast by construction.  The matching (`backend/search.py`) runs against items
prepared once per opening, in a few milliseconds per keystroke.  Pages and
Preferences are ready at once; the rows are read from the pages' own
widgets on the next idle (~10 ms); everything live comes from one worker
(one `pw-dump` and a few small files) and is merged in when it lands, so
typing never waits for any of it.  Every source is read fresh on each
opening, so nothing it shows can be stale by more than that.

What is searched, and what is not: a row a page draws once (a setting, a
tool) is read from the page itself, so a new row is found with no change
here.  A row that is one item of a list (a chain, a rule, a device) is
marked `widgets.unsearched` and found from its store instead, even before
its page has loaded.  The Dashboard, Patchbay, Devices, Equalizer and
Signal Paths draw live state, not rows to find, and are left to those
stores.
"""

from __future__ import annotations

import os
from dataclasses import replace

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ..backend import endpoints, prefs, rules, virtual  # noqa: E402
from ..backend.search import KINDS, Index, Item, group_hits  # noqa: E402
from .widgets import async_call, device_icon, esc, reduce_motion  # noqa: E402

# What each page is for, in a few words (shown under it), and the words
# someone might use for it without knowing its name (never shown).
PAGE_INFO = {
    'dashboard': ('Default devices, favourites and what is playing',
                  ('home', 'overview', 'mixer', 'volume', 'apps', 'playing',
                   'default output', 'default input', 'favourites',
                   'favorites', 'levels', 'mute')),
    'paths': ('Route sources through effects into mixes',
              ('signal', 'routing', 'mix', 'bus', 'strip', 'source',
               'stream', 'obs', 'broadcast', 'podcast', 'send')),
    'enhance': ('Equalizers and microphone clean-up',
                ('eq', 'equalizer', 'equaliser', 'autoeq', 'parametric',
                 'bass', 'treble', 'tone', 'mic', 'noise', 'echo',
                 'cleanup', 'headphone correction', 'apo')),
    'graph': ('Connect and disconnect nodes by drawing cables',
              ('patchbay', 'cables', 'wires', 'links', 'connect', 'jack',
               'qpwgraph', 'helvum', 'graph', 'ports', 'midi')),
    'devices': ('Outputs and inputs: ports, profiles, formats',
                ('speakers', 'headphones', 'headset', 'microphone', 'hdmi',
                 'usb', 'bluetooth', 'sound card', 'output', 'input', 'sink',
                 'source', 'port', 'profile', 'rename', 'hide')),
    'virtual': ('Virtual outputs, microphones and combined devices',
                ('null sink', 'virtual microphone', 'loopback', 'combine',
                 'combined', 'aggregate', 'bus', 'sub-mix', 'channel map',
                 'pro audio', 'multiple outputs')),
    'surround': ('5.1 and 7.1 speakers, upmixing, speaker test',
                 ('5.1', '7.1', 'speakers', 'subwoofer', 'lfe', 'upmix',
                  'speaker test', 'home theater', 'layout')),
    'chains': ('Convolvers, virtual surround, crossfeed',
               ('filter chain', 'filter-chain', 'convolver', 'hrtf', 'hrir',
                'crossfeed', 'virtual surround', 'hesuvi',
                'impulse response', 'conf')),
    'effects': ('LADSPA and LV2 plugin racks',
                ('plugins', 'ladspa', 'lv2', 'vst', 'clap', 'rack', 'insert',
                 'compressor', 'reverb', 'limiter', 'gate')),
    'hrir': ('Impulse responses for virtual surround',
             ('hrtf', 'sofa', 'hesuvi', 'impulse response', 'binaural',
              'spatial', 'wav')),
    'server': ("PipeWire's clock: sample rate and buffer size",
               ('sample rate', 'quantum', 'buffer', 'latency', 'clock',
                'realtime', 'priority', 'pipewire.conf', 'low latency')),
    'streams': ("How apps' sound is resampled, upmixed and downmixed",
                ('resample', 'resampler', 'upmix', 'downmix', 'channel mix',
                 'lfe', 'stereo', 'client.conf', 'pulse')),
    'policy': ('Per-app rules, automatic device choice, clock source',
               ('rules', 'app rules', 'always use', 'route app',
                'priority', 'default device', 'clock master',
                'per application')),
    'wireplumber': ('Device power and Bluetooth audio',
                    ('bluetooth', 'codec', 'ldac', 'aac', 'aptx', 'sbc',
                     'msbc', 'headset', 'a2dp', 'hfp', 'suspend', 'power',
                     'wireplumber')),
    'monitor': ('Services, dropouts, DSP load, pw-top and logs',
                ('xruns', 'dropouts', 'crackling', 'cpu', 'dsp load',
                 'pw-top', 'journal', 'logs', 'status', 'services',
                 'health')),
    'tools': ('Restart services, latency calculator, maintenance',
              ('restart', 'reset', 'overrides', 'config folder', 'journal',
               'latency calculator', 'about', 'version')),
}

# Pages whose rows are read from their widgets.  The others draw live state
# (see the module docstring).
WALK_PAGES = ('server', 'streams', 'wireplumber', 'surround', 'tools',
              'monitor', 'policy', 'virtual', 'chains', 'effects', 'hrir')

# Preferences, listed rather than read: building the dialog starts probes.
# (tab, section, title the row has, words, title shown if not the row's).
# `test_search.local.py` checks every one against a real dialog.
PREF_ROWS = (
    ('general', 'Appearance', '@color_scheme',
     ('dark', 'light', 'theme', 'appearance', 'colour scheme'), None),
    ('general', 'Volume sliders', '', ('slider style', 'classic', 'stepped',
                                       'precision', 'meter'),
     'Volume slider style'),
    ('general', 'Volume sliders', '@level_meters',
     ('meters', 'vu', 'levels', 'cpu'), None),
    ('general', 'Pages', '@show_advanced', ('advanced', 'expert', 'hidden'),
     None),
    ('general', 'Pages', '@start_page', ('startup page', 'last page'), None),
    ('general', 'Reset', 'Patchbay layout', ('reset', 'positions'), None),
    ('general', 'Reset', 'Preferences', ('reset', 'defaults'),
     'Reset preferences'),
    ('startup', 'Start with your computer', 'Start at login',
     ('autostart', 'boot', 'login'), None),
    ('startup', 'Start with your computer', 'Start minimized',
     ('background', 'tray', 'hidden'), None),
    ('startup', 'Closing the window', 'Keep running in the background',
     ('close', 'background', 'tray'), None),
    ('startup', 'Needs the app running',
     'Switch device presets automatically', ('presets', 'auto'), None),
    ('startup', '', 'Quit PipeWire Controller', ('exit', 'close'), None),
    ('notifications', 'Notify me about', 'Broken or disconnected links',
     ('notifications', 'alerts'), None),
    ('notifications', 'Notify me about', 'Service failures',
     ('notifications', 'alerts', 'crash'), None),
    ('notifications', 'Notify me about', 'Audio dropouts (xruns)',
     ('notifications', 'alerts', 'crackling'), None),
    ('notifications', '', 'Send a test notification', ('notifications',),
     None),
    ('about', 'Support the project', 'Sponsor on GitHub',
     ('donate', 'support'), None),
    ('about', 'Support the project', 'Donate with PayPal',
     ('donate', 'support'), None),
    ('about', 'Feedback and help', 'Report a bug or suggest a feature',
     ('issue', 'bug', 'github', 'feature request'), None),
    ('about', 'Feedback and help', 'Ask a question',
     ('help', 'discussions', 'support'), None),
    ('about', 'Feedback and help', 'Known issues', ('bugs',), None),
    ('about', 'Feedback and help', 'What’s new',
     ('changelog', 'release notes', 'version'), None),
    ('about', 'Feedback and help', 'Source code', ('github', 'repository'),
     None),
    ('about', 'Troubleshooting', 'Debug information',
     ('version', 'report', 'system info'), None),
    ('about', 'Credits', 'License', ('gpl',), None),
)
PREF_TABS = {'general': 'General', 'startup': 'Startup',
             'notifications': 'Notifications', 'about': 'About'}

# A page named in full comes first (backend/search.py: title_boost); a row
# that is a real setting a little above other rows of the same name.
PAGE_BOOST = 30.0
RECENT_BOOST = 8.0
RECENT_MAX = 6
PER_SECTION = 5          # rows shown per section before "Show N more"

# How long a found row stays lit, then how long it takes to fade (style.css).
FOUND_MS, FADE_MS = 1400, 700
# How long to wait for a page to draw what was picked (it reads the graph
# when it maps).
WAIT_MS, WAIT_STEP_MS = 3000, 40


def plain(text) -> str:
    """A row's title or subtitle as text: they are Pango markup."""
    if not text:
        return ''
    try:
        ok, _attrs, out, _accel = Pango.parse_markup(text, -1, '\0')
        return out if ok else text
    except GLib.Error:
        return text


# ------------------------------------------------------------- the items --
def page_items(pages) -> list[Item]:
    """The pages, the Dashboard's two views, Preferences and About."""
    out = []
    for name, title, icon, _section in pages:
        sub, words = PAGE_INFO.get(name, ('', ()))
        out.append(Item(f'page:{name}', title, 'page', subtitle=sub,
                        icon=icon, keywords=words, title_boost=PAGE_BOOST,
                        target=('page', name)))
    out.append(Item('view:mixer', 'Mixer', 'page',
                    subtitle="Dashboard · every app's and device's volume",
                    icon='audio-volume-high-symbolic',
                    keywords=('volume', 'apps', 'mute', 'solo', 'levels'),
                    title_boost=PAGE_BOOST, target=('view', 'mixer')))
    out.append(Item('view:overview', 'Overview', 'page',
                    subtitle='Dashboard · default devices and favourites',
                    icon='view-grid-symbolic', keywords=('home',),
                    title_boost=PAGE_BOOST, target=('view', 'overview')))
    out.append(Item('pref:', 'Preferences', 'pref',
                    subtitle="The app's own choices (Ctrl+,)",
                    icon='pwctl-gear-symbolic',
                    keywords=('settings', 'options', 'configure'),
                    title_boost=PAGE_BOOST, target=('pref', 'general', '')))
    out.append(Item('pref:about', 'About PipeWire Controller', 'pref',
                    subtitle='Preferences · About', icon='help-about-symbolic',
                    keywords=('version', 'credits'),
                    target=('pref', 'about', '')))
    return out


def pref_items() -> list[Item]:
    out = []
    for tab, section, row, words, shown in PREF_ROWS:
        sub = ''
        if row.startswith('@'):
            p = prefs.BY_KEY[row[1:]]
            row, sub = p.title, p.subtitle
        title = shown or row
        where = ' · '.join(x for x in ('Preferences', PREF_TABS[tab],
                                       section) if x)
        out.append(Item(f'pref:{tab}:{title}', title, 'pref', subtitle=where,
                        icon='pwctl-gear-symbolic', keywords=words, text=sub,
                        target=('pref', tab, row or section)))
    return out


def _children(w):
    c = w.get_first_child()
    while c is not None:
        yield c
        c = c.get_next_sibling()


def _row_kind(row) -> str:
    """'action' for a row that does something when activated, else
    'setting' (a control, or a line of information)."""
    if isinstance(row, Adw.ButtonRow):
        return 'action'
    if isinstance(row, (Adw.SwitchRow, Adw.ComboRow, Adw.SpinRow,
                        Adw.EntryRow, Adw.ExpanderRow)):
        return 'setting'
    if isinstance(row, Adw.ActionRow) and (
            row.get_activatable() or row.get_activatable_widget()
            is not None):
        return 'action'
    return 'setting'


def _advanced_hidden(widget, advanced) -> bool:
    """Hidden only because advanced settings are off?"""
    w = widget
    while w is not None:
        if not w.get_visible():
            return w in advanced
        w = w.get_parent()
    return False


def row_items(window, pages) -> list[Item]:
    """Every row and section the walked pages draw, read from the widgets.

    A row hidden because advanced settings are off is still found, tagged,
    so search can say where it is and offer to show it.  One hidden for any
    other reason is not.
    """
    out, seen = [], set()
    advanced = set(getattr(window, '_advanced_widgets', ()))
    for name, page_title, icon, _section in pages:
        if name not in WALK_PAGES or name not in window.pages:
            continue

        def add(w, title, kind, where, text=''):
            hidden = not w.is_visible()
            if hidden and not _advanced_hidden(w, advanced):
                return
            ident = (name, where, title, kind)
            if ident in seen:
                return
            seen.add(ident)
            out.append(Item(
                f'row:{name}:{where}:{title}', title, kind,
                subtitle=' · '.join(x for x in (page_title, where) if x),
                icon=icon, text=text,
                keywords=getattr(w, 'pwctl_search_keywords', ()),
                boost=getattr(w, 'pwctl_search_boost', 0.0),
                tag='Advanced' if hidden else '',
                target=('row', name, w, title, hidden)))

        def visit(w, where, expander):
            if getattr(w, 'pwctl_search', True) is False:
                return
            if isinstance(w, Adw.PreferencesGroup):
                title = plain(w.get_title())
                if title:
                    add(w, title, 'section', '', plain(w.get_description()))
                    where = title
            elif isinstance(w, Adw.PreferencesRow):
                title = plain(w.get_title()) if w.get_use_markup() \
                    else w.get_title()
                # an expander's own header is a row of the same name
                if title and not (isinstance(w, Adw.ActionRow)
                                  and title == expander):
                    sub = w.get_subtitle() if isinstance(
                        w, (Adw.ActionRow, Adw.ExpanderRow)) else ''
                    add(w, title, _row_kind(w), where, plain(sub))
                if isinstance(w, Adw.ExpanderRow):
                    expander = title
                    where = f'{where} › {title}' if where else title
            for c in _children(w):
                visit(c, where, expander)

        visit(window.pages[name].widget, '', None)
    return out


def collect_data() -> dict:
    """Everything live, read once in a worker.  A source that fails is
    left out; the rest still show."""
    from ..backend import (chains, enhance, hrir, paths, presets, pw, rules,
                           virtual)
    out = {}

    def read(key, fn):
        try:
            out[key] = fn()
        except Exception:       # noqa: BLE001 - one source, not the search
            out[key] = None

    read('dump', pw.pw_dump)
    dump = out.pop('dump') or []
    read('nodes', lambda: pw.list_audio_nodes(dump))
    read('streams', lambda: pw.list_streams(dump, apps_only=True))
    read('chains', chains.list_chains)
    read('virtual', virtual.list_devices)
    read('enh', enhance.list_enhancements)
    read('strips', paths.list_strips)
    read('rules', rules.load)
    read('presets', presets.all_presets)

    def library():
        d = hrir.LIBRARY_DIR
        exts = hrir.AUDIO_EXTS | hrir.SOFA_EXTS
        return sorted((p.name for p in d.iterdir()
                       if p.is_file() and p.suffix.lower() in exts),
                      key=str.lower) if d.is_dir() else []
    read('hrir', library)
    return out


def data_items(data) -> list[Item]:
    """Items from `collect_data`, on the main loop."""
    out = []
    for n in data.get('nodes') or ():
        side = 'Output' if n.is_sink else 'Input'
        kind = endpoints.KIND_PILL.get(endpoints.device_kind(n), 'Virtual')
        out.append(Item(
            f'device:{side}:{n.name}', n.description or n.name, 'device',
            subtitle=f'{side} · {kind}', icon=device_icon(n),
            keywords=(n.name, side.lower(), kind.lower()),
            tag='Default' if n.is_default else '',
            target=('device', n.is_sink, n.name)))
    apps = {}
    for s in data.get('streams') or ():
        apps.setdefault((s.is_playback, s.name or s.media), []).append(s)
    for (playback, name), streams in apps.items():
        s = streams[0]
        verb = 'Playing' if playback else 'Recording'
        what = (f'{len(streams)} streams' if len(streams) > 1
                else (s.media if s.media and s.media != name else ''))
        out.append(Item(
            f'app:{verb}:{name}', name, 'app',
            subtitle=' · '.join(x for x in (verb, what) if x),
            icon='pwctl-source-app-symbolic',
            keywords=tuple(x for x in (s.binary, 'app') if x),
            target=('app', playback, s.id)))
    for c in data.get('chains') or ():
        rack = c.template == 'effect-rack'
        page = 'effects' if rack else 'chains'
        out.append(Item(
            f'chain:{c.id}', c.name, 'mine',
            subtitle=('Effects' if rack else 'Filter Chains')
            + (' · on' if c.enabled else ' · off'),
            icon='pwctl-effects-symbolic' if rack else 'pwctl-chains-symbolic',
            keywords=(c.template, os.path.basename(c.hrir or '')),
            target=('find', page, c.name)))
    for v in data.get('virtual') or ():
        out.append(Item(
            f'virtual:{v.id}', v.name, 'mine',
            subtitle='Virtual Devices · '
            + virtual.KINDS.get(v.kind, v.kind).split(' (')[0],
            icon='pwctl-virtual-symbolic', keywords=(v.kind,),
            target=('find', 'virtual', v.name)))
    for e in data.get('enh') or ():
        out.append(Item(
            f'enh:{e.id}', e.name, 'mine',
            subtitle=('Equalizer' if e.kind == 'eq' else 'Mic')
            + (' · on' if e.enabled else ' · off'),
            icon='pwctl-eq-symbolic',
            keywords=('equalizer',) if e.kind == 'eq' else ('mic cleanup',),
            target=('eq', e.kind, e.id)))
    for s in data.get('strips') or ():
        out.append(Item(
            f'strip:{s.id}', s.name, 'mine',
            subtitle='Signal Paths · ' + ('mix' if s.role == 'mix'
                                          else 'source'),
            icon='pwctl-paths-symbolic', target=('page', 'paths')))
    for name in data.get('hrir') or ():
        out.append(Item(f'hrir:{name}', name, 'mine', subtitle='HRIR Library',
                        icon='pwctl-hrir-symbolic',
                        target=('find', 'hrir', name)))
    for rule in (data.get('rules') or {}).get('apps') or ():
        match = dict(rule.get('match') or {})
        match.pop('media.class', None)
        _key, value = next(iter(match.items()), ('', ''))
        _neg, _rx, value = rules.split_pattern(str(value))
        if value:
            out.append(Item(f'rule:{value}', value, 'mine',
                            subtitle='App Policies · app rule',
                            icon='pwctl-policy-symbolic',
                            keywords=('rule', 'policy'),
                            target=('find', 'policy', value)))
    for name, p in (data.get('presets') or {}).items():
        out.append(Item(f'preset:{name}', p.get('description', name), 'mine',
                        subtitle='Device preset', icon='user-bookmarks-symbolic',
                        keywords=('preset',), target=('presets',)))
    return out


def recent() -> list[str]:
    keys = prefs.get('search_recent') or []
    return [k for k in keys if isinstance(k, str)][:RECENT_MAX]


def remember(key):
    keys = [key] + [k for k in recent() if k != key]
    prefs.save(search_recent=keys[:RECENT_MAX])


# ----------------------------------------------------------- the dialog --
class _ResultRow(Gtk.ListBoxRow):
    def __init__(self, item, spans=(), why='title', section=None):
        super().__init__()
        self.item = item
        self.section = section
        self.add_css_class('pwctl-search-row')
        box = Gtk.Box(spacing=12, margin_top=5, margin_bottom=5,
                      margin_start=6, margin_end=6)
        icon = Gtk.Image(icon_name=item.icon or 'system-search-symbolic',
                         valign=Gtk.Align.CENTER)
        icon.add_css_class('pwctl-search-icon')
        box.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                       valign=Gtk.Align.CENTER)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                          use_markup=True)
        title.set_markup(_marked(item.title, spans))
        text.append(title)
        if item.subtitle:
            sub = Gtk.Label(label=item.subtitle, xalign=0,
                            ellipsize=Pango.EllipsizeMode.END)
            sub.add_css_class('caption')
            sub.add_css_class('dim-label')
            text.append(sub)
        if why == 'text' and item.text:
            # Matched only by its description: show it, or the row would
            # seem to have nothing to do with what was typed.
            desc = Gtk.Label(label=item.text, xalign=0,
                             ellipsize=Pango.EllipsizeMode.END)
            desc.add_css_class('caption')
            desc.add_css_class('pwctl-search-why')
            text.append(desc)
        box.append(text)
        if item.tag:
            tag = Gtk.Label(label=item.tag, valign=Gtk.Align.CENTER)
            tag.add_css_class('pwctl-search-tag')
            box.append(tag)
        self.set_child(box)
        label = item.title + (f', {item.subtitle}' if item.subtitle else '')
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])


class _MoreRow(Gtk.ListBoxRow):
    def __init__(self, kind, count, section):
        super().__init__()
        self.kind, self.section, self.item = kind, section, None
        noun = KINDS.get(kind, 'results').lower()
        label = Gtk.Label(label=f'Show {count} more {noun}', xalign=0,
                          margin_top=4, margin_bottom=4, margin_start=34)
        label.add_css_class('caption')
        label.add_css_class('dim-label')
        self.add_css_class('pwctl-search-more')
        self.set_child(label)


def _marked(title, spans) -> str:
    """The title as markup, what the query matched in bold."""
    out, at = [], 0
    for a, b in spans:
        out.append(esc(title[at:a]))
        out.append(f'<b>{esc(title[a:b])}</b>')
        at = b
    out.append(esc(title[at:]))
    return ''.join(out)


class SearchDialog(Adw.Dialog):
    """The search box and its results; Enter opens the first, the arrows
    move, Esc closes."""

    def __init__(self, search):
        super().__init__(title='Search', content_width=560,
                         content_height=540)
        self.search = search
        self.window = search.window
        self._expanded = set()
        self.last_ms = 0.0           # search + drawing, for the harness
        self.add_css_class('pwctl-search')

        self.entry = Gtk.SearchEntry(
            placeholder_text='Search settings, devices, apps…',
            hexpand=True, search_delay=40)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL],
                                   ['Search the app'])
        self.entry.connect('search-changed', lambda *_a: self.run())
        self.entry.connect('activate', lambda *_a: self._open_selected())
        self.entry.connect('stop-search', lambda *_a: self.close())
        keys = Gtk.EventControllerKey(
            propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self._on_key)
        self.entry.add_controller(keys)

        clamp = Adw.Clamp(maximum_size=520, hexpand=True,
                          child=self.entry)
        header = Adw.HeaderBar()
        header.set_title_widget(clamp)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                css_classes=['navigation-sidebar',
                                             'pwctl-search-list'])
        self.list.set_header_func(self._header)
        self.list.connect('row-activated', self._on_row)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vexpand=True, child=self.list)
        self.scroller = scroller
        self.none = Adw.StatusPage(icon_name='system-search-symbolic',
                                   title='No results')
        self.none.add_css_class('compact')
        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, 'list')
        self.stack.add_named(self.none, 'none')

        foot = Gtk.Box(spacing=18, halign=Gtk.Align.CENTER, margin_top=6,
                       margin_bottom=6)
        foot.add_css_class('pwctl-search-foot')
        for keyname, what in (('↑↓', 'move'), ('Enter', 'open'),
                              ('Esc', 'close')):
            pair = Gtk.Box(spacing=6)
            k = Gtk.Label(label=keyname)
            k.add_css_class('pwctl-search-key')
            w = Gtk.Label(label=what)
            w.add_css_class('dim-label')
            pair.append(k)
            pair.append(w)
            foot.append(pair)
        self.foot = foot

        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        view.add_bottom_bar(foot)
        self.set_child(view)
        self.entry.set_key_capture_widget(self)
        self.set_focus(self.entry)
        self.run()

    # ----------------------------------------------------------- keys --
    def _on_key(self, _ctl, keyval, _code, _state):
        step = {Gdk.KEY_Down: 1, Gdk.KEY_KP_Down: 1, Gdk.KEY_Up: -1,
                Gdk.KEY_KP_Up: -1, Gdk.KEY_Page_Down: 8,
                Gdk.KEY_Page_Up: -8}.get(keyval)
        if step is None:
            return False
        self._move(step)
        return True

    def _rows(self):
        out, i = [], 0
        while (row := self.list.get_row_at_index(i)) is not None:
            out.append(row)
            i += 1
        return out

    def _move(self, step):
        rows = self._rows()
        if not rows:
            return
        cur = self.list.get_selected_row()
        i = rows.index(cur) if cur in rows else -1
        i = max(0, min(len(rows) - 1, i + step))
        self._select(rows[i])

    def _select(self, row):
        self.list.select_row(row)
        if row is None:
            return
        # keep it in view, without taking the focus from the entry
        ok, b = row.compute_bounds(self.list)
        adj = self.scroller.get_vadjustment()
        if ok:
            top, bottom = b.get_y(), b.get_y() + b.get_height()
            if top < adj.get_value():
                adj.set_value(top)
            elif bottom > adj.get_value() + adj.get_page_size():
                adj.set_value(bottom - adj.get_page_size())

    # ------------------------------------------------------- results --
    def refresh(self):
        """The items changed (rows read, live data in): same query again,
        the same result kept selected."""
        cur = self.list.get_selected_row()
        key = getattr(getattr(cur, 'item', None), 'key', None)
        self.run(keep=key)

    def run(self, keep=None):
        t0 = GLib.get_monotonic_time()
        query = self.entry.get_text().strip()
        self.list.remove_all()
        rows = []
        if query:
            hits = self.search.index.search(query)
            groups = group_hits(hits)
            for kind, shown, _more in groups:
                limit = None if kind in self._expanded or len(groups) == 1 \
                    else PER_SECTION
                section = KINDS.get(kind, kind)
                for h in shown[:limit]:
                    rows.append(_ResultRow(h.item, h.spans, h.why, section))
                if limit is not None and len(shown) > limit:
                    rows.append(_MoreRow(kind, len(shown) - limit, section))
        else:
            index = {i.key: i for i in self.search.index.items}
            for key in recent():
                if key in index:
                    rows.append(_ResultRow(index[key], section='Recent'))
            done = {r.item.key for r in rows}
            for item in self.search.pages:
                if item.key not in done and item.key.startswith('page:'):
                    rows.append(_ResultRow(item, section='Pages'))
        for r in rows:
            self.list.append(r)
        if rows:
            self.stack.set_visible_child_name('list')
            pick = next((r for r in rows if keep and r.item is not None
                         and r.item.key == keep), rows[0])
            self.list.select_row(pick)
            if pick is rows[0]:
                self.scroller.get_vadjustment().set_value(0)
        else:
            self.none.set_description(
                f'Nothing matches “{esc(query)}”. Try a setting’s name, a '
                'device, an app, or a word like latency or bluetooth.')
            self.stack.set_visible_child_name('none')
        self.last_ms = (GLib.get_monotonic_time() - t0) / 1000

    def _header(self, row, before):
        if before is not None and before.section == row.section:
            row.set_header(None)
            return
        label = Gtk.Label(label=row.section.upper(), xalign=0)
        label.add_css_class('micro-heading')
        label.add_css_class('pwctl-search-section')
        row.set_header(label)

    # ---------------------------------------------------------- open --
    def _open_selected(self):
        row = self.list.get_selected_row()
        if row is not None:
            self._on_row(self.list, row)

    def _on_row(self, _list, row):
        if isinstance(row, _MoreRow):
            self._expanded.add(row.kind)
            at = self._rows().index(row)
            self.run()
            rows = self._rows()
            if rows:
                self._select(rows[min(at, len(rows) - 1)])
            return
        item = row.item
        remember(item.key)
        self.close()
        reveal(self.window, item)


class AppSearch:
    """The window's search: the items, read fresh on every opening."""

    def __init__(self, window, pages):
        self.window = window
        self.page_list = pages
        self.pages = page_items(pages)
        self.dialog = None
        self.index = Index(self.pages)
        self._gen = 0
        self._after = []

    def after_close(self, fn):
        """Run `fn` once the dialog has closed (at once if it has)."""
        if self.dialog is None:
            fn()
        else:
            self._after.append(fn)

    def open(self):
        if self.dialog is not None:
            self.dialog.close()          # Ctrl+K again closes it
            return
        self._gen += 1
        gen = self._gen
        self._static = self.pages + pref_items()
        self._rows, self._data = [], []
        self._rebuild()
        dlg = SearchDialog(self)
        self.dialog = dlg
        dlg.connect('closed', self._on_closed)
        dlg.present(self.window)
        # The rows on the next idle, so the dialog is up first; the live
        # data from a worker, merged in whenever it lands.
        GLib.idle_add(self._read_rows, gen)
        async_call(collect_data, lambda d, e: self._got_data(d, e, gen))

    def _on_closed(self, dlg):
        if self.dialog is dlg:
            self.dialog = None
        after, self._after = self._after, []
        for fn in after:
            fn()

    def _rebuild(self):
        # What was opened lately ranks a little higher: on copies, as the
        # same items are indexed again when the rows and the data land.
        keys = set(recent())
        items = [replace(i, boost=i.boost + RECENT_BOOST) if i.key in keys
                 else i for i in self._static + self._rows + self._data]
        self.index = Index(items)

    def _read_rows(self, gen):
        if gen == self._gen and self.dialog is not None:
            self._rows = row_items(self.window, self.page_list)
            self._rebuild()
            self.dialog.refresh()
        return False

    def _got_data(self, data, error, gen):
        if gen != self._gen or self.dialog is None or error or not data:
            return
        self._data = data_items(data)
        self._rebuild()
        self.dialog.refresh()


# --------------------------------------------------------- landing on it --
def reveal(window, item):
    """Open what `item` names and show it."""
    t = item.target
    kind = t[0]
    if kind == 'page':
        window.goto(t[1])
    elif kind == 'view':
        window.pages['dashboard'].views.set_visible_child_name(t[1])
        window.goto('dashboard')
    elif kind == 'row':
        _name, page, widget, title, hidden = t
        window.goto(page)
        if hidden:
            _offer_advanced(window, widget, title)
        elif widget.get_root() is window:
            unfold(widget)          # it can't be drawn while folded away
            when_drawn(lambda: widget, flash)
        else:
            _find_and_flash(window, page, title)
    elif kind == 'find':
        window.goto(t[1])
        _find_and_flash(window, t[1], t[2])
    elif kind == 'device':
        window.goto('devices')
        window.pages['devices'].reveal_node(t[1], t[2])
    elif kind == 'app':
        find = window.pages['dashboard'].reveal_stream(t[1], t[2])
        window.goto('dashboard')
        when_drawn(find, flash)
    elif kind == 'eq':
        window.goto('enhance')
        window.pages['enhance'].reveal(t[1], t[2])
    elif kind == 'pref':
        def prefs_tab():
            dlg = window.open_preferences(t[1])
            if t[2]:
                page = dlg.pages.get(t[1])
                when_drawn(lambda: find_row(page, t[2]), flash)
        # one dialog after the other, so the focus lands in Preferences
        search = getattr(window, 'app_search', None)
        if search is not None:
            search.after_close(prefs_tab)
        else:
            prefs_tab()
    elif kind == 'presets':
        button = getattr(window, 'presets_button', None)
        if button is not None and button.get_mapped():
            button.popup()


def _offer_advanced(window, widget, title):
    """An advanced setting while they are hidden: say so, and offer to
    show them (never turned on by itself)."""
    toast = Adw.Toast(
        title=esc(f'“{title}” is an advanced setting, hidden now'),
        button_label='Show advanced settings', timeout=8)

    def show(_t):
        prefs.save(show_advanced=True)
        # the pages show it once the preference has gone round
        when_drawn(lambda: widget if widget.is_visible() else None, flash)
    toast.connect('button-clicked', show)
    window.toaster.add_toast(toast)


def find_row(root, title):
    """The first visible row or section under `root` with this title."""
    if root is None:
        return None
    stack = [root]
    while stack:
        w = stack.pop(0)
        if isinstance(w, (Adw.PreferencesRow, Adw.PreferencesGroup)) \
                and w.is_visible() and plain(w.get_title()) == title:
            return w
        stack.extend(_children(w))
    return None


def _find_and_flash(window, page, title):
    root = window.pages[page].widget

    def find():
        row = find_row(root, title)
        if row is not None:
            unfold(row)
        return row
    when_drawn(find, flash)


def when_drawn(find, then, wait_ms=WAIT_MS):
    """Call `then(widget)` once `find()` returns a widget that is on screen
    and laid out.  A page reads its data when it maps, so what was picked
    may be drawn a moment after the page is shown.  A timer, not a frame
    callback: it has to work where frames do not tick (Broadway)."""
    tries = [max(1, wait_ms // WAIT_STEP_MS)]

    def check():
        w = find()
        if w is not None and w.get_mapped() and w.get_width() > 0:
            then(w)
            return False
        tries[0] -= 1
        return tries[0] > 0
    if check():
        GLib.timeout_add(WAIT_STEP_MS, check)


def unfold(widget):
    """Open every folded row `widget` is inside."""
    w = widget.get_parent()
    while w is not None:
        if isinstance(w, Adw.ExpanderRow) and not w.get_expanded():
            w.set_expanded(True)
        w = w.get_parent()


def flash(widget):
    """Show where `widget` is: open what folds it away, scroll it into
    view, light it for a moment and give it the focus."""
    unfold(widget)
    scroll_into_view(widget)
    widget.remove_css_class('pwctl-found-out')
    widget.add_css_class('pwctl-found')

    def fade():
        widget.remove_css_class('pwctl-found')
        widget.add_css_class('pwctl-found-out')
        GLib.timeout_add(FADE_MS, lambda: (widget.remove_css_class(
            'pwctl-found-out'), False)[1])
        return False
    GLib.timeout_add(FOUND_MS, fade)
    target = widget
    if isinstance(widget, Adw.PreferencesGroup):
        target = next((r for r in _descendants(widget)
                       if isinstance(r, Gtk.ListBoxRow) and r.is_visible()
                       and r.get_focusable()), None)
    if isinstance(target, (Adw.SpinRow, Adw.EntryRow)):
        # Their focus goes into the text, selected: the next key typed
        # would replace a saved value.  Lit and in view is enough.
        target = None
    if target is not None:
        # After the search dialog has gone: closing, it gives the focus
        # back to whatever had it before.
        search = getattr(widget.get_root(), 'app_search', None)
        if search is not None:
            search.after_close(target.grab_focus)
        else:
            target.grab_focus()


def _descendants(w):
    stack = list(_children(w))
    while stack:
        c = stack.pop(0)
        yield c
        stack.extend(_children(c))


def scroll_into_view(widget):
    """Scroll the nearest scrolled window so `widget` sits in its middle
    (or its top, if it is taller than the view)."""
    sw = widget.get_ancestor(Gtk.ScrolledWindow)
    if sw is None or sw.get_child() is None:
        return
    content = sw.get_child()
    inner = content.get_child() if isinstance(content, Gtk.Viewport) \
        else content
    ok, b = widget.compute_bounds(inner if inner is not None else content)
    if not ok:
        return
    adj = sw.get_vadjustment()
    page = adj.get_page_size()
    want = b.get_y() - max(0.0, (page - b.get_height()) / 2)
    want = max(adj.get_lower(), min(want, adj.get_upper() - page))
    start = adj.get_value()
    if abs(want - start) < 1:
        return
    if reduce_motion(widget) or not Adw.get_enable_animations(widget):
        adj.set_value(want)
        return
    anim = Adw.TimedAnimation.new(
        sw, start, want, 250,
        Adw.CallbackAnimationTarget.new(adj.set_value))
    anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
    sw._pwctl_scroll = anim       # kept alive until it has run
    anim.play()
