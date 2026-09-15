# MeshTerm on hardware

MeshTerm talks to a **node** — a MeshCore radio that speaks the companion protocol over
serial, Bluetooth LE or TCP. Plug a companion board into USB, or pair one over Bluetooth,
and none of this page applies: MeshTerm finds it, and the [README](../README.md) and the
[command line manual](cli.md) are all you need.

This page is for the hardware that *doesn't* present a node that way. Each such device has
its own step-by-step manual, and this page only tells you which one is yours.

## Which guide is yours

| I have… | Read |
| --- | --- |
| a ClockworkPi **PicoCalc**, stock or already running Calculinux on a Luckfox Lyra, and I want MeshTerm on its own screen | **[MeshTerm on the PicoCalc](picocalc.md)** — from the shopping list to a working machine |
| …and I want a **radio** inside it | the same manual, [Phase 3](picocalc.md#phase-3--add-the-radio) — a XIAO nRF52840 + Wio-SX1262 soldered to the Lyra's UART |
| a ClockworkPi **uConsole** with the hackergadgets AIO, or a Raspberry Pi with a Waveshare LoRa HAT — an SX1262 on the host's own SPI bus | **[MeshTerm on the uConsole](uconsole.md)** — the bridge that fronts that chip as a companion |
| a MeshCore companion on USB, BLE or Wi-Fi | nothing here — the [README](../README.md) |

The two targets have nothing in common but MeshTerm itself. The device-side scripts each
manual runs live in [`scripts/`](../scripts/), whose [index](../scripts/README.md) says what
every file does and which machine it runs on. Read a script before you run it; most run as
root and change the machine they run on.

## The regular and picocalc platforms

MeshTerm resolves one frozen **platform** at boot and draws everything through it. The
**regular** platform is a desktop or ssh terminal: 72 readable columns, truecolour, emoji,
a footer hint line. The **picocalc** platform is a framebuffer console: 53 columns, no
emoji, an F-key lane instead of a hint line, and sixteen colour slots rather than a colour
space — nothing loses its colour there, the node hue and the recency heat gradient
quantise to those slots instead. It is detected from the device tree on the Lyra, and
`--platform picocalc` or `MESHTERM_PLATFORM=picocalc` forces it anywhere, which is how you
preview the handheld layout on a desktop:

```bash
meshterm --mock --platform picocalc     # the whole app, simulated radio, handheld layout
meshterm specimen                       # the visual language on one card
meshterm platform                       # which platform this terminal resolved to
```

What that platform needs from the console — a font with the glyphs the interface draws,
and the stock sixteen-slot palette — is installed by the PicoCalc manual's setup script;
the details are in that manual and in the scripts' own headers.
