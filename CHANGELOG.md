<!--
HOW TO ADD A NEW ENTRY (for the next big update):
Copy the block below to the TOP of the list (newest first) and fill it in.

## [vX.Y.Z](https://github.com/knightinfected/PipeWireController/releases/tag/vX.Y.Z) — YYYY-MM-DD
**One-line headline of the release**
- Highlight one
- Highlight two

![Caption](screenshots/your-shot.png)

Screenshots: take them, save as kebab-case PNGs in `screenshots/`, then add the
`![Caption](screenshots/name.png)` line. Leave a placeholder note (like the
0.3.2 entry) until the image exists so the page never shows a broken image.
-->

# Changelog — release highlights

The bigger updates, newest first, with screenshots. This is a curated
highlights reel, not an exhaustive log — for the complete history see the
[GitHub releases](https://github.com/knightinfected/PipeWireController/releases)
and commit log.

---

## Unreleased

**Hiding a device now really hides it — and there's a way back**

- **New "Hide the whole sound card" switch.** Hiding a single output or input
  only refuses that endpoint — the card itself stayed registered, so your
  desktop's sound settings kept offering it, profile switcher and all. The new
  switch disables the card and every input and output it provides, everywhere.
  ([#3](https://github.com/knightinfected/PipeWireController/issues/3))
- **The per-endpoint switch now says what it does** — "Hide this output" /
  "Hide this input", with a subtitle that spells out that the sound card itself
  stays listed. Both switches sit together on the device's own row.
- **New "Hidden devices" list, with Unhide.** A hidden device is gone from the
  audio graph, so it used to disappear from PipeWire Controller too and the
  only way back was deleting the generated WirePlumber file by hand. Hidden
  endpoints and cards are now listed at the bottom of the Devices page and can
  be brought back with one click.
- **A hide that isn't actually in effect is flagged.** If the generated
  WirePlumber file was removed or edited by hand, the app no longer insists the
  device is hidden — the entry is marked *not in effect*, and Unhide clears it.

_Screenshots: to be added before release._

---

## [v0.3.6](https://github.com/knightinfected/PipeWireController/releases/tag/v0.3.6) — 2026-07-29
**Everything shows its level — plus the new Equalizer and Microphone cleanup pages**

- **Live level meters everywhere.** Every volume control in the app now shows
  the audio actually flowing through it — the Dashboard mixer, both device
  tabs, the Devices page and the equalizers. You can see at a glance which app
  is making noise, whether a device is really passing audio, and how loud it
  is. Meters are dB-mapped, so normal listening fills the bar instead of
  sitting near zero, and they carry a peak-hold marker.
- Metering costs effectively nothing: PipeWire's own resampler reports the
  peaks, so each meter reads about **100 bytes per second** rather than
  streaming audio. Meters run only while their row is on screen, they can't
  keep a device awake, and they never appear as nodes in the Patchbay.

![Playback tab — a live meter on every stream](screenshots/playback-live-meters.png)

![Dashboard — Overview](screenshots/dashboard-0.3.6.png)

- **Equalizer page** — parametric-equalizer outputs you route audio through.
  Import an AutoEQ / APO `ParametricEq.txt` (browse or just drag the file onto
  the row), or dial the bands in by hand: each band has its own type, frequency,
  gain and Q, and its own switch so you can hear one filter in isolation without
  deleting it. One click makes an equalizer the default output.
- **Live A/B compare** (beta) — switch an equalizer in and out *while it keeps
  running*, so playback never pauses. Bypassed audio goes straight to the
  output device.

<p align="center">
  <img src="screenshots/equalizer-create.png" alt="Creating an equalizer — preamp, output device and a starting set of bands" width="46%">
  <img src="screenshots/equalizer-edit-bands.png" alt="Editing bands — type, frequency, gain and Q, each with its own switch" width="43.5%">
</p>

![The Equalizer page — an equalizer running, with its inline mixer](screenshots/equalizer.png)

- **Microphone cleanup** — a clean copy of your microphone with echo and
  background noise removed (WebRTC). Noise suppression, automatic gain, a
  high-pass filter and voice-activity detection are individual switches, with
  extended-filter, delay-agnostic and transient suppression under Advanced. By
  default it uses whatever is playing on your speakers as the echo reference,
  so there is nothing to route.

![Microphone cleanup — configured clean microphones](screenshots/microphone-cleanup.png)

<p align="center">
  <img src="screenshots/microphone-create.png" alt="Creating a clean microphone — processing switches and advanced options" width="42%">
</p>

- **Clearer running devices.** A running equalizer or microphone is marked with
  an accent edge that runs the whole height of its block, stopped ones step
  back, and the inline mixer is laid out as the signal path — level, compare,
  what is playing through it, where it goes.
- **Fixed:** text containing `&` or `<` made a row's title or subtitle vanish
  entirely. This hit stream names reported by apps, device descriptions and
  anything you had typed yourself — equalizer, filter-chain and virtual-device
  names, app policies and HRIR filenames.

---

## [v0.3.5](https://github.com/knightinfected/PipeWireController/releases/tag/v0.3.5) — 2026-07-29
**Bug fixes related to Card Config and additions- Card configuration is its own control — with a way out of a broken profile**

- **Configuration switcher is now a dedicated row**, one per card, on the
  Output/Input Devices tabs — instead of riding on one arbitrary device row.
  It no longer jumps between rows or disappears when a profile exposes several
  sinks at once (Pro Audio) or none at all (Off), so the switcher — and the way
  back — is always reachable.
- **Added One-click Reset to a working profile** when a card gets stuck. WirePlumber
  can save a profile that later can't work (e.g. an HDMI-surround profile saved
  while nothing is plugged into HDMI), leaving you with no audio and, before,
  no obvious way out.
- **Added "unavailable" warning** — the ⚠ now flags only profiles that are
  genuinely unavailable, so a playable-but-unprobeable profile like *Pro Audio*
  is no longer falsely marked.

![The card Configuration row, separate from the device rows](screenshots/dashboard-configuration-row.png)
![A stuck profile flagged, with one-click Reset to a working one](screenshots/dashboard-configuration-reset.png)

---

## [v0.3.4](https://github.com/knightinfected/PipeWireController/releases/tag/v0.3.4) — 2026-07-27
**Import your existing virtual devices**

- **Import existing devices** (Virtual Devices page) — adopt loopback,
  combine, and null-sink drop-ins you hand-wrote in
  `~/.config/pipewire/pipewire.conf.d`. They're classified into the right kind
  (null sink · virtual mic · bus · combined), imported disabled, and the
  original file is moved into an `inactive/` folder so it loads only once (no
  duplicate). A file picker covers configs kept elsewhere.
- The header-bar bookmark button is now labelled **Device Presets** so it reads
  clearly next to the star.

---

## [v0.3.3](https://github.com/knightinfected/PipeWireController/releases/tag/v0.3.3) — 2026-07-25
**Virtual-device fixes — Mono layout and no more stray mic routing**

- **Mono 1.0 layout** for virtual devices — null sinks, virtual mics,
  aggregates, and buses can now be created as mono. Editing an existing mono
  device selects the right layout too.
- **Fixed virtual mics auto-routing to the default sink** — the null-source
  playback stream now sets `node.autoconnect=false` (matching the null-sink and
  Pro Audio map nodes), so WirePlumber no longer links a virtual mic onto your
  speakers.

---

## [v0.3.2](https://github.com/knightinfected/PipeWireController/releases/tag/v0.3.2) — 2026-07-23
**Pro Audio channel maps and a dashboard configuration switcher**

- **Pro Audio channel map** (Virtual Devices) — map a friendly channel layout
  onto the generic AUX channels a card exposes in its *Pro Audio* profile
  (e.g. a stereo sink whose FL/FR land on the interface's AUX0/AUX1). Both
  output and input directions, arbitrary N-channel maps. The links survive
  reboots and PipeWire restarts.
- **Dashboard configuration switcher** — change a card's profile (Analog
  Stereo Duplex, Pro Audio, HDMI, Off, …) straight from the Output/Input
  Devices tabs, like pavucontrol's Configuration tab.
- **`pyproject.toml`** — a proper Python project (metadata, entry point, and
  dev tooling for type-checking/linting).
- Virtual-device dialog is now a **resizable window** with a larger default
  size; fixed note-wrapping and dropdown truncation in that dialog.

![Pro Audio output map — FL/FR to AUX0/AUX1](screenshots/pro-audio-map.png)
![Pro Audio input map — AUX to a virtual mic](screenshots/pro-audio-input-map.png)
![Dashboard card configuration switcher](screenshots/dashboard-configuration.png)
![Every card profile, like pavucontrol's Configuration tab](screenshots/dashboard-configuration-options.png)
![The mapped channels routed in the Patchbay](screenshots/patchbay-pro-audio.png)

---

## [v0.3.1](https://github.com/knightinfected/PipeWireController/releases/tag/v0.3.1) — 2026-07-23
**Server tuning fixes — feedback from Wim Taymans (creator of PipeWire)**

- Raised the quantum hard-limit ceiling (8192 was only ~43 ms at 192 kHz;
  the dropdown now reaches 32768, real cap 65536) and explained the
  frames-vs-rate trade-off inline.
- Corrected the `link.max-buffers` description (it's mostly about video
  buffers, not audio) and its default.
- Added a **Custom…** typed entry to the Default/Minimum/Maximum quantum
  dropdowns for values off the preset list.

![Server page](screenshots/server.png)

---

## [v0.3.0](https://github.com/knightinfected/PipeWireController/releases/tag/v0.3.0) — 2026-07-22
**The big one — Patchbay, Monitor, Virtual Devices, Effects, App Policies**

- **Patchbay** — live audio + MIDI node graph, drag-to-connect, node metadata
  editor, routing snapshots (save/recall/export/import).
- **Monitor** — service CPU/RAM, xruns, DSP load, live `pw-top`, journal
  follow, desktop notifications.
- **Virtual Devices** — null sinks, virtual mics, combine/aggregate devices,
  buses.
- **Effects** — LADSPA/LV2 discovery and an insert-rack chain.
- **App Policies & per-device rules** — per-app routing, default-device
  priority, clock master, per-device rate/format/period/headroom/rename/hide,
  plus per-device Bluetooth profile & codec and dashboard mixer solo.

![Patchbay](screenshots/patchbay.png)
![Monitor](screenshots/monitor.png)
![Virtual devices](screenshots/virtual-devices.png)
![Effects](screenshots/effects.png)
![App policies](screenshots/app-policies.png)
