# pyACSworker

A small worker around the ACS SPiiPlus Python SDK. Subclass it, put your per-pass
controller I/O in `child_loop()`, and drive it either as a thread or from your own
superloop. Connection and reconnection are handled for you in both cases.

Used as a git submodule. Requires `SPiiPlusPython`, installed with the ACS ADK suite.

## Subclass it

```python
from acs_worker import ACSController

class Gimbal(ACSController):
    pan = 0.0

    def child_loop(self):
        # One pass of controller I/O. Runs on whichever thread owns the loop.
        pan = self.read_scalar("ACTUAL_PAN", "float")
        if pan is not None:          # None means the read failed; keep the last value
            self.pan = pan
```

`child_loop()` is the only method you need to override.

## Thread mode

The worker owns a thread and services the controller on its own, as fast as
`period` allows.

```python
gimbal = Gimbal("10.0.0.100")
gimbal.start()                       # child_loop() now runs on its own thread

while running:
    print(gimbal.pan)                # read state from any thread
    time.sleep(0.1)

gimbal.stop()                        # joins the thread and closes the link
```

To drive the controller from another thread, either call `write_scalar` directly
(the SDK serializes per handle) or hand the call to the worker so a multi-call
sequence can't interleave:

```python
gimbal.queue_command(gimbal.write_scalar, "PAN_VEL", "float", 0.5)
```

## Superloop mode

You own the loop. `tick()` does one pass and never blocks — including while the
controller is unreachable.

```python
gimbal = Gimbal("10.0.0.100")        # note: no start()

while running:
    gimbal.tick()                    # connect if needed, drain queue, child_loop()
    do_everything_else()
    time.sleep(0.010)

gimbal.stop()
```

## Choosing between them

Thread mode keeps controller I/O off your loop, so a slow transaction can't stall
the rest of your app. Superloop mode keeps everything on one thread — simpler to
reason about, but every blocking read lands inside your tick. If your loop has a
hard deadline (a watchdog, telemetry at a fixed rate), prefer thread mode.

Don't mix them: once `start()` has been called, `tick()` from another thread raises.

## Connection handling

Both modes connect on their own and retry every `reconnect_interval` seconds. The
attempt runs on a throwaway thread, because a TCP connect to a powered-down
controller blocks for seconds — that's what keeps `tick()` non-blocking.

While the link is down `read_scalar` returns `None` and `write_scalar` returns
`False`, so check `is_connected` before trusting state. Nothing on the controller
survives a power cycle, so re-push any settings when `is_connected` goes true.

## API

| | |
|---|---|
| `child_loop()` | Override this. One pass of your controller I/O. |
| `tick()` | One pass, non-blocking. `True` if `child_loop()` ran. |
| `start()` / `stop()` | Thread mode. `stop()` works in both modes. |
| `is_connected` | Link state. |
| `read_scalar(name, 'int'\|'float')` | Binary read. `None` on failure. |
| `write_scalar(name, type, value)` | Binary write. `False` on failure. |
| `execute_command("?VERSION")` | Raw ACSPL+ transaction, returns `str` or `None`. |
| `queue_command(fn, *args)` | Run `fn` on the loop thread on the next pass. |

Constructor:

```python
ACSController(ip_address="10.0.0.100", port=701,
              reconnect_interval=5.0,   # seconds between connect attempts
              period=0.001,             # thread mode only: sleep between passes
              idle_period=0.1)          # thread mode only: sleep while disconnected
```

`period` paces thread mode only — a superloop sets its own rate by how often it
calls `tick()`. On Windows `time.sleep(0.001)` really takes ~1.5 ms, so the 1 ms
default gives roughly 620 Hz. The SDK releases the GIL during its blocking calls,
so the worker doesn't starve other threads at any of these values.

See `SPiiPlus-Python-Library-Reference-Programmers-Guide.pdf` for the underlying SDK.
