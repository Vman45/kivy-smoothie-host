# implements raw hid interface to a teensy and a MPG pendant

from easyhid import Enumeration
from kivy.logger import Logger
from kivy.app import App

import threading
import traceback

mpg_rawhid= None
def start(args=""):
    global mpg_rawhid
    #print("start with args: {}".format(args))
    pid, vid= args.split(':')
    mpg_rawhid= MPG_rawhid(int(pid, 16), int(vid, 16))
    mpg_rawhid.start()
    return True

def stop():
    global mpg_rawhid
    mpg_rawhid.stop()

class RawHID:
    PACKET_LEN = 64
    TIMEOUT = 1000 # milliseconds
    CONNECT_RETRIES = 3

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.hid = None
        return None

    # Initialize libhid and connect to USB device.
    #
    # @vid - Vendor ID of the USB device.
    # @pid - Product ID of the USB device.
    #
    # Returns True on success.
    # Returns False on failure.
    def open(self, vid, pid):
        retval = False

        # Stores an enumeration of all the connected USB HID devices
        #en = Enumeration(vid=vid, pid=pid)
        en = Enumeration()
        # for d in en.find():
        #     print(d.description())

        # return a list of devices based on the search parameters
        devices = en.find(vid=vid, pid=pid, interface=0)
        if not devices:
            Logger.debug("RawHID: No matching device found")
            return None

        if len(devices) > 1:
            Logger.debug("RawHID: more than one device found: {}".format(devices))
            return None

        # open the device
        self.hid= devices[0]
        self.hid.open()

        Logger.debug("RawHID: Opened: {}".format(self.hid.description()))

        return True

    # Close HID connection and clean up.
    #
    # Returns True on success.
    # Returns False on failure.
    def close(self):
        self.hid.close()
        return True

    # Send a USB packet to the connected USB device.
    #
    # @packet  - Data, in string format, to send to the USB device.
    # @timeout - Read timeout, in milliseconds. Defaults to TIMEOUT.
    #
    # Returns True on success.
    # Returns False on failure.
    def send(self, packet, timeout=TIMEOUT):
        n= self.hid.write(packet)
        return n

    # Read data from the connected USB device.
    #
    # @len     - Number of bytes to read. Defaults to PACKET_LEN.
    # @timeout - Read timeout, in milliseconds. Defaults to TIMEOUT.
    #
    # Returns the received bytes on success.
    # Returns None on failure.
    def recv(self, plen=PACKET_LEN, timeout=TIMEOUT):

        packet= self.hid.read(size= plen, timeout=timeout)
        return packet

class MPG_rawhid():
    def __init__(self, vid, pid):
        # Teensy vendor ID and product ID
        self.vid = vid
        self.pid = pid
        self.hid = RawHID()

    def twos_comp(self, val, bits):
        """compute the 2's complement of int value val"""
        if (val & (1 << (bits - 1))) != 0: # if sign bit is set e.g., 8bit: 128-255
            val = val - (1 << bits)        # compute negative value
        return val                         # return positive value as is

    def start(self):
        self.quit= False
        self.t= threading.Thread(target=self._run)
        self.t.start()

    def stop(self):
        self.quit= True
        self.t.join()

    def _run(self):
        app= App.get_running_app()

        try:
            # Open a connection to the Teensy
            if self.hid.open(self.vid, self.pid):

                Logger.info("MPG_rawhid: Connected to HID device %04X:%04X" % (self.vid, self.pid))

                # Infinite loop to read data from the Teensy
                while not self.quit:
                    data = self.hid.recv(timeout=50)
                    if data is not None:
                        size = len(data)
                        if size == 0:
                            continue

                        if not (data[0] == 0x12 and data[1] == 0x34):
                            Logger.error("MPG_rawhid: Not an MPG HID packet")
                            continue

                        axis= data[2]
                        mult= data[3]
                        step= self.twos_comp(data[4], 8)
                        s= data[5]
                        estop= data[6]

                        Logger.debug("MPG_rawhid: axis: {}, mult: {}, step: {}, speed: {}, estop: {}".format(axis, mult, step, s, estop))

                        # a= data[7]
                        # b= data[8]
                        # c= data[9]
                        # d= data[10]
                        # us= a<<24 | b<<16 | c << 8 | d
                        # print("us= {}".format(us))

                        if app.is_connected:
                            if estop == 1 and app.status != 'Alarm':
                                app.comms.write('\x18')
                                continue

                        else:
                            continue

                        if app.main_window.is_printing:
                            continue

                        if axis == 0:
                            continue;

                        alut= {1:'X', 2:'Y', 3:'Z', 4:'A'}
                        dist= 0.01 * step * mult
                        if s == 0: s= 1
                        speed= s/10.0
                        app.comms.write("$J {}{} F{}\n".format(alut[axis], dist, speed))


                # Close the Teensy connection
                self.hid.close()

                Logger.info("MPG_rawhid: Disconnected from HID device")

            else:
                Logger.error("MPG_rawhid: Failed to open HID device %04X:%04X" % (self.vid, self.pid))

        except:
            Logger.warn("MPG_rawhid: Exception - {}".format(traceback.format_exc()))

