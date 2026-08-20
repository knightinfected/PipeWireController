# PipeWire Controller

[![AUR version](https://img.shields.io/aur/version/pipewire-control-center?logo=archlinux&label=AUR)](https://aur.archlinux.org/packages/pipewire-control-center)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
[![Sponsor on GitHub](https://img.shields.io/badge/GitHub-sponsor-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/knightinfected)
[![Donate via PayPal](https://img.shields.io/badge/PayPal-support%20this%20project-00457C?logo=paypal&logoColor=white)](https://paypal.me/hmzknight)

A native GTK4/libadwaita control center for PipeWire — for any Linux distro
running PipeWire (packaged on the AUR, install instructions for Fedora,
Ubuntu/Debian and others below).
Everything under PipeWire's *Configuration* documentation — clock/quantum
tuning, stream processing, session policy, filter chains, HRIR virtual
surround — exposed as toggles and dropdowns, plus **Signal Paths** for building
the route your audio actually takes ([new in v0.4.0](#new-in-v040)) with
**crossovers** that split the audio by frequency and send each band to its own
speakers, **live
level meters** on every volume control, a parametric **equalizer** and
**microphone cleanup** ([new in v0.3.6](#new-in-v036)), a live **patchbay**,
performance **monitoring**, **virtual devices**, routing snapshots,
per-application **policies** and LADSPA/LV2 **effect inserts**
([new in v0.3.0](#new-in-v030)).

![Dashboard — Overview, with live level meters on the default endpoints](screenshots/dashboard-0.4.0.png)

![Signal Paths — sources on the left, mixes on the right, and the sends between them drawn as curves](screenshots/signal-paths-board-light.png)

![Signal Paths — an empty board is the template catalog, from a plain speaker mix to a full broadcast chain](screenshots/signal-paths-start.png)

![Patchbay — the live graph of your whole session, drag a port onto another to connect](screenshots/patchbay-0.3.6.png)

<p align="center">
  <img src="screenshots/equalizer.png" alt="Equalizer — parametric EQ outputs with an inline mixer and live A/B compare" width="53.5%">
  <img src="screenshots/devices-per-device-settings.png" alt="Devices — per-device sample rate, bit depth, period size, headroom and more" width="44%">
</p>
<p align="center">
  <img src="screenshots/playback-live-meters.png" alt="Playback tab — a live level meter on every application stream" width="55%">
  <img src="screenshots/microphone-cleanup.png" alt="Microphone cleanup — echo and noise removal as a clean second microphone" width="43%">
</p>
<p align="center">
  <img src="screenshots/virtual-devices.png" alt="Virtual Devices — null sinks, virtual mics, aggregates and buses" width="59.5%">
  <img src="screenshots/app-policies.png" alt="App Policies — per-app routing, default priority, clock master" width="38%">
</p>
<p align="center">
  <img src="screenshots/streams.png" alt="Stream processing defaults — upmix, LFE, resampler and advanced knobs" width="44.5%">
  <img src="screenshots/tools.png" alt="Tools — service control, latency calculator and maintenance" width="47.5%">
</p>
<p align="center">
  <img src="screenshots/server.png" alt="Server settings" width="70%">
</p>

More screenshots:
[Signal Paths, one source and one mix](screenshots/signal-paths-simple.png) ·
[Signal Paths, a mix on its own](screenshots/signal-paths-mix-only.png) ·
[Signal Paths, dark mode](screenshots/signal-paths-board.png) ·
[Template browser](screenshots/signal-paths-templates-light.png) ·
[Template cards close-up](screenshots/signal-paths-start-zoom.png) ·
[Adding a stage](screenshots/signal-paths-add-stage-light.png) ·
[Delete strips](screenshots/signal-paths-delete-strips.png) ·
[Output Devices](screenshots/output-devices-led-meter.png) ·
[Output Devices with ports](screenshots/output-devices.png) ·
[Output Devices, stepped volume](screenshots/output-devices-stepped.png) ·
[Playback tab](screenshots/dashboard-playback.png) ·
[Recording tab](screenshots/dashboard-recording.png) ·
[Session and Bluetooth](screenshots/session-bluetooth.png) ·
[HRIR Library](screenshots/hrir-library.png) ·
[Filter Chains](screenshots/filter-chains.png) ·
[Filter Chains + HRIR close-up](screenshots/filter-chains-hrir.png) ·
[New filter chain](screenshots/new%20filterchain.png) ·
[Surround Setup wizard](screenshots/surround-setup.png) ·
[Surround Setup, advanced mode](screenshots/surround-advanced.png) ·
[Device presets](screenshots/device-presets.png) ·
[Volume style picker](screenshots/volume-style-picker.png)

## New in v0.4.0

> 📋 For highlights of every release — including **v0.3.5** (card configuration
> as its own control) and **v0.3.4** (import your existing virtual devices) —
> see the **[changelog](CHANGELOG.md)**.

**Signal Paths** — a new page, and the first one that is about the *shape* of
your audio rather than about settings. A **source** is where sound enters — one
app, a microphone, or everything on the default output — and carries its own
chain of processing. A **mix** carries a chain of its own and feeds real
devices. Sources sit on the left, mixes on the right, and the sends between
them are drawn as curves, so a glance tells you what is going where.

One source and one mix is a straight line, which is what most setups are. The
second column earns its place once a chain has to split — one source corrected
four different ways for four different pieces of hardware, without building it
four times.

<p align="center">
  <img src="screenshots/signal-paths-listening-chain.png" alt="One source feeding four mixes, each corrected for its own hardware" width="51.5%">
  <img src="screenshots/signal-paths-templates.png" alt="The template browser — a searchable grid of every ready-made strip" width="43.5%">
</p>

**Templates, from a plain speaker mix to a full broadcast chain.** An empty
board *is* the catalog: four complete paths that build both halves in one
click, then twenty-six ready-made strips running from the simplest at the top
to the ones people actually run for broadcast at the bottom — bass boost, a
loudness curve for listening quietly, crossfeed for headphones, a turntable
chain, the gate → tone → compressor → limiter voice chain, a mastering bus.
Each card draws the chain it is about to build, and anything wanting a plugin
you do not have says so on the card and is left out, rather than producing a
strip that will not start.

**The board is handled directly, not through menus.** Drag a stage along its
chain to reorder it, or onto another card to move it there. Drag a card to
rearrange a column, or onto the opposite column to connect the two. Drag an app
from one strip to another to move what it is playing through. A single click on
a stage takes it in or out of the signal; double-click or right-click opens it.
While you are dragging, every place the thing could land says so.

<p align="center">
  <img src="screenshots/signal-paths-drag-card.png" alt="Dragging a card — blue means rearrange, green means connect" width="56%">
  <img src="screenshots/signal-paths-stage-menu.png" alt="Right-clicking a stage — bypass, reorder, duplicate, remove" width="37%">
</p>

**One process per chain, not one per plugin.** Every stage in a strip is
compiled into a single filter graph: twenty effects are one entry in your
device list and one buffer hop, instead of twenty of each. Equalizer bands are
built from biquad filters rather than the preset-file kind, so frequency, gain
and Q **take effect while the audio plays** — no restart, no gap. Send one
chain to several devices and they are combined into one output for you; you
never have to learn what a combine sink is.

Also new: **effect racks can feed other effect racks** (rack → rack was the one
combination that could not be made), **equalizers are no longer stereo-only** —
a channel layout row offers everything from mono to 7.1 — and equalizers,
racks and filter chains now share one loop check, which also catches rings
running through a mix of them.

## New in v0.3.6

**Live level meters** — every volume control in the app now shows the audio
actually flowing through it: the Dashboard mixer, both device tabs, the
Devices page and the equalizers. You can see at a glance which app is making
noise and how loud it is. Meters are dB-mapped, so normal listening fills the
bar rather than sitting near zero, and each carries a peak-hold marker.
They cost effectively nothing — PipeWire's own resampler reports the peaks, so
a meter reads about **100 bytes per second** instead of streaming audio. They
run only while their row is on screen, can't keep a device awake, and never
show up as nodes in the Patchbay.

**Equalizer** — parametric-equalizer outputs you route audio through. Import
an AutoEQ / APO `ParametricEq.txt` (browse, or drag the file onto the row), or
dial the bands in by hand: each band has its own type, frequency, gain and Q,
plus its own switch so you can hear one filter in isolation without deleting
it. One click makes an equalizer the default output. **Live A/B compare**
(beta) switches an equalizer in and out *while it keeps running*, so playback
never pauses.

<p align="center">
  <img src="screenshots/equalizer-create.png" alt="Creating an equalizer — preamp, output device and a starting set of bands" width="46%">
  <img src="screenshots/equalizer-edit-bands.png" alt="Editing bands — type, frequency, gain and Q, each with its own switch" width="43.5%">
</p>

**Microphone cleanup** — a clean copy of your microphone with echo and
background noise removed (WebRTC). Noise suppression, automatic gain, a
high-pass filter and voice-activity detection are individual switches, with
extended-filter, delay-agnostic and transient suppression under Advanced. By
default it uses whatever is playing on your speakers as the echo reference, so
there is nothing to route — just pick the clean microphone as your input.

<p align="center">
  <img src="screenshots/microphone-create.png" alt="Creating a clean microphone — processing switches and advanced options" width="42%">
</p>

## New in v0.3.0

v0.3 grows the app from a config editor into a full graph tool: a live
patchbay, performance monitoring, virtual devices, routing snapshots,
per-application policies and plugin effect inserts — all built on the same
"one process per thing, drop-ins only, never touch base files" foundation.

**Patchbay** — a live node graph of the whole session, audio *and* MIDI.
Drag a port onto another to connect, select a link and press Delete to
disconnect, drag nodes to arrange them (layout persists), pan/zoom, toggle
MIDI and monitor ports, and watch the signal flow animate along active
links. Double-click any node for its properties, per-node latency, and a
live **metadata editor** (e.g. pin a stream with `target.object`).

![Patchbay — live graph with drag-to-connect patching](screenshots/patchbay.png)

**Live Monitor** — per-service CPU and RAM sparklines, xrun and DSP-load
meters, a live `pw-top` table and a cursor-followed journal with a
warnings-only filter. Optional desktop notifications fire when a link
breaks, a service fails, or new dropouts appear.

<p align="center">
  <img src="screenshots/monitor.png" alt="Live Monitor — CPU/RAM, xruns, DSP load, pw-top and logs" width="45%">
  <img src="screenshots/effects.png" alt="Effects — insert LADSPA/LV2 plugin racks into the signal path" width="51%">
</p>

**Effects** — insert racks of **LADSPA/LV2 plugins** into the signal path;
the rack shows up as an output device you route apps through. Plugins are
discovered by introspection (164 LADSPA + 71 LV2 found on the dev machine),
reorderable in series, and mono plugins run as an L/R pair automatically.
VST3/CLAP presence is detected with a bridge-host hint.

**Virtual Devices** — create null sinks, virtual microphones,
combined/aggregate devices (one sink that plays on several cards at once)
and buses/sub-mixes. Each runs as its own tiny process — temporary or
persistent — so creating or removing one never interrupts playback.

<p align="center">
  <img src="screenshots/virtual-combined.png" alt="Virtual device dialog — combine several outputs into one" width="38%">
  <img src="screenshots/app-policies.png" alt="App Policies — per-app routing, default priority, clock master" width="59%">
</p>

**App Policies** — per-application routing rules (send an app to a fixed
device, stop it auto-connecting, or pin it in place), default-device
priority, and a preferred graph **clock master**.

<p align="center">
  <img src="screenshots/virtual-devices.png" alt="Virtual Devices — null sinks, virtual mics, aggregates and buses" width="70%">
</p>

Also new: **routing snapshots** (save / recall / export / import the whole
patch by name), **per-device settings** on the Devices page (sample rate,
bit depth, period size, headroom, rename, hide), **per-device Bluetooth
profile & codec**, and **solo** toggles in the Dashboard mixer.

## Currently working on / upcoming

Rough list, no particular order, no promises on timing:

- ~~**Signal Paths — sources / mixer**~~ — shipped in v0.4.0. Still to do on it:
  - UI design improvements.
  - Proper bypass, without stopping playback to do it.
  - Card dragging could be smoother.
  - Pinning sources/mixers.
  - …and more.
- **Network audio manager** — PipeWire's network side in the GUI: RAOP/AirPlay
  sinks, streaming between machines, discovery.
- **UI improvements overall** — the app has been extremely boring to look at.
  The live meters in v0.3.6 and the Signal Paths board in v0.4.0 were first
  steps, there's a lot more to do:
  - Fix the Dashboard overview to really just be an overview.
  - Move Playback, Output Devices and Recording Devices to their own top-level
    sidebar entries.
  - …and more — I have a lot of ideas, it just takes time.
- **Quality of life** — scaling, where buttons actually live, gaps and
  spacing. Small stuff, but it adds up.
- **Individual channel volume** (I know, I know.)
- **Hide the complicated sections behind Advanced**, so opening the app isn't
  a wall of knobs unless you want it to be.
- **Microphone cleanup button in the Input/Recording section**, done cleanly —
  so you can clean up a mic without going hunting for it.

### What I'm failing at

Getting feedback. I use this thing daily on my own setup, which means I mostly
find the bugs *I* happen to walk into — and every PipeWire setup is wildly
different. If you try it and something is broken, confusing, or just annoying,
please [open an issue](https://github.com/knightinfected/PipeWireController/issues)
— even a one-liner helps more than you'd think.

## Support

[![Sponsor on GitHub](https://img.shields.io/badge/GitHub-sponsor-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/knightinfected)
[![Donate via PayPal](https://img.shields.io/badge/PayPal-support%20this%20project-00457C?logo=paypal&logoColor=white)](https://paypal.me/hmzknight)

Any support will help me a ton and will allow me to spend more time maintaining this project and if you can't but want to support consider giving me feedback or issues you came across.  Support through [GitHub Sponsors](https://github.com/sponsors/knightinfected),
or one-off through PayPal — whichever suits you.

Completely optional — the app is GPL-3.0 and always will be, and nothing is
ever locked behind a donation.

## If you like tinkering like me…

…here are some cool GitHubs to visit for sound tools:

- [JDSP4Linux](https://github.com/Audio4Linux/JDSP4Linux) — JamesDSP audio
  effects processor for PipeWire/PulseAudio
- [EasyEffects](https://github.com/wwmm/easyeffects) — the classic effects
  rack for PipeWire
- [coppwr](https://github.com/dimtpap/coppwr) — low-level PipeWire graph
  inspector and patchbay
- [polybar-pulseaudio-control](https://github.com/marioortizmanero/polybar-pulseaudio-control)
  — volume/sink switcher module for Polybar
- [virtual-surround-manager](https://github.com/Berny23/virtual-surround-manager)
  — HRIR-based virtual surround setup tool for Windows and Linux

…and some goated repos for presets, HRIRs, and DDCs:

- [M0Rf30/easyeffects-presets](https://github.com/M0Rf30/easyeffects-presets)
  — easily the best EasyEffects preset collection
- [HRIR database](https://airtable.com/appayGNkn3nSuXkaz/shruimhjdSakUPg2m/tbloLjoZKWJDnLtTc)
  — big searchable HRIR database (Airtable)
- [GentleDynamics](https://github.com/droidwayin/GentleDynamics) — the best presets 
  if you want add your own HRIRs
- [JackHack96/EasyEffects-Presets](https://github.com/JackHack96/EasyEffects-Presets)
  — the default recommendation for EasyEffects presets
- [JamesDSP DDC library](https://media.aicp-rom.com/JamesDSP/DDC/) —
  headphone DDC correction files
- [Linux_Audio](https://github.com/BayouGuru67/Linux_Audio) — hard stuff
  for large speaker systems

## Requirements

Linux with a running **PipeWire** audio session — PipeWire ≥ 1.0 and
WirePlumber ≥ 0.5 (port/profile switching uses `wpctl set-route` /
`set-profile`). GTK 4 with **libadwaita ≥ 1.4**, Python ≥ 3.10. Pure
Python, no build step. Developed and tested on PipeWire 1.6 /
libadwaita 1.9.

## Install

### Arch / CachyOS / EndeavourOS / Manjaro

Available on the [AUR](https://aur.archlinux.org/packages/pipewire-control-center):

```bash
paru -S pipewire-control-center   # or: yay -S pipewire-control-center
```

> [!NOTE]
> **Upgrading from 0.4.x or earlier?** The package was renamed to
> `pipewire-control-center` in v0.5.0, because an unrelated project holds
> `pipewire-controller` on PyPI and the two collide over `/usr/bin` and the
> desktop entry. `pipewire-controller` stays on the AUR as a transitional
> package, so an ordinary `paru -Syu` moves you across automatically.
>
> **The app itself is still PipeWire Controller.** Only the package, the
> command and the desktop entry changed — the command is now
> `pipewire-control-center`, or `pwcc` for short. Your settings are untouched:
> `~/.config/pipewire-controller/` deliberately keeps its old name, so chains,
> signal paths and equalizers all carry over.
>
> If you previously installed by hand rather than from the AUR, delete the
> leftover `~/.local/bin/pipewire-controller` symlink and any stale
> `pipewire-controller.desktop` in `~/.local/share/applications/`.

This installs everything (app, dependencies, desktop entry) — skip the
Run section below. To run from a git checkout instead, install the
dependencies manually:

```bash
sudo pacman -S --needed pipewire wireplumber pipewire-pulse gtk4 libadwaita \
    python-gobject python-cairo python-numpy python-soundfile
```

### Fedora

```bash
sudo dnf install pipewire wireplumber pipewire-pulseaudio gtk4 libadwaita \
    python3-gobject python3-numpy python3-soundfile
```

### Ubuntu 24.04+ / Debian 13+

```bash
sudo apt install pipewire wireplumber pipewire-pulse gir1.2-gtk-4.0 \
    gir1.2-adw-1 python3-gi python3-numpy python3-soundfile
```

Older releases ship a libadwaita before 1.4 and won't work.

### Other distros

Install GTK 4 + libadwaita (≥ 1.4) with GObject introspection and
PyGObject and pycairo from your package manager, then grab the Python audio bits via
pip if your distro doesn't package them:

```bash
python3 -m pip install --user numpy soundfile
```

Optional extras on any distro: `noise-suppression-for-voice` (RNNoise mic
template), `lsp-plugins-ladspa` (extra LADSPA plugins for imported
chains), PipeWire built with libmysofa (SOFA spatializer templates).

## Run

```bash
git clone https://github.com/knightinfected/PipeWireController.git
cd PipeWireController
./pipewire-control-center
```

Optional app-menu entry (the desktop file expects `pipewire-control-center`
on your `PATH`, which the symlink provides). The second symlink is a short
alias — `pwcc` is quicker to type and collides with nothing:

```bash
mkdir -p ~/.local/bin ~/.local/share/applications
ln -sf "$PWD/pipewire-control-center" ~/.local/bin/
ln -sf "$PWD/pipewire-control-center" ~/.local/bin/pwcc
cp io.github.knightinfected.PipeWireControlCenter.desktop \
    ~/.local/share/applications/
```

## What it does

**Dashboard** — pavucontrol-style tabs, refreshed live:
- *Playback / Recording*: every application stream with volume, mute and a
  device dropdown that **moves the stream live** (`target.object`
  metadata); recording streams can capture any source or any sink's
  monitor ("Monitor of X").
- *Output / Input Devices*: volume, mute, default star and **port
  selection** (speakers/headphones/HDMI, with unplugged markers).
- *Overview*: service states, graph rate/quantum/latency, an interactive
  **latency calculator** (with one-click "test live" via force-quantum/rate),
  default endpoints, stream activity, filter-chain summary.
- Four **volume-slider styles** — classic, stepped (5% notches), precision
  (−/+ nudge buttons for trackpads), LED meter (studio segment bar) —
  switchable from the pill in the bottom-right corner.

**Patchbay** — a live, draggable node graph of every node, port and link in
the session (audio and MIDI). Connect by dragging a port onto another,
disconnect a selected link with Delete, and double-click a node to edit its
metadata and read its declared latency. Save the whole routing as a named
**snapshot** and recall, export or import it later.

**Devices** — every sink and source (including virtual filter-chain sinks):
set default, volume, mute. Hardware devices expand to **per-device
settings** — sample rate, bit depth, ALSA period size, headroom, preferred
quantum, suspend timeout, plus rename/hide — written as WirePlumber rules.

**Virtual Devices** — null sinks, virtual microphones, combined/aggregate
outputs (play on several cards at once) and buses/sub-mixes, each as its own
lightweight process; temporary or persistent across reboots. Already
hand-wrote some loopback/combine/null-sink drop-ins in `pipewire.conf.d`?
**Import** adopts them into the page — classified into the right kind and
brought under the same enable/edit/delete controls.

**App Policies** — per-application rules (fixed target device, disable
auto-connect, pin in place), per-device default-selection priority, and a
preferred graph clock master.

**Effects** — a LADSPA/LV2 plugin browser and a rack builder; racks become
routable output devices. VST3/CLAP are detected with a bridge-host hint.

**Monitor** — service CPU/RAM, xrun and DSP-load meters, a live `pw-top`
table, a follow-along journal, and optional desktop notifications for broken
links, service failures and dropouts.

**Surround Setup** — a guided wizard for real 5.1/7.1 rigs: choose the
layout, pick the matching sound-card profile (★ suggests the right one;
applied instantly — doubles as a Bluetooth codec picker), apply
recommended upmix/bass-management defaults per layout, then click each
speaker on a room map to hear a test tone (the subwoofer gets 60 Hz). For
headphone users there's a one-click **Virtual 7.1 Headphones** sink —
clearly marked as virtual, never made the default automatically, and
removable like any other chain.

**Advanced toggle** (bottom-left) reveals a curated set of deeper settings
across all pages — quantum hard limit, RT scheduling, strict checks,
center-extraction cutoff, rear ambience delay, stereo widen, Hilbert taps,
ALSA headroom, and more — each explained in plain language.

**Device presets** (bookmark menu, top-right) — snapshot channel-mix
settings, volume and card profile per output device, re-apply them with one
click, or let the app auto-apply a device's preset whenever it becomes the
default output (e.g. Bluetooth headphones reconnecting).

**Server** — two layers, clearly separated:
- *Runtime overrides* (instant, non-persistent): force sample rate, force
  quantum via `pw-metadata -n settings` — experiment freely, a restart
  resets them.
- *Persistent settings*: default/allowed sample rates, quantum min/default/
  max, power-of-two quantum, mlock, denormals, RT priority, link buffers,
  strict checks, log level. The app shows the **actual merged value** from
  `/usr/share` → `/etc` → `~/.config` (including distro tweaks), marks
  anything it has overridden with an accent bar, and offers one-click reset
  per row.

**Streams** — resampler quality (0–14), disable resampling,
stereo→surround upmix (psd/simple/none), LFE crossover, LFE folding,
normalize downmix, monitor volumes. Written to *both* `client.conf.d` and
`pipewire-pulse.conf.d` so native and Pulse apps behave identically.

**Session & Bluetooth** (WirePlumber) — never-suspend devices (fixes pops /
cut-off first seconds), SBC-XQ, mSBC wideband mic, hardware volume,
auto-switch to headset profile, and **per-device Bluetooth profile & codec**
selection for each connected device.

**Filter Chains** — the core:
- Create / edit / clone / delete / enable / disable from the GUI; valid SPA
  JSON is always generated and validated before it is written.
- **Each chain runs as its own process** (`pwctl-chain@<id>` systemd user
  unit running `pipewire -c`). Toggling, editing or swapping the HRIR of one
  chain restarts *only that chain's process* — your main audio never skips.
- Templates: Virtual Surround 7.1 / 5.1 / stereo-widener (14-ch HeSuVi
  HRIR), plain 7.1 passthrough sink (no HRIR), true-stereo 4-ch IR
  convolver, 1–2-ch stereo convolver, SOFA spatializer 7.1/5.1, headphone
  crossfeed (no IR needed), parametric EQ with AutoEq file support, bass
  boost, RNNoise noise-cancelling mic.
- The right template is auto-selected from the analyzed channel count of the
  chosen IR (14 ch → HeSuVi, 4 ch → true-stereo, 1–2 ch → stereo IR,
  `.sofa` → spatializer).
- Optional fixed output device per chain, convolver gain, per-template knobs.
- **Import** detects your existing hand-written drop-ins in
  `~/.config/pipewire/filter-chain.conf.d/` (including an `inactive/`
  folder), brings them under app management verbatim, and still supports
  HRIR swapping and raw text editing with validation.
- Per-chain journal viewer and generated-config viewer.

**HRIR Library** — import single files or whole folders (e.g. a downloaded
HeSuVi `hrir/` directory). Every file is analyzed (channels / rate / length /
format) and classified: 14 ch = HeSuVi, 4 ch = true stereo, 1–2 ch = plain
IR, `.sofa` = HRTF. "New chain" on any file creates a chain with the right
template pre-selected. A built-in generator synthesizes a basic 14-channel
demo HRIR so virtual surround can be tested before downloading a real set
(get real ones from the HeSuVi project's `hrir` folder — use the 14-channel
wavs, not the `*-.wav` variants).

**Tools** — restart audio stack / WirePlumber, journal viewer, latency
calculator, view every drop-in the app has written, open config folders,
one-click *reset all overrides*.

## Where things are written

| What | Where |
|---|---|
| Server settings | `~/.config/pipewire/pipewire.conf.d/99-pipewire-controller.conf` |
| Stream settings | `~/.config/pipewire/{client,pipewire-pulse}.conf.d/99-pipewire-controller.conf` |
| Session settings | `~/.config/wireplumber/wireplumber.conf.d/99-pipewire-controller.conf` |
| Device & app policy rules | `~/.config/pipewire-controller/rules.json` (regenerates the WirePlumber and stream drop-ins above) |
| Chain metadata | `~/.config/pipewire-controller/chains/*.json` |
| Virtual device metadata | `~/.config/pipewire-controller/virtual/*.json` |
| Routing snapshots | `~/.config/pipewire-controller/routing/*.json` |
| Generated chain / virtual-device configs | `~/.config/pipewire-controller/generated/*.conf` |
| HRIR library | `~/.config/pipewire-controller/hrir/` |
| UI preferences & device presets | `~/.config/pipewire-controller/ui.json` |
| Chain / virtual-device runner unit | `~/.config/systemd/user/pwctl-chain@.service` |

Deleting those paths removes every trace of the app. Base config files are
never modified.

## Design notes

- A small parser/serializer for PipeWire's relaxed **SPA JSON** dialect
  (`pwctl/spa_json.py`) reads any real-world config (verified round-trip on
  all shipped configs) and writes idiomatic conf files.
- Persistent changes surface a banner naming exactly which service needs a
  restart, with a one-click restart button; runtime changes apply instantly.
- All subprocess work (`pw-dump`, `systemctl`, …) runs off the UI thread.

## Acknowledgments

- Thanks to **Wim Taymans**, creator of PipeWire, for reviewing the Server
  page's quantum and buffer settings — his feedback corrected the
  `link.max-buffers` description and the quantum hard-limit range in v0.3.1.
