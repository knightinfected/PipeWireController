# Working on

What's being built or planned. No dates; shipped items move to the
[changelog](CHANGELOG.md).

Currently I work on this project all by myself, and I really appreciate people
reporting issues and contributing.

If you're comfortable working with AI tools, I'd love a pull request for any of
the planned items below. I'm focusing on the backend first, so the rest will be
slower. I'll add tests once the backend is fully done for everything. You'll be
credited as a co-author, and [donations](README.md#support) are always
appreciated since they help cover the AI side plus give me some motivation for
feature requests. Thanks!

| Where | What | Status |
|---|---|---|
| Dashboard | Needs attention: plain-language status, one-click fixes (muted output, BT headset stuck in call mode), output switching, keyboard shortcuts | *Next* |
| Dashboard | Volume, mute and output controls under each *Playing now* app | *Designed* |
| Dashboard | Game/chat mix knob, mic and output picked manually | *Planned* |
| Dashboard | EasyEffects and JamesDSP presets, managed from the Dashboard | *Designed* |
| Dashboard | Event-driven updates instead of polling | *Designed* |
| Signal Paths | Mic → effects → virtual mic: pick the capture mic per strip ([#29](https://github.com/knightinfected/PipeWireController/issues/29)) | *Built, being finished* |
| Patchbay | Matrix view: connect many channels at once ([#27](https://github.com/knightinfected/PipeWireController/issues/27)) | *Planned* |
| Network | Network audio: AirPlay speakers, other PCs, listen on your phone | *Designed* |
| Session & Bluetooth | WirePlumber session settings done properly: every live setting, no restart | *Designed* |
| Effects | Plugin knobs: sliders for LADSPA/LV2 controls ([#15](https://github.com/knightinfected/PipeWireController/issues/15)) | *Designed* |
| Saving | Scenes (switchable setups, auto-switch on device plug-in) and backup/restore | *Designed* |
| Saving | Snapshot and preset bugs: an app playing on two outputs, settings leaking between devices | *Fixes worked out* |
| Preferences | Allow volume above 100 % | *Planned* |
| Whole app | Motion: fades and slides instead of jumps, respects reduce-motion | *In progress* |
