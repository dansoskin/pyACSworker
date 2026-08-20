import SPiiPlusPython as sp
import threading
import time
import logging
import queue

import math


class ACSController(threading.Thread):

    def __init__(self, ip_address: str = "10.0.0.100", port: int = 701):
        self.ip_address = ip_address
        self.port = port
        self.handle = None  # Will store the communication handle
        self.is_connected = False

        threading.Thread.__init__(self, daemon=True)
        self.logger = logging.getLogger('__main__.'+__name__)
        self.logger.info("acs initialized")
        self.output_queue = queue.Queue()
        # Controller calls queued by other threads, drained by run() on this
        # thread. See queue_command().
        self.command_queue = queue.Queue()

    def connect(self) -> bool:
        print(f"Attempting to connect to controller at {self.ip_address}:{self.port}...")
        try:
            # Open the communication channel via TCP/IP
            # The handle is stored in self.handle for use in other functions
            self.handle = sp.OpenCommEthernetTCP(self.ip_address, self.port)
            if  self.handle == -1:
                self.logger.info(f"acs failed to connect to {self.ip_address}:{self.port}, error code: {sp.GetLastError()}")
                self.is_connected = False
                self.handle = None
            else:
                self.logger.info(f"ACS driver connected to {self.ip_address} successfully")
                self.is_connected = True
        
        except Exception as err:
            self.logger.info(f"acs failed to connect with error: {err}")
            self.is_connected = False
            self.handle = None

    def disconnect(self):
        if self.is_connected:
            print("Disconnecting from controller...")
            sp.CloseComm(self.handle)
            self.handle = None
            self.is_connected = False
            print("Disconnected.")
        else:
            print("Already disconnected.")

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
            print("Error: Not connected to the controller.")
            return None
        try:
            # Use Transaction to send a command and receive a reply.
            reply = sp.Transaction(self.handle, command_str)
            
            # Check if reply is an AcsError object
            if hasattr(reply, '__class__') and 'AcsError' in str(type(reply)):
                print(f"Error executing command '{command_str}': {reply}")
                return None
            
            # The reply often includes a newline character, so we strip it.
            if isinstance(reply, str):
                return reply.strip()
            else:
                print(f"Unexpected reply type for command '{command_str}': {type(reply)}")
                return None
        except Exception as e:
            print(f"Error executing command '{command_str}': {e}")
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

    def queue_command(self, func, *args, **kwargs) -> None:
        """Queue a controller call for execution on the ACS thread.

        The SDK serializes calls per communication handle, so calling it from
        another thread works but blocks that thread until the handle is free --
        and a multi-call sequence could still be interleaved unless it is
        wrapped in CaptureComm/ReleaseComm. Keeping every call on this one
        thread avoids both problems: producers only enqueue, and run() drains.
        """
        self.command_queue.put_nowait((func, args, kwargs))

    def drain_commands(self) -> None:
        """Execute every queued call. Runs on the ACS thread, from run()."""
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

    def child_loop(self):
        pass

    def run(self):
        self.connect()
        while True:
            if not self.is_connected:
                time.sleep(5)
                self.connect()
                continue

            try:
                self.drain_commands()
                self.child_loop()
            except Exception as e:
                self.logger.error(f"error in child_loop (disconnecting): {e}")
                self.disconnect()
                continue

            time.sleep(0.001)

