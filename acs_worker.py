"""ACS controller worker, usable two ways.

Thread mode::

    acs = MyController()
    acs.start()             # run() drives tick() on its own thread
    ...
    acs.stop()

Superloop mode::

    acs = MyController()    # never call start()
    while running:
        acs.tick()          # one pass, never blocks
        time.sleep(0.010)
    acs.stop()

Both modes execute the same per-pass work; only the caller and the rate differ.
Pick one: tick() refuses to run on a foreign thread once start() has been
called, because two threads sharing one comm handle interleave.

Subclasses override child_loop() and nothing else.
"""

import SPiiPlusPython as sp
import threading
import time
import logging
import queue


class ACSController(threading.Thread):

    def __init__(self, ip_address: str = "10.0.0.100", port: int = 701,
                 reconnect_interval: float = 5.0,
                 period: float = 0.001,
                 idle_period: float = 0.1):
        self.ip_address = ip_address
        self.port = port
        self.handle = None  # Will store the communication handle
        self.is_connected = False

        threading.Thread.__init__(self, daemon=True)
        self.logger = logging.getLogger('__main__.'+__name__)
        self.logger.info("acs initialized")
        self.output_queue = queue.Queue()
        # Controller calls queued by other threads, drained by tick() on
        # whichever thread owns the loop. See queue_command().
        self.command_queue = queue.Queue()

        # Pacing. period and idle_period apply to thread mode only -- a
        # superloop caller sets its own rate by how often it calls tick().
        self.reconnect_interval = reconnect_interval
        self.period = period
        self.idle_period = idle_period

        self._clock = time.monotonic
        self._shutdown = threading.Event()
        self._connecting = False      # a connect attempt is in flight
        self._next_attempt = 0.0      # monotonic deadline for the next one

    def connect(self) -> bool:
        self.logger.info(f"acs connecting to {self.ip_address}:{self.port}")
        try:
            # Open the communication channel via TCP/IP
            # The handle is stored in self.handle for use in other functions
            self.handle = sp.OpenCommEthernetTCP(self.ip_address, self.port)
            if  self.handle == -1:
                self.logger.warning(f"acs failed to connect to {self.ip_address}:{self.port}, error code: {sp.GetLastError()}")
                self.is_connected = False
                self.handle = None
            else:
                self.logger.info(f"ACS driver connected to {self.ip_address} successfully")
                self.is_connected = True

        except Exception as err:
            self.logger.warning(f"acs failed to connect with error: {err}")
            self.is_connected = False
            self.handle = None

        return self.is_connected

    def disconnect(self):
        if not self.is_connected:
            self.logger.debug("acs already disconnected")
            return

        self.logger.info("acs disconnecting")
        try:
            sp.CloseComm(self.handle)
        except Exception as err:
            self.logger.warning(f"acs CloseComm failed: {err}")
        finally:
            # Cleared either way: a handle we cannot close is not a handle we
            # can keep using, and tick() has to be able to reconnect.
            self.handle = None
            self.is_connected = False
        self.logger.info("acs disconnected")

    #------------------------------------------------------------

    def execute_command(self, command_str: str) -> str | None:
        """
        Sends a raw ACSPL+ command string to the controller and returns the response.

        Args:
            command_str (str): The ACSPL+ command to send (e.g., "?T1", "VEL(0)=100").

        Returns:
            str | None: The controller's response string, or None if an error occurs.
            """
        if not self.is_connected:
            self.logger.warning(f"cannot execute '{command_str}': not connected")
            return None
        try:
            # Use Transaction to send a command and receive a reply.
            reply = sp.Transaction(self.handle, command_str)

            # Check if reply is an AcsError object
            if hasattr(reply, '__class__') and 'AcsError' in str(type(reply)):
                self.logger.warning(f"error executing command '{command_str}': {reply}")
                return None

            # The reply often includes a newline character, so we strip it.
            if isinstance(reply, str):
                return reply.strip()
            else:
                self.logger.warning(f"unexpected reply type for command '{command_str}': {type(reply)}")
                return None
        except Exception as e:
            self.logger.warning(f"error executing command '{command_str}': {e}")
            return None

    def read_scalar(self, var_name: str, var_type: str, nbuf: int = sp.ACSC_NONE) -> int | float | None:
        """Read a scalar ACS variable over the binary interface.

        var_type is 'int' (ReadInteger) or 'float' (ReadReal). nbuf selects the
        program buffer for a local variable; ACSC_NONE means a global.
        Returns None if not connected, on an unknown type, or on a controller error.
        """
        if not self.is_connected:
            self.logger.warning(f"cannot read '{var_name}': not connected")
            return None

        if var_type == 'int':
            reply = sp.ReadInteger(self.handle, nbuf, var_name,
                                   sp.ACSC_NONE, sp.ACSC_NONE, sp.ACSC_NONE, sp.ACSC_NONE)
        elif var_type == 'float':
            reply = sp.ReadReal(self.handle, nbuf, var_name,
                                sp.ACSC_NONE, sp.ACSC_NONE, sp.ACSC_NONE, sp.ACSC_NONE)
        else:
            self.logger.error(f"unsupported variable type '{var_type}' for '{var_name}'")
            return None

        # The SDK returns an AcsError object on failure rather than raising.
        if isinstance(reply, sp.AcsError):
            self.logger.warning(f"failed to read '{var_name}': error {sp.GetLastError()}")
            return None
        return reply

    def write_scalar(self, var_name: str, var_type: str, value: int | float,
                     nbuf: int = sp.ACSC_NONE) -> bool:
        """Write a scalar ACS variable over the binary interface.

        Mirror of read_scalar. Returns True on success, False otherwise.
        """
        if not self.is_connected:
            self.logger.warning(f"cannot write '{var_name}': not connected")
            return False

        if var_type == 'int':
            reply = sp.WriteInteger(self.handle, nbuf, var_name,
                                    sp.ACSC_NONE, sp.ACSC_NONE, sp.ACSC_NONE, sp.ACSC_NONE,
                                    int(value))
        elif var_type == 'float':
            reply = sp.WriteReal(self.handle, nbuf, var_name,
                                 sp.ACSC_NONE, sp.ACSC_NONE, sp.ACSC_NONE, sp.ACSC_NONE,
                                 float(value))
        else:
            self.logger.error(f"unsupported variable type '{var_type}' for '{var_name}'")
            return False

        if isinstance(reply, sp.AcsError):
            self.logger.warning(f"failed to write '{var_name}'={value}: error {sp.GetLastError()}")
            return False
        return True

    def read_array(self, var_name: str, var_type: str, count: int,
                   first: int = 0, nbuf: int = sp.ACSC_NONE) -> list | None:
        """Read `count` elements of a one-dimensional ACS array.

        The array sibling of read_scalar -- and the SDK has no separate array call:
        ReadInteger/ReadReal become an array read purely by being handed a real index
        range where read_scalar passes ACSC_NONE. The whole range travels in one
        transaction, which is the point: one round trip instead of `count` of them.

        Reads indices first..first+count-1 inclusive. The library offers no way to ask
        how long an array is, so `count` is the caller's business; asking for more than
        the array holds is a controller error, not a short read.

        Returns a plain list of int/float -- the SDK hands back a numpy array, and a
        bare scalar when the range is a single element; both are normalised here so
        callers need no numpy. Returns None if not connected, on an unknown type, on a
        controller error, or if the reply is not `count` values long. That last case is
        None rather than a short list on purpose: callers unpack the result positionally
        and already treat None as "keep the last known value".
        """
        if not self.is_connected:
            self.logger.warning(f"cannot read '{var_name}': not connected")
            return None
        if count < 1 or first < 0:
            self.logger.error(f"cannot read '{var_name}': bad range first={first} count={count}")
            return None

        last = first + count - 1  # the SDK's index range is inclusive at both ends
        if var_type == 'int':
            reply = sp.ReadInteger(self.handle, nbuf, var_name,
                                   first, last, sp.ACSC_NONE, sp.ACSC_NONE)
        elif var_type == 'float':
            reply = sp.ReadReal(self.handle, nbuf, var_name,
                                first, last, sp.ACSC_NONE, sp.ACSC_NONE)
        else:
            self.logger.error(f"unsupported variable type '{var_type}' for '{var_name}'")
            return None

        # The SDK returns an AcsError object on failure rather than raising.
        if isinstance(reply, sp.AcsError):
            self.logger.warning(
                f"failed to read '{var_name}[{first}..{last}]': error {sp.GetLastError()}")
            return None

        if hasattr(reply, 'tolist'):        # numpy array, the documented array return
            values = reply.tolist()
        elif isinstance(reply, (list, tuple)):
            values = list(reply)
        else:                               # a one-element range came back unwrapped
            values = [reply]

        if len(values) != count:
            self.logger.warning(
                f"read '{var_name}[{first}..{last}]' returned {len(values)} values, "
                f"expected {count}")
            return None
        return values

    def write_array(self, var_name: str, var_type: str, values,
                    first: int = 0, nbuf: int = sp.ACSC_NONE) -> bool:
        """Write a one-dimensional ACS array. Mirror of read_array.

        `values` is any sequence; its length is the count, so there is no separate
        argument to keep in step with it. Writes indices first..first+len(values)-1
        inclusive in one transaction. Returns True on success, False otherwise.

        Like write_scalar, elements are coerced to the requested type, so a value that
        is not a number raises rather than returning False.
        """
        if not self.is_connected:
            self.logger.warning(f"cannot write '{var_name}': not connected")
            return False

        values = list(values)
        if not values or first < 0:
            self.logger.error(
                f"cannot write '{var_name}': bad range first={first} count={len(values)}")
            return False

        last = first + len(values) - 1
        if var_type == 'int':
            reply = sp.WriteInteger(self.handle, nbuf, var_name,
                                    first, last, sp.ACSC_NONE, sp.ACSC_NONE,
                                    [int(v) for v in values])
        elif var_type == 'float':
            reply = sp.WriteReal(self.handle, nbuf, var_name,
                                 first, last, sp.ACSC_NONE, sp.ACSC_NONE,
                                 [float(v) for v in values])
        else:
            self.logger.error(f"unsupported variable type '{var_type}' for '{var_name}'")
            return False

        if isinstance(reply, sp.AcsError):
            self.logger.warning(
                f"failed to write '{var_name}[{first}..{last}]': error {sp.GetLastError()}")
            return False
        return True

    def queue_command(self, func, *args, **kwargs) -> None:
        """Queue a controller call for execution on the loop-owning thread.

        The SDK serializes calls per communication handle, so calling it from
        another thread works but blocks that thread until the handle is free --
        and a multi-call sequence could still be interleaved unless it is
        wrapped in CaptureComm/ReleaseComm. Keeping every call on one thread
        avoids both problems: producers only enqueue, and tick() drains.

        Useful in both modes. In superloop mode the drain happens on whichever
        thread calls tick().
        """
        self.command_queue.put_nowait((func, args, kwargs))

    def drain_commands(self) -> None:
        """Execute every queued call. Runs on the loop-owning thread, from tick()."""
        while True:
            try:
                func, args, kwargs = self.command_queue.get_nowait()
            except queue.Empty:
                return
            try:
                func(*args, **kwargs)
            except Exception as e:
                name = getattr(func, "__name__", repr(func))
                self.logger.error(f"queued command {name} failed: {e}")

    #------------------------------------------------------------

    def _service_reconnect(self) -> None:
        """Start a connect attempt if one is due and none is already running.

        The attempt runs on a throwaway thread because OpenCommEthernetTCP
        blocks: a TCP connect to a powered-down controller can stall for
        seconds, which a superloop caller cannot afford inside its tick.
        """
        if self._connecting or self._clock() < self._next_attempt:
            return
        self._connecting = True
        threading.Thread(target=self._attempt_connect, daemon=True,
                         name=f"acs-connect-{self.ip_address}").start()

    def _attempt_connect(self) -> None:
        try:
            self.connect()
            if self._shutdown.is_set() and self.is_connected:
                # Shutdown landed while the connect was in flight -- do not leak
                # the handle we just opened.
                self.disconnect()
        finally:
            # Deadline set before the flag clears, so a failed attempt cannot
            # re-fire on the very next tick.
            self._next_attempt = self._clock() + self.reconnect_interval
            self._connecting = False

    def child_loop(self):
        pass

    def tick(self) -> bool:
        """One pass of servicing. Never blocks.

        Reconnects if the link is down, otherwise drains the command queue and
        runs child_loop(). Returns True when child_loop() ran.

        Call this from your own loop in superloop mode; run() calls it for you
        in thread mode.
        """
        if self.is_alive() and threading.current_thread() is not self:
            raise RuntimeError(
                "tick() called from outside the worker thread -- use start() or "
                "tick(), not both: two threads on one comm handle interleave")

        if self._shutdown.is_set():
            # stop() is final. Without this a superloop that ticks once more on
            # its way out would re-open the link it just closed.
            return False

        if not self.is_connected:
            self._service_reconnect()
            return False

        try:
            self.drain_commands()
            self.child_loop()
        except Exception as e:
            self.logger.error(f"error in child_loop (disconnecting): {e}")
            self.disconnect()
            return False
        return True

    def run(self):
        while not self._shutdown.is_set():
            if self.tick():
                # time.sleep, NOT _shutdown.wait: on Windows Event.wait floors at
                # the ~15.6 ms scheduler tick regardless of the timeout asked for,
                # which would cap servicing near 65 Hz. time.sleep uses a
                # high-resolution timer (a 1 ms request measures ~1.5 ms).
                # Either way the GIL is released, so other threads run.
                time.sleep(self.period)
            else:
                # Link is down, nothing to service: trade that resolution for a
                # prompt stop(), since wait() returns the moment the event is set.
                self._shutdown.wait(self.idle_period)
        self.disconnect()

    def stop(self, timeout: float | None = 2.0) -> None:
        """Shut down and close the link. Works in both modes.

        Thread mode joins the worker, which disconnects on its way out.
        Superloop mode (start() never called) disconnects here.
        """
        self._shutdown.set()
        if threading.current_thread() is self:
            return          # called from child_loop(); run() disconnects as it unwinds
        if self.is_alive():
            self.join(timeout)
        else:
            self.disconnect()
