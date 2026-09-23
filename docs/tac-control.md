# Debug-board (TAC) control

Many Qualcomm boards sit on a debug board (Alpaca / TAC) that drives their power
switch, buttons and boot-mode straps. Labs often front those debug boards with a
[pytactl](https://github.com/qualcomm/pytactl) REST service (`pytactl service`), and
the device dictionary's power commands call it:

```jinja
{% set hard_reset_command = ['/usr/local/bin/tac-api.py --serial NNPMP28T002L --command powerOff',
                             'sleep 2',
                             '/usr/local/bin/tac-api.py --serial NNPMP28T002L --command powerOn'] %}
```

LAVA's device commands only expose what the dictionary scripts (`power_on`,
`hard_reset`, user commands). `tac_info` and `tac_command` go one level lower: any
quick method the debug board defines (`powerOn`, `bootToEDL`, `bootToUEFI`, ...) and
individual pins (`kpd_pwr`, ...), including holding a pin for a set time. Typical uses:

- hold the power key long enough for the PMIC to reset a board whose SoC has frozen —
  with `qcom_scm.download_mode=full` on the kernel command line that lands the board in
  crashdump mode, where LAVA's qdl teardown (`ramdump: true`) collects the dump;
- put a board into EDL/UEFI or toggle straps by hand during bring-up.

Requires hosted mode with the gateway enabled — see [deployment.md](deployment.md).

## How requests reach the TAC service

The TAC service, like ser2net, is on the dispatcher's Docker network, out of reach of
both the MCP server and the containers a job runs. The
[serial-console](serial-console.md) proxy already runs there as a LAVA Test Services
container with a reverse tunnel to the gateway, so TAC requests ride it: the gateway
sends a TAC control line over the session's tunnel, the proxy makes that single HTTP
request to the TAC service and returns the status and body.

So TAC control needs a **console session** (`open_console_session` + your deploy/boot
job) or a **board session opened with `console=true`**, on a device that allows Test
Services.

## Scope and safety

- **Only the session's own board.** The TAC serial is read from the assigned board's
  device dictionary (the `tac-api ... --serial <serial>` in its power commands) once
  LAVA has scheduled the job. The agent never supplies it.
- **Only the gateway can send TAC requests.** Each control line carries a token derived
  from the session's private key. The proxy holds that key (it dials out with it); a
  human given `attach_console` access to the same relay does not, so they cannot use
  the relay to reach other boards on the TAC service.
- **Only TAC routes.** The proxy forwards `GET /<serial>[/quick|/command|/pin[/<name>]]`,
  `PUT /<serial>/quick/<method>` and `PUT /<serial>/(command|pin)/<name>?value=0|1`,
  nothing else on the service.
- **Holds always release.** `tac_command(..., hold_seconds=N)` asserts the pin, waits
  (up to 60 s) and releases it even if the wait is interrupted.

Pin-level control bypasses the dictionary's power sequencing, so leave the board powered
and in a sane state for the rest of the job.

## Configuration

The proxy talks to `http://tac-api:80` by default. A lab whose service lives elsewhere
sets `TAC_API_URL` in the device dictionary's `environment` (LAVA writes it into the
proxy's compose `.env`); an empty value disables the bridge.

```jinja
{% set environment = {'TAC_API_URL': 'http://tac-service:5000'} %}
```

## Tools

- `tac_info(session_id)` — the board's TAC serial, its quick methods and pin commands.
- `tac_command(session_id, name, value=None, hold_seconds=None)` — run a quick method
  (no value), set a pin (`value=1`/`0`), or press a pin for `hold_seconds`.

Both accept a console session id or a board session id (opened with `console=true`).
