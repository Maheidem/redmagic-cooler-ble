# AGENTS.md

## Cooler Experiment Result

On 2026-07-08, this repo was used to test whether a RedMagic/Nubia phone cooler
attached to the Mac measurably improves Mac CPU/GPU temperatures under sustained
heavy load.

Experiment:

- `90s` high baseline: thermal switch on, fan level 8.
- `180s` cooler off: thermal switch off, fan level 1.
- `180s` high recovery: thermal switch on, fan level 8.
- Main comparison ignored the first `30s` of each phase.
- Safety abort threshold was `103 C` CPU/GPU or a macOS thermal/performance warning.

Result:

| Phase | Cooler | CPU avg | GPU avg | Mac fan avg | Mac power avg |
| --- | ---: | ---: | ---: | ---: | ---: |
| High baseline | `26 W`, ~`4987 RPM`, `16.8 C` | `90.9 C` | `83.5 C` | `5776 RPM` | `115.8 W` |
| Cooler off | `1 W`, `0 RPM`, `37.0 C` | `91.8 C` | `84.4 C` | `5762 RPM` | `118.1 W` |
| High recovery | `26 W`, ~`5022 RPM`, `16.2 C` | `92.2 C` | `84.7 C` | `5776 RPM` | `120.3 W` |

Conclusion: the cooler physically works and consumes about `25 W` extra power
when active, but in this attachment/setup its effect on actual Mac CPU/GPU die
temperature was negligible to very small. It cooled its own/contact area quickly
but did not meaningfully reduce Mac fan RPM, CPU temperature, GPU temperature, or
macOS thermal-warning state during the observed load.

After any live experiment, restore a safe/off state before leaving the machine:

1. Disable cooler auto control.
2. Disable the cooler thermal switch.
3. Disable Diablo mode.
4. Set lighting mode to off.
5. Set fan level to 1.
6. Stop the local dashboard process.

On 2026-07-08, after sending auto off, thermal switch off, Diablo off, light
off, and fan level 1, the cooler remained BLE-reachable but no fresh RPM or
power telemetry frames were observed during a 20 second passive watch. It only
reported temperature, fan-limit, and protection frames, which appears closer to
the app's actual off state than fan level 1 alone.
